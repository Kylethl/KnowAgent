# 测试全局配置（P7 引入）
# ---------------------------------------------------------------------
# 为什么需要 conftest：
#   P7 给 /chat 加了「限流与配额」，而配额计数会写进**真实 Redis**（若本机起着 Redis）。
#   如果不隔离，反复跑测试会累积当日计数（默认 300 次/日），
#   跑到某个时刻就会突然出现「/chat 返回 429」的假失败——
#   这种「今天能过、明天挂」的测试是最浪费时间的。
#
# 因此在测试进程启动时（早于任何 app 模块导入）固定两个开关：
#   - LLM_RATE_LIMIT_ENABLED=false：默认关闭限流，测试专注业务契约；
#     限流本身的逻辑由 tests/test_p7_rate_limit.py 显式打开开关后单独验证；
#   - AUDIT_ENABLED=false：审计落库会写真实数据库（audit_logs），
#     单元测试不该产生副作用数据；审计逻辑同样在专用测试里用假会话验证。
#
# 说明：这里用**直接赋值**而不是 setdefault。
#   若开发者本机 .env 里恰好开着这些开关，setdefault 会失效并重新引入不确定性。
#   测试必须可重复，宁可覆盖环境变量。

import os

# 必须在 import app.* 之前设置：Settings 是 pydantic-settings，首次实例化后即缓存
os.environ["LLM_RATE_LIMIT_ENABLED"] = "false"
os.environ["AUDIT_ENABLED"] = "false"
# 数据不出域保持 standard：strict 会让未配置白名单的环境直接拒绝出站，
# 而部分测试会走到「构造请求」的边界（如 embedding 参数校验），不应被合规策略干扰
os.environ["DATA_RESIDENCY_MODE"] = "standard"
