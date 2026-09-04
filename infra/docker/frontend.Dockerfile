# Multi-stage Frontend image for MCPFlow.
# Targets: build → runtime (nginx-unprivileged static) → dev (Vite)

ARG NODE_IMAGE=node:22.14.0-bookworm-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.27.4-alpine

FROM ${NODE_IMAGE} AS build
WORKDIR /frontend
ENV CI=1
RUN corepack enable && corepack prepare pnpm@11.25.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM ${NGINX_IMAGE} AS runtime
COPY infra/docker/frontend-nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /frontend/dist /usr/share/nginx/html
EXPOSE 8080
# Image USER is already non-root (nginx).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=10 \
  CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1

FROM ${NODE_IMAGE} AS dev
WORKDIR /frontend
ENV CI=1 \
    PORT=8080
RUN corepack enable && corepack prepare pnpm@11.25.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
EXPOSE 8080
CMD ["pnpm", "dev", "--host", "0.0.0.0", "--port", "8080"]
