# KnowAgent · 知识库问答网站

一个基于 **RAG（检索增强生成）** 的企业级知识库问答系统。用户上传文档构建共享知识库，系统通过向量检索 + 大模型生成带引用来源的答案；支持多模态文档（图片 OCR / 音视频 ASR）、管理员审核入库、账号体系、反馈评估与生产级监控运维。

> 全栈自研：前端 React + Vite + TypeScript + Ant Design，后端 FastAPI + LangChain + PostgreSQL(pgvector) + Redis + Celery。

---

## ✨ 核心特性

| 能力 | 说明 |
|------|------|
| 💬 流式对话 | SSE 打字机流式输出，支持多轮上下文 |
| 📚 知识库问答 | 上传 Word / Markdown / 图片 / 音频 / 视频，向量化入库后参与检索 |
| 🔗 答案引用 | 回答下方展示引用文档与片段，可追溯来源 |
| 👥 账号体系 | 注册 / 登录（JWT），会话按用户隔离 |
| 🔍 管理员审核 | 文档提交 → 管理员审核 → 通过后自动入库 / 驳回 |
| 🖼️ 多模态 | 图片 OCR（qwen3.5-ocr）、音视频 ASR（qwen3.8-omni-flash）后归一化为文本入库 |
| 📢 运营闭环 | 审核结果站内信通知、答案点赞/点踩反馈与评估 |
| 📊 生产加固 | 结构化日志、Sentry、LLM 限流配额、Prometheus/Grafana 监控、数据库定时备份、审计日志、数据不出域 |

## 🧱 技术栈

| 层 | 技术 |
|----|------|
| 前端 | React 18 · TypeScript · Vite · Ant Design 5 · React Router |
| 后端 | Python 3.11 · FastAPI · Pydantic v2 · SQLAlchemy 2（async） |
| AI Agent | LangChain · DeepSeek（对话生成） |
| 向量检索 | PostgreSQL + pgvector · qwen3.7-text-embedding-flash（1024 维） |
| 多模态 | qwen3.5-ocr（图片）· qwen3.8-omni-flash（音视频）· ffmpeg |
| 存储 | PostgreSQL（主存+向量）· Redis（缓存/队列）· 本地对象存储 / MinIO |
| 异步任务 | Celery + Redis（文档入库 / 媒体转写） |
| 部署 | Docker Compose · nginx · Prometheus · Grafana · Kubernetes |

## 🏗️ 系统架构

```
用户浏览器 ──HTTPS──▶ Nginx（前端静态 + 反代 /api）
                        │
                        ▼
                  Backend（FastAPI）
             ├─ 流式对话（SSE + RAG 检索增强）
             ├─ 文档上传 → 审核 → 异步入库
             ├─ JWT 鉴权 / 会话隔离
             ├─ LLM 限流配额 / 审计日志 / 数据不出域
             └─ /metrics（Prometheus）
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   PostgreSQL        Redis          Celery Worker
   (主存+pgvector)  (缓存/队列)   (文档入库/媒体转写)
```

## 📁 项目结构

```
KnowAgent/
├── backend/            # FastAPI 后端
│   ├── app/
│   │   ├── api/        # 路由（chat/documents/admin/ops/...）
│   │   ├── core/       # 日志/鉴权/限流/指标/数据不出域等基础设施
│   │   ├── db/         # ORM 模型 + 引擎
│   │   ├── services/   # 业务逻辑（检索/入库/审计/备份/...）
│   │   └── tasks/      # Celery 任务
│   ├── sql/            # 分阶段建表 DDL（001~009）
│   ├── scripts/        # 管理员创建 / 备份 / 恢复脚本
│   ├── tests/          # pytest 测试
│   └── requirements.txt / environment.yml
├── frontend/           # React 前端（Vite）
│   └── src/
│       ├── pages/      # 对话页 / 登录页 / 管理后台 / 系统状态页
│       ├── components/ # 组件（消息气泡/上传/审计/备份面板等）
│       └── services/   # API 封装
├── docker/             # Docker Compose 编排（开发/生产/监控/备份/HTTPS）
├── k8s/                # Kubernetes 部署清单（含网络隔离）
└── 云服务器部署指南.md  # 云服务器完整部署步骤
```

---

## 🚀 快速开始（本地开发）

### 前置依赖

- Python 3.11（推荐 Anaconda / miniconda）
- Node.js 18+
- Docker（用于起 PostgreSQL / Redis）

### 1. 启动依赖（PostgreSQL + Redis）

```bash
cd docker
docker compose -f docker-compose.dev.yml up -d
```

### 2. 配置后端

```bash
cd backend
# 创建并激活 conda 环境
conda create -n knowAgent python=3.11
conda activate knowAgent

# 安装依赖
pip install -r requirements.txt

# 配置环境变量（填入真实 Key）
cp .env.example .env
vi .env    # 填入 DEEPSEEK_API_KEY / QWEN_API_KEY 等
```

### 3. 初始化数据库与管理员

```bash
cd backend
# 建表（含 pgvector 扩展与全部 DDL）
python -m app.db.init_db

# 创建管理员账号
python scripts/create_admin.py --username admin --password '你的密码' --promote
```

### 4. 启动后端

```bash
cd backend
uvicorn app.main:app --reload --port 8000
```

### 5. 启动前端

```bash
cd frontend
npm install
npm run dev    # http://localhost:5173
```

打开浏览器访问 `http://localhost:5173`，登录后即可对话与上传文档。

> 若需完整入库流程（审核 → 向量化），请确保 Redis 已启动并运行 Celery worker（见下文「异步任务」）。

### 启动 Celery Worker（异步入库必需）

```bash
cd backend
# 文档入库 worker
celery -A app.tasks.celery_app:celery_app worker -l info -Q celery -c 2
# 媒体 worker（OCR/ASR）
celery -A app.tasks.celery_app:celery_app worker -l info -Q media -c 1
```

---

## 🐳 Docker Compose 一键部署

```bash
cd docker
cp .env.example .env
vi .env    # 填入 DEEPSEEK_API_KEY / QWEN_API_KEY / JWT_SECRET_KEY 等

# 主服务（nginx + backend + worker + media-worker + postgres + redis）
docker compose -f docker-compose.yml up -d --build

# 监控（Prometheus + Grafana）
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d

# 定时备份（每日 03:00 UTC 的 pg_dump 容器）
docker compose -f docker-compose.yml -f docker-compose.backup.yml up -d
```

首次部署需初始化数据库与管理员（在 backend 容器内或本地执行）：

```bash
docker compose exec backend python -m app.db.init_db
docker compose exec backend python scripts/create_admin.py --username admin --password '你的密码' --promote
```

### HTTPS 上线（Let's Encrypt）

```bash
# 1) 域名解析到服务器公网 IP
# 2) 首次签发证书
docker compose -f docker-compose.yml -f docker-compose.https.yml run --rm certbot \
  certbot certonly --webroot -w /var/www/certbot \
  --cert-name knowagent -d your.domain.com \
  --email you@example.com --agree-tos --no-eff-email
# 3) 启动 HTTPS 全站
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --build
```

---

## ☁️ 云服务器部署

完整的云服务器（ECS / VPS）部署步骤、安全组配置、合规私有化落地、
生产上线检查清单，请参阅 **[《云服务器部署指南》](./云服务器部署指南.md)**。

包含：
- 服务器规格建议与安全组规则
- 容器化部署（推荐）与裸机部署两种方式
- Kubernetes 部署（`k8s/` 清单 + 网络隔离）
- 数据不出域 / 模型本地化 / 审计合规配置

---

## ⚙️ 配置说明

核心配置集中在 `backend/.env`（本地）或 `docker/.env`（容器），关键项：

| 变量 | 说明 | 默认 |
|------|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek 对话模型 Key | 空（未配置时 /chat 返回提示） |
| `DEEPSEEK_MODEL` | 对话模型 | `deepseek-flash` |
| `QWEN_API_KEY` | 千问向量/多模态 Key | 空 |
| `QWEN_EMBEDDING_MODEL` | 知识库向量模型 | `qwen3.7-text-embedding-flash` |
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+asyncpg://knowagent:knowagent@localhost:5432/knowagent` |
| `JWT_SECRET_KEY` | 登录令牌签名密钥（生产必填随机值） | — |
| `LLM_RATE_LIMIT_*` | LLM 限流（分钟/日/token 三级） | 20/分 · 300/日 · 200000 token/日 |
| `DATA_RESIDENCY_MODE` | 数据不出域：`standard` / `strict` | `standard` |
| `SENTRY_DSN` | Sentry 错误上报（留空关闭） | 空 |
| `BACKUP_*` | 数据库备份开关/保留策略 | 见 `.env.example` |

完整配置项见 `backend/.env.example`。

---

## 🧪 测试

```bash
# 后端（394+ 测试）
cd backend
pytest -q

# 前端
cd frontend
npm run test      # vitest
npx tsc --noEmit  # 类型检查
```

---

## 🗺️ 开发路线（已全部完成）

| 阶段 | 功能 | 状态 |
|------|------|------|
| P1 地基 | 流式对话 + 文档提交 | ✅ |
| P2 账号 | 对话历史 + JWT 鉴权 | ✅ |
| P3 审核入库 | 管理员审核 + RAG 管线 | ✅ |
| P4 RAG 可见 | 检索增强回答 + 引用来源 | ✅ |
| P5 多模态 | 图/音/视提交 + OCR/ASR 入库 | ✅ |
| P6 运营闭环 | 审核通知 + 反馈评估 | ✅ |
| P7 生产加固 | 监控/日志/备份 + 合规私有化 | ✅ |

详细需求拆解见 [`知识库网站需求清单与技术栈.md`](./知识库网站需求清单与技术栈.md) 与 [`开发任务清单.md`](./开发任务清单.md)。

---

## 📄 许可证

本项目为个人/学习用途项目，暂未指定开源许可证。如需使用请自行评估。

---

## 🙋 致谢

- [DeepSeek](https://www.deepseek.com) — 对话生成大模型
- [阿里云百炼 / 通义千问](https://bailian.aliyun.com) — 向量与多模态模型
- [FastAPI](https://fastapi.tiangolo.com) · [LangChain](https://www.langchain.com) · [Ant Design](https://ant.design) · [pgvector](https://github.com/pgvector/pgvector)
