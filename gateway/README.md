# ERP 独立集成网关（批次4：实例身份与 OAuth 迁移）

网关运行在管理中心之外，拥有自己的 SQLite 持久化配置/状态，不读取管理中心 MySQL/Redis，
也不依赖中心 Worker/scheduler。共享主密钥与平台 admin token 只存在于网关进程；租户实例只持有
一次性发放的实例令牌（数据库只存 HMAC 摘要）。

## 架构与数据

```text
租户实例 (GATEWAY_INSTANCE_ID/TOKEN) ──受限接口──▶ 网关 (SQLite)
平台管理员 (X-Gateway-Token)        ──管理接口──▶ 网关
渠道 OAuth 回调 (一次性 state)      ─────────────▶ 网关 → 按实例 callback_url 转发
```

SQLite 表：`gateway_instances`（实例/白名单/令牌摘要）、`gateway_channel_apps`（渠道密钥加密）、
`gateway_oauth_states`（一次性 state）、`gateway_refresh_locks`（刷新互斥租约）、
`gateway_deliveries`（幂等交付/重试）、`gateway_rate_limits`（限流窗口）、
`gateway_settings`（迁移/回滚开关）、`gateway_audit_logs`（审计）。

## 运行

```bash
# 仓库根目录构建（镜像需要 backend gateway 包）
docker build -f gateway/Dockerfile -t erp-gateway .
docker run -d --name erp-gateway -p 8100:8100 \
  -v erp-gateway-data:/data \
  -e GATEWAY_SECRET_KEY='<32+ 随机字符，仅网关持有>' \
  -e GATEWAY_ADMIN_TOKEN='<平台管理令牌>' \
  erp-gateway

curl -fsS http://127.0.0.1:8100/healthz
```

本地源码运行（在 `gateway/`）：`GATEWAY_DB_PATH=/tmp/gateway.db GATEWAY_SECRET_KEY=... uvicorn gateway_app:app --port 8100`。

## 环境变量

| 变量 | 说明 |
|---|---|
| `GATEWAY_DB_PATH` | SQLite 路径，默认 `/data/gateway.db` |
| `GATEWAY_SECRET_KEY` | 网关主密钥（令牌摘要 + 渠道密钥加密），必填，禁止泄露到租户 |
| `GATEWAY_ADMIN_TOKEN` | 平台管理令牌（平台侧保留，租户不得持有） |
| `GATEWAY_CHANNELS` / `GATEWAY_ENVIRONMENTS` | 允许渠道/环境，默认 `shopee,tiktok` / `production,test` |
| `GATEWAY_INSTANCE_AUTH_ENABLED` | 是否启用实例令牌新链，默认 `true` |
| `GATEWAY_LEGACY_TENANT_ADMIN_ENABLED` | 是否允许租户用旧 admin token 取渠道配置，默认 `true`（迁移期） |
| `GATEWAY_LEGACY_OAUTH_FALLBACK_ENABLED` | 是否保留无 state 的固定 OAuth 回调，默认 `true`（迁移期） |
| `GATEWAY_OAUTH_STATE_TTL_SECONDS` | 一次性 state 有效期，默认 600 |
| `GATEWAY_REFRESH_LOCK_TTL_SECONDS` | 刷新锁租约，默认 120 |
| `GATEWAY_RATE_LIMIT_PER_MINUTE` | 实例+路径每分钟限流，默认 120 |
| `GATEWAY_DELIVERY_MAX_ATTEMPTS` | 幂等交付最大尝试次数，默认 5 |
| `GATEWAY_DELIVERY_RETRY_BACKOFF_SECONDS` | 重试间隔，默认 30 |
| `GATEWAY_VERIFY_WINDOW_SECONDS` | 新链验证窗口，默认 600 |

迁移开关持久化在 `gateway_settings`，重启不丢失；环境变量只作为首次初始化默认值。

## 接口

平台管理（`X-Gateway-Token`）：

```text
GET  /api/v1/gateway/admin/instances
POST /api/v1/gateway/admin/instances                  # 返回一次性 instance_token
GET  /api/v1/gateway/admin/instances/{id}
PATCH /api/v1/gateway/admin/instances/{id}
POST /api/v1/gateway/admin/instances/{id}/rotate-token
POST /api/v1/gateway/admin/instances/{id}/verify      # 新链验证（需实例近期真实调用，force 仅演练）
GET  /api/v1/gateway/admin/migration
POST /api/v1/gateway/admin/migration/switch
GET  /api/v1/gateway/admin/audit-logs
GET/PUT /api/v1/gateway/channel-apps[/{channel}/{environment}]
```

租户实例（`Authorization: Bearer <instance_token>` 或 `X-Gateway-Instance-Token`）：

```text
GET  /api/v1/gateway/instance/me
GET  /api/v1/gateway/instance/channel-apps/{channel}/{environment}
POST /api/v1/gateway/instance/oauth/states            # 一次性 state，绑定实例/租户/店铺/渠道/环境
POST /api/v1/gateway/instance/refresh-locks           # 刷新互斥 acquire
POST /api/v1/gateway/instance/refresh-locks/{key}/renew
POST /api/v1/gateway/instance/refresh-locks/{key}/release
POST /api/v1/gateway/instance/deliveries              # 必须带 Idempotency-Key
GET  /api/v1/gateway/instance/deliveries/{id}
POST /api/v1/gateway/instance/deliveries/{id}/retry
```

公共 OAuth 回调（无用户会话）：`GET /api/v1/gateway/oauth/{channel}/callback?state=...&code=...`
—— 校验一次性 state（重放 410/过期 410/渠道不符 400），成功后 302 到实例 callback_url。

迁移期兼容：`GET /api/v1/gateway/channel-apps/.../internal` 仍接受 admin token，但受
`legacy_tenant_admin_enabled` 控制；租户侧应改用实例令牌访问 `/instance/channel-apps`。

## 迁移与回滚（新链验证通过后再撤销旧令牌）

1. 平台在网关登记实例（channels/environments/store_ids/callback_url），把一次性 instance_token
   写入该实例环境变量 `GATEWAY_INSTANCE_ID` / `GATEWAY_INSTANCE_TOKEN`，开启
   `GATEWAY_INSTANCE_AUTH_ENABLED=true`。
2. 实例调用 `/instance/me`、`/instance/channel-apps/...` 完成真实新链调用（网关记录 last_seen）。
3. 平台 `POST /admin/instances/{id}/verify`（或 force，仅离线演练）标记验证通过。
4. 全部 active 实例验证后：`POST /admin/migration/switch {"revoke_legacy": true}` →
   legacy 租户 admin token 与固定 OAuth 回调关闭；平台 admin API 不受影响。
5. 回滚：`POST /admin/migration/switch {"legacy_tenant_admin_enabled": true,
   "legacy_oauth_fallback_enabled": true}` 立即恢复旧路径（不需要重启网关）。

> 生产变更要求：先离线/预发验证，再切流量；不要在演练或测试中修改生产密钥。

## 租户 channels 接入（OAuth relay / 刷新锁，默认关闭）

租户实例（channels）通过以下环境变量逐步切换，网关侧无需改配置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_OAUTH_RELAY_ENABLED` | `false` | OAuth 发起走 `/instance/oauth/states`，回调由网关校验后转实例 callback_url |
| `GATEWAY_OAUTH_RELAY_LEGACY_FALLBACK_ENABLED` | `true` | 网关 state 解析失败时回退租户本地签名 state（回滚用） |
| `GATEWAY_REFRESH_LOCK_ENABLED` | `false` | token 刷新使用网关 `/instance/refresh-locks` 互斥（409 视为其他执行者刷新中） |
| `GATEWAY_REFRESH_LOCK_LEGACY_FALLBACK_ENABLED` | `true` | 网关锁不可用时回退本地 Redis 锁 |
| `GATEWAY_PUBLIC_URL` | `GATEWAY_BASE_URL` | 浏览器可达的网关地址，用于拼接 `.../api/v1/gateway/oauth/{channel}/callback` |

启用 relay 时，平台需把该实例的 gateway `callback_url` 配为租户统一入口：
`https://<tenant>/api/v1/integrations/gateway/oauth/callback`；TikTok 应用后台的
Redirect URI、Shopee 回调地址均指向网关 `GATEWAY_PUBLIC_URL` 下的 OAuth callback。
回调落到租户统一入口后按 `gateway_channel` 分发到 Shopee/TikTok 原回调逻辑。

## 测试

```bash
cd backend
python -m pytest tests/unit/test_gateway.py tests/unit/test_channels_gateway_relay.py -q
```

覆盖双实例授权回调/凭据隔离、state 重放/过期、刷新锁并发、限流、幂等重试、迁移开关、
SQLite 持久化与网关模块无中心依赖。
