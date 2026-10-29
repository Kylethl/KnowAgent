# 数据不出域守卫（P7-B2：合规）
# ---------------------------------------------------------------------
# 背景（《需求清单》P7 功能2「数据不出域配置」）：
#   本系统会把两类数据发到外部：
#     1. 用户提问 + 检索到的知识库片段  → 对话模型（DeepSeek）
#     2. 文档正文 / 图片 / 音视频         → 向量与多模态模型（qwen 系列）
#   对金融、政务、医疗等私有化场景，「数据去了哪个域名」必须可控、可审计。
#
# 本模块提供两个能力：
#   - check_outbound(url, purpose)：在**真正发请求之前**校验目标域名，
#     strict 模式下不在白名单内直接抛 DataResidencyViolation（fail-closed）；
#   - residency_report()：给状态页/合规自检用的「当前出站拓扑」快照，
#     列出每个用途的目标域名、是否放行、是否为内网本地模型。
#
# 三种放行规则（按顺序判断）：
#   1. standard 模式：一律放行（仅记录，不拦截）；
#   2. 目标是本机/私网地址：放行——这类地址本身就意味着「模型已本地化」，
#      数据在机房内部流转，符合不出域要求；
#   3. 域名在白名单内（支持 ".aliyuncs.com" 这种后缀写法）：放行；
#      其余一律拒绝。
#
# 为什么白名单支持后缀匹配：
#   云厂商的 endpoint 常带地域前缀（dashscope.cn-beijing.aliyuncs.com），
#   每次都写全量域名极易漏配；用后缀可以一次覆盖同厂商的所有地域节点。

from typing import Dict, List, Optional
from urllib.parse import urlparse

from ..config import get_settings
from ..core.errors import DataResidencyViolation
from ..core.logging import get_logger

logger = get_logger(__name__)

# 本机/内网判定用的固定主机名（这些名字一定指向本机或局域网）
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}


def _is_private_host(host: str) -> bool:
    """判断主机是否为「本机 / 私网地址」（本地化模型即部署在这些地址上）。

    判定口径：
      - 固定主机名（localhost 等）→ 是；
      - IPv4 私网段：10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、127.0.0.0/8；
      - Kubernetes 集群内域名：以 .svc、.svc.cluster.local、.cluster.local 结尾。

    Args:
        host: 主机名或 IP（小写）。

    Returns:
        True 表示属于本机/私网（数据不出域）。
    """
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES:
        return True
    # Kubernetes Service 域名：集群内部解析，流量不出集群网络
    if host.endswith(".svc") or host.endswith(".svc.cluster.local") or host.endswith(
        ".cluster.local"
    ):
        return True

    # IPv4 私网段判断（只处理点分十进制，IPv6 简化处理：以 fe80/fc/fd 开头视为内网）
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            return False
        if a == 10 or a == 127:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        if a == 192 and b == 168:
            return True
        if a == 169 and b == 254:
            # 链路本地地址（169.254/16）：容器/云主机元数据服务所在网段
            return True
    if host.startswith(("fe80:", "fc", "fd")):
        return True
    return False


def _host_matches_allowlist(host: str, allowlist: List[str]) -> bool:
    """判断主机是否命中白名单（精确匹配或后缀匹配）。

    例：白名单含 ".aliyuncs.com" 时，
        "dashscope.aliyuncs.com" 命中；"evil-aliyuncs.com" 不命中
        （后缀匹配要求以 "." 开头，避免被相似域名绕过）。

    Args:
        host: 目标主机名（小写）。
        allowlist: 白名单列表（小写，可含前导点表示后缀）。

    Returns:
        True 表示允许出站。
    """
    host = (host or "").strip().lower()
    for item in allowlist:
        item = item.strip().lower()
        if not item:
            continue
        if item.startswith("."):
            # 后缀匹配：host 必须以该后缀结尾（含点），或恰好等于去掉点后的名字
            if host.endswith(item) or host == item.lstrip("."):
                return True
            continue
        if host == item:
            return True
        # 兼容「写成域名但期望覆盖子域」的配置：example.com 覆盖 a.example.com
        if host.endswith("." + item):
            return True
    return False


def extract_host(url: str) -> str:
    """从 URL 中取出小写主机名（解析失败返回空串）。

    Args:
        url: 完整 URL（如 https://api.deepseek.com/v1/chat/completions）。

    Returns:
        主机名；URL 非法时返回空串。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return (parsed.hostname or "").lower()


def check_outbound(url: str, purpose: str) -> Dict[str, object]:
    """出站前校验目标域名是否允许（strict 模式下可能抛异常）。

    Args:
        url: 即将请求的完整 URL。
        purpose: 用途标识（llm / embedding / media / storage / sentry），
            用于日志与错误提示（"哪个能力"被拦了）。

    Returns:
        {"host": 主机名, "allowed": bool, "local": bool, "mode": 模式}

    Raises:
        DataResidencyViolation: strict 模式下目标不在白名单且非内网地址。
    """
    settings = get_settings()
    host = extract_host(url)
    mode = (settings.DATA_RESIDENCY_MODE or "standard").strip().lower()

    # URL 解析不出主机：说明配置本身有问题，交给调用方后续的请求失败去暴露，
    # 这里不拦（否则会掩盖「配置写错」的真实原因）。
    if not host:
        return {"host": "", "allowed": False, "local": False, "mode": mode}

    local = _is_private_host(host)

    # 1) standard 模式：只记录不拦截
    if mode != "strict":
        logger.debug("出站调用（standard 模式）：purpose=%s，host=%s", purpose, host)
        return {"host": host, "allowed": True, "local": local, "mode": mode}

    # 2) strict：本机/私网一律放行（模型已本地化，数据不出机房）
    if local:
        logger.debug("出站调用（strict 模式，内网目标）：purpose=%s，host=%s", purpose, host)
        return {"host": host, "allowed": True, "local": True, "mode": mode}

    # 3) strict：白名单校验
    allowlist = settings.outbound_allowed_hosts_list
    if _host_matches_allowlist(host, allowlist):
        logger.info("出站调用（strict 模式，白名单放行）：purpose=%s，host=%s", purpose, host)
        return {"host": host, "allowed": True, "local": False, "mode": mode}

    # 4) 拒绝：fail-closed，并写警告日志（这条日志本身就是合规证据）
    logger.warning(
        "数据不出域拦截：purpose=%s，host=%s，白名单=%s",
        purpose,
        host,
        ",".join(allowlist) or "(空，仅允许内网)",
    )
    raise DataResidencyViolation(host=host, purpose=purpose)


def residency_report() -> Dict[str, object]:
    """生成当前出站拓扑快照（状态页与合规自检使用）。

    Returns:
        形如：
        {
          "mode": "strict",
          "region": "cn",
          "strict": True,
          "allowlist": [".aliyuncs.com"],
          "targets": [
            {"purpose": "llm", "label": "对话生成", "url": "...", "host": "...",
             "local": False, "allowed": True, "model": "deepseek-flash"},
            ...
          ]
        }
    """
    settings = get_settings()
    allowlist = settings.outbound_allowed_hosts_list
    mode = (settings.DATA_RESIDENCY_MODE or "standard").strip().lower()
    strict = mode == "strict"

    # 各能力的「逻辑出站目标」：与实际调用点保持一致（见 llm.py / embedding.py / media_extract.py）
    # 说明：这里只做展示，不做真实请求，因此可以安全地在状态页高频调用。
    candidates = [
        {
            "purpose": "llm",
            "label": "对话生成",
            "url": f"{settings.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions",
            "model": settings.DEEPSEEK_MODEL,
        },
        {
            "purpose": "embedding",
            "label": "知识库向量",
            "url": settings.QWEN_EMBEDDING_BASE_URL,
            "model": settings.QWEN_EMBEDDING_MODEL,
        },
        {
            "purpose": "media",
            "label": "图片 OCR / 音视频转写",
            "url": settings.media_llm_base_url,
            "model": f"{settings.MEDIA_OCR_MODEL} / {settings.MEDIA_ASR_MODEL}",
        },
    ]

    targets: List[Dict[str, object]] = []
    for item in candidates:
        host = extract_host(str(item["url"]))
        local = _is_private_host(host)
        # standard 模式下「允许」恒为 True；strict 下才真正判定
        allowed = True if not strict else (local or _host_matches_allowlist(host, allowlist))
        targets.append(
            {
                "purpose": item["purpose"],
                "label": item["label"],
                "url": str(item["url"]),
                "host": host,
                "model": item["model"],
                "local": local,
                "allowed": allowed,
            }
        )

    return {
        "mode": mode,
        "region": settings.DATA_REGION,
        "strict": strict,
        "allowlist": allowlist,
        "targets": targets,
    }


def describe_purpose(purpose: str) -> str:
    """把用途代码翻译为中文（错误提示用）。"""
    return {
        "llm": "对话生成模型",
        "embedding": "知识库向量模型",
        "media": "多模态解析模型",
        "storage": "对象存储",
        "sentry": "错误上报服务",
    }.get(purpose, purpose)
