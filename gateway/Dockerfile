FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.135.2 uvicorn[standard]==0.42.0 cryptography==50.0.1
COPY gateway_app.py /app/gateway_app.py
RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8100
CMD ["uvicorn", "gateway_app:app", "--host", "0.0.0.0", "--port", "8100"]
