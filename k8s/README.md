# Kubernetes 部署清单（P7-Dep2：私有化 / 云原生部署）
# ---------------------------------------------------------------------
# 目录结构（按资源类型组织，便于 kubectl apply -f 批量部署）：
#   k8s/
#     namespace.yaml          命名空间 + 默认网络隔离
#     network-policy.yaml     网络策略（P7-Dep2 网络隔离）
#     configmap.yaml          非敏感配置（模型名/日志/限流阈值）
#     secret.yaml.example     敏感配置模板（Key/密钥，复制为 secret.yaml 填真实值）
#     postgres.yaml           PostgreSQL + pgvector（单副本 + PVC）
#     redis.yaml              Redis（AOF 持久化 + PVC）
#     backend.yaml            FastAPI Deployment + Service
#     worker.yaml             Celery worker + media-worker（可与 backend 合一）
#     frontend.yaml           nginx 托管前端 + Service（入口）
#     ingress.yaml            对外入口（HTTP/HTTPS）
#     cronjob-backup.yaml     定时备份（pg_dump CronJob）
#
# 部署顺序：
#   kubectl apply -f namespace.yaml
#   kubectl apply -f configmap.yaml
#   cp secret.yaml.example secret.yaml   # 填真实 Key
#   kubectl apply -f secret.yaml
#   kubectl apply -f postgres.yaml redis.yaml
#   kubectl apply -f backend.yaml worker.yaml frontend.yaml
#   kubectl apply -f ingress.yaml cronjob-backup.yaml
#   kubectl apply -f network-policy.yaml
#
# 说明：为「私有化单机/小集群」精简为单副本；生产高可用需另配
#   PostgreSQL 主从、Redis 哨兵、backend 多副本 HPA（见文档 5.3 节）。
