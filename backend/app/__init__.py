# KnowAgent 后端应用包（FastAPI）
# 说明：本包承载 P1 起的全部后端能力，按分层组织：
#   app/config.py     —— 配置管理（环境变量 / .env）
#   app/main.py       —— FastAPI 应用骨架、CORS、路由注册
#   app/api/          —— HTTP 路由层（SSE 流式接口）
#   app/services/     —— 外部依赖服务（DeepSeek 调用、会话记忆）
#   app/core/         —— 与业务无关的基础设施（SSE 编码、日志）
__version__ = "0.1.0"
