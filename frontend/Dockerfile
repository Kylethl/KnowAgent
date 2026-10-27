# 前端服务镜像（P1-Dep1 Docker Compose）：多阶段构建
# 阶段 1：用 Node 构建 React 产物（dist/）
# 阶段 2：用 nginx:alpine 托管静态产物，并反代 /api 到后端 FastAPI。

# ---------- 阶段 1：构建 ----------
FROM node:20-alpine AS build
WORKDIR /app

# 先复制依赖清单并安装（利用层缓存：源码变化不会触发重装依赖）
COPY package.json ./
RUN npm install

# 复制全部源码后构建（产出 dist/）
COPY . .
RUN npm run build

# ---------- 阶段 2：托管 ----------
FROM nginx:alpine

# 自定义配置：托管 dist + 反代 /api → backend:8000（剥离 /api 前缀）+ SSE 支持
# 主配置 /etc/nginx/nginx.conf 已含 events/http 块并 include /etc/nginx/conf.d/*.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 复制阶段 1 构建出的静态资源
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
# nginx 镜像默认以前台方式运行，无需额外 CMD
