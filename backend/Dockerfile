# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# ERP 后端镜像（API / Worker / 调度器 / 迁移发布任务共用）
#
# 构建（在 deploy/ 目录执行）：
#   docker compose -f docker-compose.yml build api
# 或单独构建：
#   docker build -t ventasync-backend:local ./backend
#
# 约定：
# * 基础镜像 python:3.12-slim，运行时非 root（uid/gid 10001）。
# * 只安装 requirements.txt（开发依赖 requirements-dev.txt 不进镜像）。
# * 依赖层与应用层分离，改代码不会重装依赖。
# * 镜像内不含 .env / 密钥；全部配置由 Compose 环境变量注入。
# * 启动命令由 Compose 按服务覆盖（API=uvicorn、Worker=celery、调度器/迁移=python -m）。
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

# 可选 pip 源（中国大陆服务器可传 --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/）
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

# 先复制依赖清单，单独构建 wheel 层，命中缓存时不重复下载
COPY requirements.txt ./
RUN pip wheel --wheel-dir /wheels --index-url "${PIP_INDEX_URL}" \
        ${PIP_TRUSTED_HOST:+--trusted-host "${PIP_TRUSTED_HOST}"} -r requirements.txt


FROM python:3.12-slim AS runtime

# Python 运行时基线：不写字节码、日志立即刷新、时间统一 UTC
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    TZ=UTC

# 非 root 运行账号。uid/gid 固定，便于挂载目录与日志权限管理。
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /wheels \
    && chown -R app:app /app /wheels

WORKDIR /app

# 依赖来自 builder 的 wheel，不访问外网、不编译；镜像内不保留 pip 缓存
COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# 应用代码（按变化频率从低到高复制，最大化层缓存命中）
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts

USER app

# API 默认监听端口；Worker / 调度器 / 迁移任务不监听端口
EXPOSE 8000

# 默认命令为 API；Compose 中 worker / scheduler / migrate 会覆盖
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
