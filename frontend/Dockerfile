# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# ERP 前端镜像：node:22-alpine 构建 → nginx:alpine 静态托管
#
# 构建（在 deploy/ 目录执行）：
#   docker compose -f docker-compose.yml build nginx
# 或单独构建：
#   docker build -t ventasync-frontend:local ./frontend
#
# 约定：
# * 产物目录默认 `dist`（Vite 默认输出）。若 frontend/vite.config.ts 修改了
#   `build.outDir`，必须同步修改下方 COPY 路径与 deploy/README.md。
# * 运行时镜像只含静态文件，不包含 node_modules / 源码。
# * api 反向代理、gzip、安全响应头、前端路由回退都在 deploy/nginx/nginx.conf；
#   Compose 会把该文件挂到 /etc/nginx/nginx.conf，因此这里删除镜像自带的
#   default.conf，避免与挂载配置同时监听 80 端口。
# ---------------------------------------------------------------------------
FROM node:22-alpine AS build

# 默认使用国内镜像，海外网络可通过 --build-arg NPM_REGISTRY=https://registry.npmjs.org 覆盖
ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /app

RUN npm config set registry "$NPM_REGISTRY" \
    && npm config set fund false \
    && npm config set audit false

# 依赖层缓存：lockfile 未变化时复用 node_modules 层
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then \
        npm ci --no-audit --no-fund; \
    else \
        npm install --no-audit --no-fund; \
    fi

# 源码层
COPY . .
RUN npm run build


FROM nginx:alpine AS runtime

# 静态产物（Vite 默认 dist）。若构建产物目录变化，改这一行即可。
COPY --from=build /app/dist /usr/share/nginx/html

# 删除默认站点；实际站点配置由 Compose 挂载 deploy/nginx/nginx.conf
RUN rm -f /etc/nginx/conf.d/default.conf

# nginx 官方镜像默认以 root 启动 master 并降权 worker，无需额外用户
EXPOSE 80

STOPSIGNAL SIGQUIT

CMD ["nginx", "-g", "daemon off;"]
