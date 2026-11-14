# syntax=docker/dockerfile:1
# 双实例 Gateway HA 镜像：与 deploy/gateway/Dockerfile 相同，额外安装 PostgreSQL 驱动。
# 构建上下文必须是仓库根目录：
#   docker build -f deploy/gateway/ha/Dockerfile -t erp-gateway-ha:$(git rev-parse --short HEAD) .
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.135.2 \
    "uvicorn[standard]==0.42.0" \
    cryptography==50.0.1 \
    "psycopg[binary]>=3.2,<4"

COPY gateway/gateway_app.py /app/gateway_app.py
COPY backend/app/__init__.py /app/app/__init__.py
COPY backend/app/modules/__init__.py /app/app/modules/__init__.py
COPY backend/app/modules/gateway /app/app/modules/gateway

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8100

RUN useradd --system --home /app --shell /usr/sbin/nologin gateway \
    && chown -R gateway:gateway /app /data
USER gateway

CMD ["uvicorn", "gateway_app:app", "--host", "0.0.0.0", "--port", "8100", "--proxy-headers", "--forwarded-allow-ips", "*"]
