# 独立集成网关镜像。
# 构建（在仓库根目录，需要把后端 gateway 包一起复制进镜像）：
#   docker build -f gateway/Dockerfile -t erp-gateway .
# 运行：
#   docker run -d --name erp-gateway -p 8100:8100 -v erp-gateway-data:/data \
#     -e GATEWAY_SECRET_KEY=... -e GATEWAY_ADMIN_TOKEN=... erp-gateway
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.135.2 \
    uvicorn[standard]==0.42.0 \
    cryptography==50.0.1

# 启动器 + 自包含网关后端包（不复制管理中心业务代码）
COPY gateway/gateway_app.py /app/gateway_app.py
COPY backend/app/__init__.py /app/app/__init__.py
COPY backend/app/modules/__init__.py /app/app/modules/__init__.py
COPY backend/app/modules/gateway /app/app/modules/gateway

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8100

# 非 root 运行（数据目录属主由卷/编排保证可写）
RUN useradd --system --home /app --shell /usr/sbin/nologin gateway \
    && chown -R gateway:gateway /app /data
USER gateway

CMD ["uvicorn", "gateway_app:app", "--host", "0.0.0.0", "--port", "8100"]
