# ERP 独立集成网关（阶段三基础）

独立于管理中心运行，拥有自己的 SQLite 配置库，不读取中心 MySQL。

## 当前能力

- 健康检查：`GET /healthz`、`GET /api/v1/gateway/health`
- 渠道应用配置：
  - `GET /api/v1/gateway/channel-apps`
  - `GET /api/v1/gateway/channel-apps/{channel}/{environment}`
  - `PUT /api/v1/gateway/channel-apps/{channel}/{environment}`
- 支持 Shopee / TikTok，production / test
- App Secret 使用 `GATEWAY_SECRET_KEY` 派生密钥加密保存
- 管理接口使用 `X-Gateway-Token`，值来自 `GATEWAY_ADMIN_TOKEN`
- 普通查询只返回 `has_secret`/掩码，不回显明文密钥

## 运行

```bash
docker build -t erp-gateway gateway
docker run -d --name erp-gateway \
  -p 8100:8100 \
  -v erp-gateway-data:/data \
  -e GATEWAY_ADMIN_TOKEN='change-me' \
  -e GATEWAY_SECRET_KEY='change-me-too' \
  erp-gateway
```

健康检查：

```bash
curl http://127.0.0.1:8100/healthz
```

写入渠道应用：

```bash
curl -X PUT http://127.0.0.1:8100/api/v1/gateway/channel-apps/shopee/production \
  -H 'X-Gateway-Token: change-me' -H 'Content-Type: application/json' \
  -d '{"app_key":"...","app_secret":"...","region":"sg","updated_by":"ops"}'
```

## 尚未迁移

- OAuth 回调域名与回调处理
- 令牌刷新任务与刷新租约
- 租户节点访问网关的受限鉴权（当前仅管理端 token）
- 从中心 `platform_channel_apps` 迁移现有渠道密钥
- 渠道调用限流与审计

在该服务完成回调与令牌刷新迁移前，生产渠道调用仍由管理中心处理。
