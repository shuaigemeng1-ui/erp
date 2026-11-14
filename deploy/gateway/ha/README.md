# 独立网关双实例 + PostgreSQL HA（迁移与切换手册）

目标：消除 Gateway 单机入口故障点。**两个无状态 Gateway 实例共享一个高可用 PostgreSQL**，
负载均衡后面统一公网 URL；Webhook 事件、店铺路由、实例、channel apps、OAuth state、
refresh lock、deliveries、audit、settings 全部在 PostgreSQL。

- 现有单机 SQLite 网关代码保留为默认后端（`GATEWAY_DATABASE_URL` 留空）；
- 设置 `GATEWAY_DATABASE_URL=postgresql://...` 即切换 PostgreSQL 后端；
- dispatcher 使用 `lease_token/lease_expires_at` + PostgreSQL `FOR UPDATE SKIP LOCKED`，
  两个实例可同时领取，不会重复投递；同店铺默认串行保证事件顺序；
- 重放、严格回执、积压统计沿用 task-41 实现。

## 1. 目录

| 文件 | 用途 |
| --- | --- |
| `Dockerfile` | HA 镜像（额外安装 `psycopg[binary]`） |
| `docker-compose.yml` | 本地/演练栈：PostgreSQL + gateway-a/b + Nginx LB |
| `nginx.conf` | 统一入口，上游双实例，无需 sticky session |
| `.env.example` | 集成环境变量模板（密钥必填） |
| `../../../scripts/gateway_ha_migrate.py` | SQLite → PostgreSQL 迁移、序列修正、数量/状态/事件指纹对账 |
| `backend/tests/unit/test_gateway_postgres_integration.py` | 双实例集成测试（需 `GATEWAY_TEST_POSTGRES_URL`） |

## 2. 生产 PostgreSQL HA 前置

Gateway 只需一个**读写端点**，推荐：

- 托管 PostgreSQL 多可用区（RDS/Aurora/Cloud SQL/阿里云 RDS 高可用）；
- 或 Patroni / CloudNativePG / pgpool 等提供自动 failover 的统一读写地址；
- 不把 PostgreSQL 与 Gateway 放在同一故障域；Gateway 两实例也应跨主机/可用区；
- 要求：连接串带 `sslmode=require`（或等价 TLS）；连接超时/重试由驱动/HA 端点负责；
- 备份：沿用独立数据库备份策略，不要只备份 Gateway 本地卷（PG 模式下本地卷无状态）。

## 3. 构建与本地集成启动

```bash
# 在仓库根目录
docker build -f deploy/gateway/ha/Dockerfile -t erp-gateway-ha:$(git rev-parse --short HEAD) .

cp deploy/gateway/ha/.env.example deploy/gateway/ha/.env
# 填 GATEWAY_SECRET_KEY / GATEWAY_ADMIN_TOKEN / GATEWAY_WEBHOOK_RELAY_SECRET / POSTGRES_PASSWORD
# 注意：GATEWAY_SECRET_KEY 必须与旧 SQLite 网关完全相同，否则 payload/channel secret 无法解密。

docker compose -f deploy/gateway/ha/docker-compose.yml --env-file deploy/gateway/ha/.env up -d --build
curl -fsS http://127.0.0.1:8088/healthz
```

`docker-compose.yml` 里的 PostgreSQL 只用于本地集成/演练。生产把
`GATEWAY_DATABASE_URL` 指向托管 HA 端点，并移除 `postgres` 服务/卷（gateway 无本地状态）。

## 4. SQLite → PostgreSQL 切换（维护窗口）

### 4.1 切换前

1. Lead 批准维护窗口（建议 **20–40 分钟**：停写 + 预检 + 迁移 + 验证 + LB 切换）；
2. 记录当前旧 Gateway 的 `/healthz` webhook pending/oldest/retry 数字；
3. 停止旧 Gateway **写入入口**：LB/反代摘除或停容器（只停一个实例不够，必须全部旧入口停写）；
4. 保留旧 SQLite 文件（只读备份，用于回滚）；确认 `GATEWAY_SECRET_KEY` 已同步到新实例；
5. 目标 PostgreSQL 为空库（或明确允许幂等重跑）。

### 4.2 预检与迁移

```bash
# 只读预检：源 SQLite 计数/状态/pending + 目标是否已初始化
python scripts/gateway_ha_migrate.py \
  --sqlite /data/gateway.db \
  --postgres 'postgresql://gateway:...@pg-ha:5432/gateway?sslmode=require'

# 正式迁移（必须确认旧写入已停止）
python scripts/gateway_ha_migrate.py \
  --sqlite /data/gateway.db \
  --postgres 'postgresql://gateway:...@pg-ha:5432/gateway?sslmode=require' \
  --apply --confirm STOP-OLD-GATEWAY
```

脚本行为：

- 目标 schema 由 `GatewayStore.init()` 创建（双实例启动时用 PostgreSQL advisory lock 串行化迁移）；
- 复制 10 张表，保留原 ID；BIGSERIAL 序列按 max(id) 修正；settings 采用 upsert；
- 复制后对账：各表行数、webhook 状态分布、pending 数、事件行指纹（id/notification/status/attempts/
  next_attempt/payload_sha256/lease 等）；任一不一致退出码 1，禁止切流量；
- 不写旧 SQLite，不双写；**不会自动双确认/自动切换 DNS**。

### 4.3 启动双实例并切换

```bash
docker compose -f deploy/gateway/ha/docker-compose.yml --env-file deploy/gateway/ha/.env up -d
# 两个实例 healthz 200、webhook.pending 与迁移报告一致
curl -fsS https://gateway.example.com/healthz
```

将公网 `gateway.alienyan.store`（或对应域名）的 Caddy/Nginx 上游从旧单实例切到 HA LB。
TikTok/Shopee 回调 URL 不变（仍为统一公网 URL），无需改回调地址。

### 4.4 验证

- [ ] `gateway-a`、`gateway-b` 均 `/healthz` 200，`webhook.pending/delivered/dead` 与迁移报告一致；
- [ ] 同一通知分别打到两个实例，`gateway_webhook_events` 仅 1 行且 `duplicate_count=1`；
- [ ] 停掉一个实例，另一个继续领取 pending 并投递（已交付事件不重复）；
- [ ] OAuth state 在一个实例创建、另一个实例消费/解析成功且只能一次；
- [ ] refresh lock 跨实例互斥；旧 owner 释放后另一实例可获取；
- [ ] `backup_records`/旧 Gateway 已停写；旧 SQLite 不再有写入（文件 mtime 不变）；
- [ ] 观察 15–30 分钟 pending/oldest/retry/failure_classes，无积压异常。

### 4.5 回滚

1. 停两个新 Gateway 写入入口；
2. 把公网上游切回旧 SQLite Gateway；
3. **禁止双确认**：如果新 Gateway 已接收写入，不能简单回切旧 SQLite（会丢新数据）；
   必须先按业务影响评估、冻结新入口、将新增事件/状态补迁移回 SQLite（或接受前滚），
   由 Lead 决策；
4. 保留 PostgreSQL 数据与旧 SQLite 只读副本作为审计证据。

## 5. 测试

本地无 PostgreSQL/Docker 时可先跑单元测试；有 PG 时：

```bash
cd backend
GATEWAY_TEST_POSTGRES_URL='postgresql://gateway:...@127.0.0.1:5432/gateway_test' \
  python -m pytest tests/unit/test_gateway_postgres_integration.py -q
```

集成测试覆盖：

1. 双实例同时接收同一事件 → 仅入库一次（duplicate_count=1）；
2. 一个实例关闭后另一个领取/投递 pending（不复用已交付事件）；
3. 共享 OAuth state 跨实例消费/解析一次性；refresh lock 跨实例互斥/释放；
4. SQLite→PostgreSQL 迁移脚本执行后 pending 不丢、行数/状态/事件指纹对账通过。

本地 SQLite 回归仍必须全绿：

```bash
cd backend && python -m pytest -q
```

## 6. 当前未完成项（必须如实记录）

- 本机无 PostgreSQL/Docker daemon：**未实际启动 PG、未执行真实双实例网络验证、未构建 HA 镜像**；
  已交付可运行实现、compose/迁移脚本与 opt-in 集成测试，需在有 PG/Docker 的环境执行；
- 生产 PostgreSQL HA 选型（Patroni/CNPG/托管）与故障切换演练未执行；
- 真实切换窗口 DNS/LB 变更需 Lead/运维审批，不在本仓库自动执行；
- task-45 的备份/恢复文档独立进行；切换前仍按现有备份流程做旧 SQLite 与 PG 备份。
