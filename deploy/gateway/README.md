# 独立集成网关部署包

网关只负责渠道应用配置、OAuth 回调、令牌刷新互斥、限流与幂等交付；不承载租户业务数据。

## 1. 主机要求

首期约 100 个租户：

- 2 vCPU
- 4 GB RAM
- 40 GB 系统盘 + 30–50 GB `/data` 持久盘
- 独立备份盘/NFS/异机目录（建议 30–50 GB，用于加密 SQLite 快照与恢复演练；禁止与 `/data` 同盘）
- Ubuntu 24.04 LTS
- Docker Engine + Compose v2
- 公网域名（示例 `gateway.example.com`）和 HTTPS 证书
- 只开放 443；SSH 限制来源；`8100` 只监听 `127.0.0.1`

网关不需要 MySQL、Redis 或业务数据库。

## 2. 目录

```text
/opt/erp-gateway/
  docker-compose.yml
  docker-compose.backup.yml   # 可选备份 sidecar（每 15 分钟）
  backup.env                  # 备份目录/密钥目录/频率/保留（600）
  .env
```

`docker-compose.yml` 从仓库的 `deploy/gateway/` 复制。构建上下文是仓库根目录，因此需要把仓库克隆到网关主机：

```bash
git clone <repo> /opt/erp
cd /opt/erp
```

## 3. 配置

```bash
cd /opt/erp/deploy/gateway
cp .env.example /opt/erp-gateway/.env
chmod 600 /opt/erp-gateway/.env
```

修改：

```ini
GATEWAY_PUBLIC_URL=https://gateway.example.com
GATEWAY_SECRET_KEY=<openssl rand -base64 48>
GATEWAY_ADMIN_TOKEN=<平台专用随机令牌>
```

`GATEWAY_SECRET_KEY` 只保存在网关主机；不要复制到租户或管理中心。`/data` 保存 SQLite，必须持久化并备份。

## 4. 启动

```bash
cd /opt/erp
docker compose -f deploy/gateway/docker-compose.yml --env-file /opt/erp-gateway/.env up -d --build
curl -fsS http://127.0.0.1:8100/healthz
```

将 `deploy/gateway/nginx.conf.example` 按实际域名/证书修改后放入宿主机 Nginx，重载：

```bash
nginx -t && systemctl reload nginx
```

## 5. 登记租户实例

平台管理员调用（令牌只放平台侧）：

```bash
curl -fsS -X POST https://gateway.example.com/api/v1/gateway/admin/instances \
  -H "Authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": 3,
    "instance_id": 1,
    "channels": ["shopee", "tiktok"],
    "environments": ["production"],
    "store_ids": [],
    "callback_url": "https://zuhu.alienyan.store/api/v1/integrations/gateway/oauth/callback"
  }'
```

保存返回的实例令牌；租户 `.env` 只配置：

```ini
GATEWAY_CHANNEL_CONFIG=true
GATEWAY_INSTANCE_ID=...
GATEWAY_INSTANCE_TOKEN=...
GATEWAY_PUBLIC_URL=https://gateway.example.com
```

先保持 `GATEWAY_OAUTH_RELAY_ENABLED=false`、`GATEWAY_REFRESH_LOCK_ENABLED=false`，验证实例 `/instance/me` 和 `/instance/channel-apps` 后再逐项开启。

## 6. 平台回调登记

- TikTok 应用 Redirect URI：`https://gateway.example.com/api/v1/gateway/oauth/tiktok/callback`
- Shopee 回调：`https://gateway.example.com/api/v1/gateway/oauth/shopee/callback`

回调由网关校验一次性 state 后转发到实例 `callback_url`。

## 7. 迁移与回滚

1. 网关登记实例。
2. 租户切到实例令牌，验证 `/instance/channel-apps`。
3. 平台管理员 `POST /admin/instances/{id}/verify`。
4. 全部 active 实例验证后，`POST /admin/migration/switch {"revoke_legacy":true}` 才撤销旧管理员令牌路径。
5. 回滚：`POST /admin/migration/switch {"legacy_tenant_admin_enabled":true,"legacy_oauth_fallback_enabled":true}`。

任何时候都要保留旧链路可用，直到新链验证完成。

## 8. 备份与恢复（SQLite 在线快照，禁止直接 cp/tar WAL）

网关 SQLite 使用 WAL 模式，直接 `cp`/`tar` 数据卷会得到不一致副本。使用仓库脚本
`scripts/gateway_backup.py`（SQLite 在线备份 API + 加密 + manifest + 恢复演练）：

```bash
# 1) 独立密钥目录（权限 700，密钥 600；必须与备份分开保存）
install -d -m 700 /opt/erp-gateway/keys
python3 /opt/erp/scripts/gateway_backup.py snapshot \
  --key-file /opt/erp-gateway/keys/backup.key --init-key

# 2) 手动快照（sidecar 未启用时；实际生产建议用备份 sidecar）
python3 /opt/erp/scripts/gateway_backup.py snapshot \
  --source /data/gateway.db \
  --backup-root /opt/erp-gateway/backups \
  --key-file /opt/erp-gateway/keys/backup.key \
  --offsite local --offsite-dir /opt/erp-gateway/offsite --json

# 3) 隔离恢复演练（只读备份 + 临时目录；不碰生产卷）
python3 /opt/erp/scripts/gateway_backup.py restore-drill \
  --backup /opt/erp-gateway/backups/gateway-*.db.enc \
  --key-file /opt/erp-gateway/keys/backup.key \
  --work-dir /tmp/gateway-drill --report-dir /tmp/gateway-drill-reports --json

# 4) 只读状态 / 磁盘预算 / 事件清理（默认 dry-run）
python3 /opt/erp/scripts/gateway_backup.py status \
  --database /data/gateway.db --backup-root /opt/erp-gateway/backups --json
python3 /opt/erp/scripts/gateway_backup.py retention \
  --database /data/gateway.db --json
```

15 分钟自动快照（可选 sidecar，只读挂载数据卷，不重启主网关）：

```bash
cp deploy/gateway/backup.env.example /opt/erp-gateway/backup.env
chmod 600 /opt/erp-gateway/backup.env
# 实际卷名如 erp-gateway-v2-data：在 backup.env 设置 GATEWAY_DATA_VOLUME
docker compose -f deploy/gateway/docker-compose.yml \
  -f deploy/gateway/docker-compose.backup.yml \
  --env-file /opt/erp-gateway/.env --env-file /opt/erp-gateway/backup.env \
  --profile backup config -q
docker compose -f deploy/gateway/docker-compose.yml \
  -f deploy/gateway/docker-compose.backup.yml \
  --env-file /opt/erp-gateway/.env --env-file /opt/erp-gateway/backup.env \
  --profile backup up -d --no-deps gateway-backup
```

保留策略：已完成事件原文 7 天、去重行 30 天、未完成事件保留到处理结束；
`retention --apply` 必须在维护窗口、先备份、先 dry-run 后执行。
快照保留：最近 24h 全部 + 30 天每日一份，磁盘预算见
`docs/gateway-backup.md`。同时把 `GATEWAY_SECRET_KEY` 和备份加密密钥
`backup.key` 分别保存到独立密钥管理系统；密钥丢失将无法恢复渠道凭据/备份。

## 9. 安全

- 网关容器非 root 运行
- 不对公网开放 8100；只通过 Nginx 443
- 租户只持有实例令牌
- 平台管理员令牌只在平台侧
- `/data` 目录权限 700，`.env` 权限 600
- 不在日志中输出渠道 secret/令牌
