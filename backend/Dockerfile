# 后端服务镜像（P1-Dep1 Docker Compose）
# 作用：将 FastAPI 后端打包为可运行容器，配合 nginx + PostgreSQL 本地一键编排。
# 基础镜像与本地 Anaconda 环境 knowAgent 保持一致：Python 3.11（见《需求清单》0. 总体约定）。

FROM python:3.11-slim

# 环境变量：让容器内的 Python 输出无缓冲、不写字节码缓存，pip 不落本地缓存
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 应用统一放在 /app 工作目录
WORKDIR /app

# 1) 先复制依赖清单并安装（利用 Docker 层缓存：依赖不变则无需重装）
#    P5 起 requirements.txt 含 imageio-ffmpeg：镜像内自带静态 ffmpeg，
#    用于视频提取音轨与长音频切片，无需在镜像里 apt 安装数百 MB 的系统 ffmpeg。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) 复制应用源码与建表脚本
#    - app/：FastAPI 应用包（含 api / services / db / core / config 等）
#    - sql/ ：init_db 按文件名顺序执行的建表脚本（001_p1_db1 / 002_p1_db2）
#    注意：绝不复制 .env（含真实 DeepSeek Key），凭证统一由 Compose 环境变量注入。
COPY app ./app
COPY sql ./sql

# 3) 复制容器启动脚本：等待数据库就绪 → 执行建表（幂等）→ 启动 uvicorn
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# 上传文件本地存储挂载点（STORAGE_BACKEND=local 时使用，详见 services/storage.py）
VOLUME ["/app/data/uploads"]

# 服务监听端口（uvicorn 在入口脚本中绑定 0.0.0.0:8000）
EXPOSE 8000

# 容器启动入口
ENTRYPOINT ["./docker-entrypoint.sh"]
