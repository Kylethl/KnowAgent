# Prometheus 指标（P7-Dep1：监控）
# ---------------------------------------------------------------------
# 职责：把「接口流量 / 耗时 / LLM 调用与配额 / 检索模式 / 依赖健康 / 备份结果」
#       暴露为 Prometheus 文本格式，供 Prometheus 抓取、Grafana 出图与告警。
#
# 设计取舍（务必先读，改起来才不会踩坑）：
#   1. **指标基数（cardinality）是成本**：绝不能用原始 URL（含 UUID、query）当 label，
#      否则每个文档、每个会话都会长出一条新时间序列，Prometheus 内存会被打爆。
#      因此 path 一律经 normalize_path() 归一化（UUID/数字 → :id）。
#   2. **可选依赖**：prometheus_client 是生产依赖（已在 requirements.txt），
#      但本地开发/裁剪镜像里可能没装。缺失时本模块自动降级为进程内轻量实现，
#      /metrics 仍能输出合法文本——保证「装不装都不影响业务、装了才有完整能力」。
#   3. **不记录敏感信息**：label 里只有方法、路径模板、状态码、结果枚举，
#      没有用户 ID、没有令牌、没有请求体。
#
# 命名规范：统一前缀 knowagent_，单位后缀 _seconds / _total，符合 Prometheus 惯例。

from __future__ import annotations

import re
import threading
from typing import Dict, List, Optional, Tuple

from .logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------
# 指标名常量：集中定义，避免各处手写字符串导致 Grafana 面板对不上
# ---------------------------------------------------------------------
HTTP_REQUESTS_TOTAL = "knowagent_http_requests_total"
HTTP_DURATION = "knowagent_http_request_duration_seconds"
HTTP_IN_PROGRESS = "knowagent_http_requests_in_progress"
LLM_REQUESTS_TOTAL = "knowagent_llm_requests_total"
LLM_DURATION = "knowagent_llm_request_duration_seconds"
LLM_TOKENS_TOTAL = "knowagent_llm_tokens_total"
LLM_RATE_LIMITED_TOTAL = "knowagent_llm_rate_limited_total"
RAG_RETRIEVAL_TOTAL = "knowagent_rag_retrieval_total"
DEPENDENCY_UP = "knowagent_dependency_up"
AUDIT_EVENTS_TOTAL = "knowagent_audit_events_total"
BACKUP_LAST_SUCCESS = "knowagent_backup_last_success_timestamp_seconds"
BACKUP_TOTAL = "knowagent_backup_total"
APP_INFO = "knowagent_app_info"

# 耗时直方图的桶（秒）：覆盖 5ms~30s，问答接口偶发 20s+ 也能落桶
_DURATION_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0
)

# UUID / 纯数字路径段 → 归一化占位符，控制 label 基数
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_NUM_RE = re.compile(r"^\d+$")


# ---------------------------------------------------------------------
# 降级实现（未安装 prometheus_client 时使用）
# ---------------------------------------------------------------------
class _FallbackStore:
    """极简指标存储：只实现本项目用到的 counter / gauge / histogram。

    为什么自己写而不是「没装就直接不输出」：
        /metrics 是部署验收的一部分（K8s 探针、Prometheus 抓取目标），
        本地开发时也应能确认端点通、指标有数；直接 500 会让人误判成配置错误。
    """

    def __init__(self) -> None:
        # 互斥锁：指标更新可能来自多个线程（Celery worker / 线程池）
        self._lock = threading.Lock()
        # 计数器：{(name, labels_tuple): value}
        self._counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
        # 仪表盘：同上
        self._gauges: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
        # 直方图：{key: {"count": n, "sum": x, "buckets": {le: n}}}
        self._histograms: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], dict] = {}

    @staticmethod
    def _key(name: str, labels: Dict[str, str]) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
        """把 name + labels 转成可哈希的字典键（labels 排序保证稳定）。"""
        return name, tuple(sorted(labels.items()))

    def inc(self, name: str, labels: Dict[str, str], value: float = 1.0) -> None:
        """计数器累加。"""
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def set_gauge(self, name: str, labels: Dict[str, str], value: float) -> None:
        """仪表盘直接设值。"""
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, labels: Dict[str, str], value: float) -> None:
        """直方图观测一次（累计 count/sum/bucket）。"""
        key = self._key(name, labels)
        with self._lock:
            entry = self._histograms.setdefault(
                key, {"count": 0, "sum": 0.0, "buckets": {b: 0 for b in _DURATION_BUCKETS}}
            )
            entry["count"] += 1
            entry["sum"] += value
            # 落入对应的桶：Prometheus 的桶是「小于等于 le」的累计口径
            for bound in _DURATION_BUCKETS:
                if value <= bound:
                    entry["buckets"][bound] += 1
                    break

    def render(self) -> str:
        """按 Prometheus 文本格式输出全部指标。

        说明：每个指标名只输出一次 `# TYPE`，随后是其全部时间序列；
        直方图必须补 `le="+Inf"` 桶（等于总次数），否则 histogram_quantile 会算错。
        """
        lines: List[str] = []
        declared: set[str] = set()

        with self._lock:
            # ---------- 计数器 ----------
            for (name, labels), value in sorted(self._counters.items()):
                if name not in declared:
                    lines.append(f"# TYPE {name} counter")
                    declared.add(name)
                lines.append(f"{name}{_fmt_labels(labels)} {value}")

            # ---------- 仪表盘 ----------
            for (name, labels), value in sorted(self._gauges.items()):
                if name not in declared:
                    lines.append(f"# TYPE {name} gauge")
                    declared.add(name)
                lines.append(f"{name}{_fmt_labels(labels)} {value}")

            # ---------- 直方图 ----------
            for (name, labels), entry in sorted(self._histograms.items()):
                if name not in declared:
                    lines.append(f"# TYPE {name} histogram")
                    declared.add(name)
                for bound, count in entry["buckets"].items():
                    bucket_labels = dict(labels)
                    bucket_labels["le"] = str(bound)
                    lines.append(
                        f"{name}_bucket"
                        f"{_fmt_labels(tuple(sorted(bucket_labels.items())))} {count}"
                    )
                inf_labels = dict(labels)
                inf_labels["le"] = "+Inf"
                lines.append(
                    f"{name}_bucket"
                    f"{_fmt_labels(tuple(sorted(inf_labels.items())))} {entry['count']}"
                )
                lines.append(f"{name}_sum{_fmt_labels(labels)} {entry['sum']}")
                lines.append(f"{name}_count{_fmt_labels(labels)} {entry['count']}")

        # 末尾保留换行，符合 exposition 格式要求
        return "\n".join(lines) + "\n"



def _fmt_labels(labels: Tuple[Tuple[str, str], ...]) -> str:
    """把 label 元组格式化为 `{k="v",...}`；空标签返回空串。"""
    if not labels:
        return ""
    # 值里的反斜杠/引号需转义，否则会产生非法 exposition 文本
    body = ",".join(
        f'{k}="{str(v).replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for k, v in labels
    )
    return "{" + body + "}"


# ---------------------------------------------------------------------
# 后端选择：优先 prometheus_client，缺失则降级
# ---------------------------------------------------------------------
_prom: Optional[dict] = None
_fallback: Optional[_FallbackStore] = None


def _init_backend() -> None:
    """惰性初始化指标后端（模块导入时不做事，避免拖慢冷启动与单测）。"""
    global _prom, _fallback
    if _prom is not None or _fallback is not None:
        return

    try:
        from prometheus_client import (  # 局部导入：让缺失时能干净降级
            CollectorRegistry,
            Counter,
            Gauge,
            Histogram,
            generate_latest,
            CONTENT_TYPE_LATEST,
        )

        # 使用独立 registry：不污染 prometheus_client 的默认全局注册表，
        # 单测重复 import / reload 时不会因「重复注册同名指标」抛 ValueError。
        registry = CollectorRegistry()

        _prom = {
            "registry": registry,
            "generate_latest": generate_latest,
            "content_type": CONTENT_TYPE_LATEST,
            "http_requests": Counter(
                HTTP_REQUESTS_TOTAL,
                "HTTP 请求总数（按方法/路径模板/状态码）",
                ["method", "path", "status"],
                registry=registry,
            ),
            "http_duration": Histogram(
                HTTP_DURATION,
                "HTTP 请求耗时（秒）",
                ["method", "path"],
                buckets=_DURATION_BUCKETS,
                registry=registry,
            ),
            "http_in_progress": Gauge(
                HTTP_IN_PROGRESS,
                "正在处理的 HTTP 请求数",
                registry=registry,
            ),
            "llm_requests": Counter(
                LLM_REQUESTS_TOTAL,
                "LLM 调用次数（按结果：success/error/rate_limited）",
                ["outcome"],
                registry=registry,
            ),
            "llm_duration": Histogram(
                LLM_DURATION,
                "LLM 调用耗时（秒）",
                buckets=_DURATION_BUCKETS,
                registry=registry,
            ),
            "llm_tokens": Counter(
                LLM_TOKENS_TOTAL,
                "LLM token 用量（direction=prompt/completion）",
                ["direction"],
                registry=registry,
            ),
            "rate_limited": Counter(
                LLM_RATE_LIMITED_TOTAL,
                "被限流/超额拒绝的请求数（scope=minute/day/tokens）",
                ["scope"],
                registry=registry,
            ),
            "rag": Counter(
                RAG_RETRIEVAL_TOTAL,
                "检索结果分布（mode=rag/no_hit/disabled/fallback）",
                ["mode"],
                registry=registry,
            ),
            "dependency_up": Gauge(
                DEPENDENCY_UP,
                "依赖是否可用（1=可用，0=不可用）",
                ["dependency"],
                registry=registry,
            ),
            "audit": Counter(
                AUDIT_EVENTS_TOTAL,
                "审计事件数（按动作）",
                ["action"],
                registry=registry,
            ),
            "backup_last_success": Gauge(
                BACKUP_LAST_SUCCESS,
                "最近一次成功备份的 Unix 时间戳（秒）",
                registry=registry,
            ),
            "backup_total": Counter(
                BACKUP_TOTAL,
                "备份执行次数（result=success/failure）",
                ["result"],
                registry=registry,
            ),
            "app_info": Gauge(
                APP_INFO,
                "应用信息（恒为 1，版本/环境在 label 上）",
                ["version", "env"],
                registry=registry,
            ),
        }
        logger.debug("指标后端：prometheus_client")
    except ImportError:
        _fallback = _FallbackStore()
        logger.debug("指标后端：内置降级实现（未安装 prometheus_client）")


def normalize_path(raw_path: str) -> str:
    """把原始 URL 路径归一化为「路径模板」，压低指标 label 基数。

    例：
        /documents/3f2a...-.../chunks/9b1c... → /documents/:id/chunks/:id
        /admin/documents/123/review          → /admin/documents/:id/review

    Args:
        raw_path: 原始路径（不含 query）。

    Returns:
        归一化后的路径模板。
    """
    if not raw_path:
        return "/"
    segments = raw_path.split("/")
    normalized = []
    for seg in segments:
        if not seg:
            normalized.append(seg)
            continue
        if _UUID_RE.fullmatch(seg) or _NUM_RE.match(seg):
            normalized.append(":id")
        else:
            normalized.append(seg)
    return "/".join(normalized)


# ---------------------------------------------------------------------
# 对外 API（业务/中间件只调用这些函数，不关心后端是哪个）
# ---------------------------------------------------------------------

def observe_http(method: str, raw_path: str, status_code: int, duration: float) -> None:
    """记录一次 HTTP 请求（次数 + 耗时）。

    Args:
        method: HTTP 方法（GET/POST...）。
        raw_path: 原始路径（内部会归一化）。
        status_code: 响应状态码。
        duration: 耗时（秒）。
    """
    _init_backend()
    path = normalize_path(raw_path)
    labels = {"method": (method or "GET").upper(), "path": path, "status": str(status_code)}
    if _prom is not None:
        _prom["http_requests"].labels(**labels).inc()
        _prom["http_duration"].labels(method=labels["method"], path=path).observe(duration)
    elif _fallback is not None:
        _fallback.inc(HTTP_REQUESTS_TOTAL, labels)
        _fallback.observe(HTTP_DURATION, {"method": labels["method"], "path": path}, duration)


def inc_in_progress(delta: int) -> None:
    """增减「处理中请求数」仪表盘（进入 +1，离开 -1）。

    Args:
        delta: +1 或 -1。
    """
    _init_backend()
    if _prom is not None:
        _prom["http_in_progress"].inc(delta)
    elif _fallback is not None:
        # 降级实现没有原子 inc，直接读改写（只用于本地开发，可接受）
        key = (HTTP_IN_PROGRESS, ())
        current = _fallback._gauges.get(key, 0.0)
        _fallback.set_gauge(HTTP_IN_PROGRESS, {}, current + delta)


def inc_llm_request(outcome: str) -> None:
    """记录一次 LLM 调用结果。

    Args:
        outcome: success / error / rate_limited / not_configured。
    """
    _init_backend()
    if _prom is not None:
        _prom["llm_requests"].labels(outcome=outcome).inc()
    elif _fallback is not None:
        _fallback.inc(LLM_REQUESTS_TOTAL, {"outcome": outcome})


def observe_llm_duration(seconds: float) -> None:
    """记录 LLM 调用耗时（秒）。"""
    _init_backend()
    if _prom is not None:
        _prom["llm_duration"].observe(seconds)
    elif _fallback is not None:
        _fallback.observe(LLM_DURATION, {}, seconds)


def inc_llm_tokens(prompt_tokens: int, completion_tokens: int) -> None:
    """累加 token 用量（供成本看板与配额核对）。

    Args:
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。
    """
    _init_backend()
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return
    if _prom is not None:
        if prompt_tokens > 0:
            _prom["llm_tokens"].labels(direction="prompt").inc(prompt_tokens)
        if completion_tokens > 0:
            _prom["llm_tokens"].labels(direction="completion").inc(completion_tokens)
    elif _fallback is not None:
        if prompt_tokens > 0:
            _fallback.inc(LLM_TOKENS_TOTAL, {"direction": "prompt"}, prompt_tokens)
        if completion_tokens > 0:
            _fallback.inc(LLM_TOKENS_TOTAL, {"direction": "completion"}, completion_tokens)


def inc_rate_limited(scope: str) -> None:
    """记录一次被限流拒绝。

    Args:
        scope: minute / day / tokens。
    """
    _init_backend()
    if _prom is not None:
        _prom["rate_limited"].labels(scope=scope).inc()
    elif _fallback is not None:
        _fallback.inc(LLM_RATE_LIMITED_TOTAL, {"scope": scope})


def inc_rag(mode: str) -> None:
    """记录一次检索的模式分布（rag/no_hit/disabled/fallback）。"""
    _init_backend()
    if _prom is not None:
        _prom["rag"].labels(mode=mode).inc()
    elif _fallback is not None:
        _fallback.inc(RAG_RETRIEVAL_TOTAL, {"mode": mode})


def set_dependency(dependency: str, up: bool) -> None:
    """设置依赖可用性（1/0），由状态检查定期刷新。

    Args:
        dependency: postgres / redis / object_storage / deepseek / embedding / media。
        up: 是否可用。
    """
    _init_backend()
    value = 1.0 if up else 0.0
    if _prom is not None:
        _prom["dependency_up"].labels(dependency=dependency).set(value)
    elif _fallback is not None:
        _fallback.set_gauge(DEPENDENCY_UP, {"dependency": dependency}, value)


def inc_audit(action: str) -> None:
    """记录一次审计事件（按动作分类）。"""
    _init_backend()
    if _prom is not None:
        _prom["audit"].labels(action=action).inc()
    elif _fallback is not None:
        _fallback.inc(AUDIT_EVENTS_TOTAL, {"action": action})


def record_backup(success: bool, timestamp: Optional[float] = None) -> None:
    """记录一次备份结果（次数 + 最近成功时间戳）。

    Args:
        success: 是否成功。
        timestamp: 成功时的时间戳（秒）；失败时可传 None。
    """
    _init_backend()
    import time

    if _prom is not None:
        _prom["backup_total"].labels(result="success" if success else "failure").inc()
        if success:
            _prom["backup_last_success"].set(timestamp or time.time())
    elif _fallback is not None:
        _fallback.inc(BACKUP_TOTAL, {"result": "success" if success else "failure"})
        if success:
            _fallback.set_gauge(BACKUP_LAST_SUCCESS, {}, timestamp or time.time())


def set_app_info(version: str, env: str) -> None:
    """设置应用信息（启动时调用一次）。"""
    _init_backend()
    if _prom is not None:
        _prom["app_info"].labels(version=version, env=env).set(1)
    elif _fallback is not None:
        _fallback.set_gauge(APP_INFO, {"version": version, "env": env}, 1)


def render() -> Tuple[bytes, str]:
    """输出指标文本。

    Returns:
        (payload_bytes, content_type)。未安装 prometheus_client 时由降级实现生成，
        content_type 仍为 Prometheus 标准文本类型。
    """
    _init_backend()
    if _prom is not None:
        payload: bytes = _prom["generate_latest"](_prom["registry"])
        return payload, _prom["content_type"]
    if _fallback is not None:
        return _fallback.render().encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8"
    # 理论不可达：后端初始化失败时给出空指标而不是 500
    return b"", "text/plain; charset=utf-8"


def backend_name() -> str:
    """返回当前指标后端名称（状态页展示用：prometheus_client / builtin）。"""
    _init_backend()
    return "prometheus_client" if _prom is not None else "builtin"
