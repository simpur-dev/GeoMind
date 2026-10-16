# GeoMind 演示壳镜像（架构 §9.5 末句：compose 用 profiles 可选启用）
#
# 两阶段：node 构建 dist → nginx 静态托管 + /api 反代宿主机后端。
# key 合规（§9.7 + 总纲铁律）：`.dockerignore` 已排除 `.env`，高德 JS key 只从 build arg 进，
# 既不入库也不留在镜像层；不传 arg 就构建出「SVG 降级档」——两档本来就是交付面（§9.4）。

# 基础镜像走 daocloud 加速前缀（2026-10-16 验收裁决 ③）：registry-1.docker.io 本机
# 直连超时而不走代理（用户决定弃用代理），daocloud 拉取的是同一官方镜像分层。
# 供应链口径与 npm 的 npmmirror 一致；若直连恢复可改回官方裸名。
FROM docker.m.daocloud.io/library/node:22-alpine AS build
WORKDIR /app

ARG VITE_AMAP_KEY=""
ENV VITE_AMAP_KEY=${VITE_AMAP_KEY}

# 依赖层单独缓存：lockfile 不变就不重跑 npm ci
COPY package.json package-lock.json .npmrc ./
RUN npm ci

COPY tsconfig.json tsconfig.app.json tsconfig.node.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build          # = vue-tsc --noEmit -p tsconfig.app.json + vite build

FROM docker.m.daocloud.io/library/nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
