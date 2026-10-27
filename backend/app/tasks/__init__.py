# Celery 任务包（P3-B2）
# ---------------------------------------------------------------------
# 约定：本包下的模块**只在真正需要异步执行时才被导入**，
#       因此 celery 未安装或 Redis 不可达不会影响 Web 主流程（见
#       services/indexing_service.enqueue_index_document 的回退逻辑）。
