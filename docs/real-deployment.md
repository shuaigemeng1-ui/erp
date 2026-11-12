# 真实服务器部署记录（2026-11-11）

> 目标：把本仓库部署到 `121.37.45.132`，MySQL 使用 Docker 容器。
> 本文只记录架构、步骤与验证证据，不含任何密码/密钥；服务器上的敏感值保存于
> `/root/ventasync-credentials.txt`（权限 600），请登录后按需修改。
>
> 当前访问地址：**https://erp.alienyan.store/**（Let's Encrypt 证书，Caddy 自动续期；
> `http://121.37.45.132/` 会 301 跳转到该域名）。


## 1. 服务器初始状态

| 项 | 值 |
| --- | --- |
| 系统 | Ubuntu 24.04.4 LTS（内核 6.8，x86_64） |
| 资源 | 2 vCPU / 1.9 GiB RAM / 2 GiB swap / 40 GB 磁盘 |
| 容器运行时 | Docker 29.8.2，Docker Compose v5.5.1 |
| 已有服务 | Caddy（80/443，托管 `shuren.alienyan.store`、`2fa.alienyan.store`）；2fauth 容器（127.0.0.1:8080） |
| 镜像源 | Docker 使用华为云 SWR 镜像加速；PyPI 用阿里云；npm 用 npmmirror |

按用户要求：**关闭除 Caddy 外的应用服务**——停止 `2fauth` 并设置 `--restart=no`（数据与容器保留，
需要时 `docker start 2fauth` 即可恢复），Caddy 保持运行，并只在其 Caddyfile 末尾追加一个
IP 站点反代（原文件已备份为 `Caddyfile.bak-*`，两个已有域名不受影响）。

## 2. 部署架构

```text
公网 :80/:443 ── Caddy（宿主机 systemd，自动 HTTPS + HSTS）
              ├─ https://erp.alienyan.store  -> reverse_proxy 127.0.0.1:8081
              └─ http://121.37.45.132        -> 301 -> https://erp.alienyan.store
                    └─ nginx 容器（前端口 80，绑定 127.0.0.1:8081）
                         ├─ 静态：Vue 构建产物 dist（SPA 回退）
                         └─ /api/ -> api 容器（FastAPI + uvicorn，仅内部网络）

docker compose 项目 ventasync（/opt/ventasync/deploy）：
  mysql:8.4（InnoDB/utf8mb4，命名卷 ventasync_mysql_data，128M buffer pool）
  redis-cache:7（会话/缓存，volatile-lru）
  redis-queue:7（Celery 队列，noeviction + AOF）
  api / worker（celery，concurrency=1）/ scheduler（python -m app.workers.scheduler）
  nginx（frontend/Dockerfile.runtime，使用本地构建的 dist，不在服务器跑 Node）
  migrate（profile=release，独立发布任务，API 启动不迁移）
```

关键环境变量（`/opt/ventasync/deploy/.env`，600 权限）：
`APP_ENV=production`、`APP_ORIGIN=http://121.37.45.132`、`HTTP_BIND=127.0.0.1`、`HTTP_PORT=8081`、
`PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`、`CELERY_CONCURRENCY=1`、
`MYSQL_INNODB_BUFFER_POOL_SIZE=128M`、`MYSQL_MAX_CONNECTIONS=100`、`TENANT_POOL_SIZE=3`。

## 3. 部署步骤（实际执行）

1. 本地打包（排除 `.git`、`node_modules`、缓存；**包含 `frontend/dist`**）→ SFTP 上传 `/opt/ventasync.tar.gz`；
2. 服务器解压到 `/opt/ventasync`，生成 `.env`（强随机 SECRET_KEY、ENCRYPTION_KEY、MySQL/中心库/管理员密码）；
3. `docker compose -f docker-compose.yml -f docker-compose.server.yml up -d --build`；
4. `--profile release run --rm migrate`（中心库 baseline 到 `center_0002`）；
5. `docker compose exec -T api python -m app.cli platform-init`（首个平台管理员，不开放公共注册）；
6. Caddy 追加 IP 站点并 `caddy validate` + `systemctl reload caddy`；
7. 通过 API 完成真实开通验证（见下）。

部署过程中修复的问题（均已回写仓库）：

| 问题 | 修复 |
| --- | --- |
| `backend/.dockerignore` 误排除 `alembic/`，镜像构建 `COPY` 失败 | 移除该排除项 |
| `requirements.txt` 固定了不存在的 `PyMySQL==1.4.6` | 改为 `1.1.2` |
| Celery Redis 传输运行期缺 `packaging` | 显式加入依赖 |
| kombu 5.7 要求 redis-py ≥ 6.1 | `redis` 由 5.3.1 升到 6.4.0 |
| 低配内存与端口占用（base 与 override 端口映射叠加） | 基础编排支持 `HTTP_BIND`，server override 不再重复声明 ports |
| 服务器无法构建 Node（2C2G） | 新增 `frontend/Dockerfile.runtime`，使用本地构建的上传 dist |
| 无邮件通道时平台拿不到负责人邀请链接 | 新增 `POST /platform/tenants/{id}/owner-invitation` 重签接口 + 前端「生成邀请链接」 |
| 中文注释导致 Alembic 读取 ini 失败（GBK locale） | `alembic/*.ini` 改为 ASCII 注释 |

## 4. 实测验证结果（真实 MySQL/Redis/Caddy，非模拟）

| 验证项 | 结果 |
| --- | --- |
| 7 个容器健康 | api / mysql / redis×2 / worker / scheduler / nginx 全部 healthy（nginx 通过 Caddy 公网可达） |
| 公网访问 | `http://121.37.45.132/` 200；`/assets/index-*.js` 200（1.0MB）；SPA 路由 `/products`、`/activate/:token` 200 |
| API 健康 | `/healthz` 200；`/api/v1/auth/csrf` 200 且下发 `vs_csrf` Cookie |
| CSRF / 来源校验 | 无 CSRF Header 的 POST → 403；`Origin: http://evil.example` → 403 `AUTH_ORIGIN_INVALID` |
| 平台管理员 | `platform-init` 成功，登录 200，`is_platform_admin=true`；错误密码 401 |
| 真实租户开通 | 创建 `demo`、`demo2` 两个租户均 `202 async` → Worker 执行 9 步 → `active`；数据库 `vs_tenant_demo`、`vs_tenant_demo2` 各 **29 张表**，迁移版本 `tenant_0004` |
| 邀请流程 | `owner-invitation` 返回一次性 token → 未登录预览 200 → 接受邀请 200 → membership `active`；重复使用同一 token → 422 `AUTH_TOKEN_USED`；已激活后再次签发 → 409 `CONFLICT` |
| 权限 | owner 登录后 `bootstrap` 返回 `roles=["owner"]`、20 项权限；平台管理员成为 demo 租户 owner 后同样可操作 |
| 商品/库存 | owner 创建商品 200；库存 +25 成功、-1000 返回 409 `INVENTORY_NEGATIVE_FORBIDDEN`；库存余额 25、流水 1 条、审计 3 条 |
| 租户隔离 | owner 访问 demo 租户 → 404 `TENANT_NOT_FOUND`；平台管理员（非 demo2 成员）访问 demo2 → 404；两租户商品互不可见 |
| 生命周期 | 暂停租户 → `/me/tenants` 状态 `suspended`、`bootstrap` 403 `TENANT_SUSPENDED`；启用后恢复 `active` |
| 登录限流 | 连续 5 次错误密码后第 6 次 429 `AUTH_RATE_LIMITED` |
| 真实库无外键 | `information_schema.REFERENTIAL_CONSTRAINTS`（中心库 + 全部租户库）**0 条** |
| 中文注释 | 表/字段 COMMENT 全部存在（仅框架表 `alembic_version` 无注释）；`HEX(name)` 校验中文未损坏 |
| 备份脚本 | 真实执行：中心库 5.2KB、租户库 8.7KB 的 `.sql.gz` + `.sha256`，输出目录 `/opt/backups` |
| 迁移发布任务 | `migrate_all.py --list` 正确列出中心库 + 1 个租户库；重复执行幂等（当前版本不重复） |
| Worker 任务 | Celery ping 1 node online；开通任务 Worker 实跑成功（2.9 秒完成 9 步） |
| 资源占用 | 常驻内存约 1.0 GiB / 1.9 GiB，swap 未使用；磁盘 9.5 GiB / 40 GiB |
| 平台审计 | `tenant.create`×2、`tenant.suspend`、`tenant.activate`、`platform.tenant.owner_invitation` 等记录可查 |

测试数据（可清理）：
* 租户 `demo`（负责人 admin@ventasync.local）与 `demo2`（负责人 owner.demo2@example.com）；
* `demo` 内有商品 `SKU-D1-1`；`demo2` 内有商品 `SKU-D2-1`、库存 25、审计 3 条。

## 5. 日常运维命令（服务器 `/opt/ventasync`）

```bash
cd /opt/ventasync/deploy
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env"

$COMPOSE ps                                   # 状态
$COMPOSE logs -f --tail=200 api worker        # 日志
$COMPOSE restart api worker scheduler         # 重启进程
$COMPOSE --profile release run --rm migrate   # 发布迁移（中心库 + 全部租户库）
$COMPOSE exec -T api python -m app.cli platform-init   # 幂等初始化管理员

bash ../deploy/scripts/backup.sh -d ventasync_center -o /opt/backups
bash ../deploy/scripts/backup.sh --all-tenants -o /opt/backups
# 恢复：deploy/scripts/restore.sh -f <文件> -d <库名> --yes（恢复后先对账再开推送）

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy
```

更新版本：本地重新打包上传（含新 `frontend/dist`）→ 解压覆盖 → `$COMPOSE up -d --build` → 迁移。
只改前端时：`$COMPOSE build nginx && $COMPOSE up -d nginx`。

## 6. 当前限制与外部前置条件

1. **Shopee 真实联调未做**：尚无 Shopee 应用（Partner ID/Key、回调地址审核）与已授权测试店铺，
   店铺授权、商品读取、拉单、库存推送的真实链路未验证；平台「渠道应用」仍为未配置状态。
2. **HTTPS 已启用**：`https://erp.alienyan.store`（Let's Encrypt，Caddy 自动续期，HSTS；HTTP 自动 301 跳转）。
   Cookie 已改为 `Secure`，`APP_ORIGIN`/`CORS_ALLOW_ORIGINS` 已切换为 https。
3. **运维身份仍为 root**：`PROVISIONER_DATABASE_URL` 使用 MySQL root（权限仅用于建库/建号/迁移），
   已按用户要求统一 root 密码；P1 应替换为最小权限账号（注意：密码含 `@` 等字符时 URL 需编码）。
4. **备份已定时化**：`/etc/cron.d/ventasync-backup` 每日 03:30 备份中心库 + 全部租户库并保留 14 天；
   单租户恢复演练已完成（见 §8.4）。
5. **已停止 2fauth**：如需恢复：`docker update --restart=unless-stopped 2fauth && docker start 2fauth`
   （占用 8080，与当前应用不冲突）。
6. 测试租户/商品/审计数据仍在库中，正式试点前建议清理或改作演示数据。

## 7. 下一步计划（建议按优先级执行）

> P0 已于 2026-11-11 完成（Shopee 真实店铺联调依赖外部应用审批，仍待用户提供 Partner 凭据）。
> P0 实施与验证细节见 §8；以下保留原始计划，未完成项已在 §8.6/§8.7 标注。

### P0（上线前必须）
1. **改密与密钥**：修改平台管理员密码、MySQL root 密码、中心库账号密码；更新 `deploy/.env` 并重启；
   服务器 root 登录改用 SSH Key，关闭密码登录。
2. **域名 + HTTPS**：给 Caddy 添加域名站点（自动证书），更新 `APP_ORIGIN`/`CORS_ALLOW_ORIGINS`/
   `SHOPEE_AUTH_REDIRECT_URL`，设 `SESSION_COOKIE_SECURE=true`，重启 api/nginx。
3. **Shopee 应用与测试店铺**：在 Shopee 开放平台申请应用 → 平台「渠道应用」填写 Partner ID/Key/
   回调地址 → 授权测试店铺 → 完成「商品读取→映射→拉单→库存扣减→库存推送」真实闭环；
   先关闭自动推送，核对映射后再开启。
4. **备份定时化 + 恢复演练**：配置每日 `backup.sh --all-tenants` cron，保留 14 天；做一次
   单租户恢复演练并核对库存/订单。

### P1（试点前）
5. **最小权限运维账号**：创建仅具 `CREATE/CREATE USER/GRANT` 的 provisioner 账号，替换 root；
   收紧 MySQL 端口（已仅容器网络，无需公网）。
6. **监控与告警**：容器健康、`/readyz`、Celery queue 积压、MySQL 连接数、磁盘；日志接入 Loki/ELK。
7. **计划任务核对**：确认 scheduler 正常轮询 `sync_schedules`、Outbox 投递、Webhook 补偿；
   设置试点租户的定时拉单间隔。
8. **页面验收**：用真实数据走一遍商品/库存/成员/角色/审计页面；确认成本权限、数据范围表现符合预期。

### P2（首版增强）
9. **套餐额度落地**：目前 `plans/plan_features` 已建模但未在成员/店铺/SKU 创建时强制额度，
   需补 `core/quota` 与各模块创建校验（plan §3「额度降低不删除已有数据，仅限制超额新增」）。
10. **文件上传/S3**：商品图片目前用 URL；补 S3 上传接口与前端上传组件。
11. **前端体验**：成员邀请列表/重发按钮、任务详情轮询页、订单库存影响明细展示。
12. **备份/恢复自动化脚本化**：把恢复后对账流程写成脚本（关推送→恢复→对账→开推送）。

### P3（规模化）
13. API/Worker 水平扩容（Nginx 多副本 upstream、Celery 分队列扩缩容）；
14. 接入真实监控与审计告警；多租户连接池与 MySQL 连接数按租户数扩容评估；
15. 安全加固：CSP、HSTS、依赖漏洞扫描、定期轮换店铺凭据。

## 8. P0 上线加固实施与验证（2026-11-11 晚）

### 8.1 域名与 HTTPS

| 项 | 结果 |
| --- | --- |
| 域名 | `erp.alienyan.store` → A 记录 121.37.45.132（已在 223.5.5.5 / 1.1.1.1 / 8.8.8.8 验证） |
| 证书 | Let's Encrypt，`notBefore=2026-11-11`、`notAfter=2027-02-09`，自动续期由 Caddy 负责 |
| 访问 | `https://erp.alienyan.store/` 200、`/healthz` 200、`/api/v1/auth/csrf` 200 |
| 跳转 | `http://121.37.45.132/` → 301 → `https://erp.alienyan.store/` |
| 安全头 | HSTS(max-age=1y, includeSubDomains)、CSP、X-Frame-Options、X-Content-Type-Options、Referrer-Policy |
| Cookie | `vs_session`：HttpOnly + Secure + SameSite=Lax；`vs_csrf`：Secure（JS 可读） |
| 应用配置 | `APP_ORIGIN=https://erp.alienyan.store`、`CORS_ALLOW_ORIGINS` 同域、`SESSION_COOKIE_SECURE=true`、`SHOPEE_AUTH_REDIRECT_URL=https://erp.alienyan.store/api/v1/integrations/shopee/oauth/callback` |
| Caddy 文件 | `/etc/caddy/Caddyfile`（每次修改前自动备份 `Caddyfile.bak-*`）；示例已归档到 `deploy/caddy/Caddyfile.example` |

### 8.2 密码与密钥轮换

| 对象 | 处理 | 验证 |
| --- | --- | --- |
| 平台管理员 | 旧密码 → 新强密码（大小写+数字+符号，长度 20） | 旧密码 401 `AUTH_INVALID_CREDENTIALS`，新密码 200；其它会话被撤销 |
| 服务器 root | 按用户要求统一密码 | SSH 重新登录验证通过 |
| MySQL root（`root@%`、`root@localhost`） | 按用户要求统一密码 | `mysql -uroot` 新密码登录成功；provisioner 建库/删库验证通过 |
| 中心库运行账号 `ventasync` | 轮换为 32 位随机密码 | API 连接中心库、登录、迁移均正常 |
| `SECRET_KEY` | 轮换为 32 字节随机值 | 服务重启后登录/CSRF 正常 |
| `ENCRYPTION_KEY` | **保留未轮换** | 该密钥用于加密租户库密码与渠道凭据；轮换需先重加密 `tenant_databases`，列入后续变更 |
| `PROVISIONER_DATABASE_URL` | 因 root 密码含 `@`，改为显式 URL 并对密码编码（`@`→`%40`） | `provisioner_engine` 创建/删除临时库成功 |

> 敏感值统一保存在服务器 `/root/ventasync-credentials.txt`（600）；仓库中不包含任何真实密码/密钥。

### 8.3 备份定时化

```cron
# /etc/cron.d/ventasync-backup
30 3 * * * root cd /opt/ventasync && bash deploy/scripts/backup.sh --all-tenants -o /opt/backups -k 14 >> /var/log/ventasync-backup.log 2>&1
```

已按 cron 同款命令真实执行一次：中心库 + `vs_tenant_demo` + `vs_tenant_demo2` 均生成 `.sql.gz` 与 `.sha256`，
日志写入 `/var/log/ventasync-backup.log`；cron 服务 active。

### 8.4 单租户恢复演练（真实执行）

步骤与结果：

1. 记录另一租户 `vs_tenant_demo2` 基线：products=1、inventory_balances=1、audit_logs=3；
2. 平台暂停 `demo` 租户（恢复脚本要求非 active）；
3. 用最新 `vs_tenant_demo_*.sql.gz` 执行 `restore.sh -f ... -d vs_tenant_demo --tenant-id 1 --yes`；
4. 恢复后：表数 **29**、Alembic 版本 `tenant_0004`、商品 1 条、`stock_push_enabled=0`、
   `order_sync_enabled=0`（脚本自动关闭推送/同步）；
5. `demo2` 三张表计数保持 **1 / 1 / 3** 不变 → 单租户恢复不影响其他租户；
6. 重新开启推送/同步并启用租户；管理员 `bootstrap` 200、商品列表可读 → 恢复后业务可继续使用。

结论：恢复脚本、sha256 校验、恢复后自动关推送、跨租户隔离均真实通过；正式试点前建议再按业务时段做一次演练。

### 8.5 部署期新增修复

| 问题 | 修复 |
| --- | --- |
| `api` 容器重建后 nginx 仍缓存旧 IP → 502 | nginx 改为 Docker DNS 动态解析（`resolver 127.0.0.11 valid=10s` + 变量 `proxy_pass`） |
| root 密码含 `@` 导致 `PROVISIONER_DATABASE_URL` 解析错误 | Compose 支持显式 `PROVISIONER_DATABASE_URL`；文档要求 URL 编码特殊字符 |
| 切换 HTTPS 后 `Secure` Cookie 在 HTTP 测试环无法写入 | 端到端测试统一走 https 域名；文档记录 |
| Caddy 修改可能影响已有站点 | 每次修改前备份；`caddy validate` 通过后 reload；已有两个域名仍正常 |

### 8.6 P0 完成情况

| P0 项 | 状态 |
| --- | --- |
| 改密与密钥轮换 | **完成**（ENCRYPTION_KEY 因租户库密码加密暂不轮换，已记录） |
| 域名 + HTTPS + 自动续期 | **完成** |
| 备份定时化 + 恢复演练 | **完成**（每日 03:30；单租户真实恢复演练通过） |
| SSH Key / 关闭密码登录 | **未执行**：按用户要求保留 root 密码登录（统一为指定密码）；建议后续单独配置 Key 并关闭密码登录 |
| Shopee 应用与测试店铺联调 | **待外部条件**：需用户提供 Shopee 开放平台应用（Partner ID/Key、回调审核）与测试店铺授权；回调地址已就绪 |

### 8.7 后续立即要做

1. 提供 Shopee 平台应用凭据 → 平台「渠道应用」配置 → 授权测试店铺 → 完成真实订单/库存闭环；
2. 创建最小权限 MySQL provisioner 账号并替换 root（P1），建议同时启用 SSH Key；
3. 配置监控告警与日志采集（P1）；
4. 按 `deploy/README.md` 做一次试点租户页面验收（成本权限、数据范围、任务状态）。

## 9. 域名唯一入口与数据持久化（2026-11-12）

### 9.1 只允许域名访问

| 场景 | 结果 |
| --- | --- |
| `https://erp.alienyan.store/` | 200（正常） |
| `http://121.37.45.132/` | 301 → `https://erp.alienyan.store/` |
| `https://121.37.45.132/` | TLS 握手失败（无 IP 证书，不可用） |
| SNI=erp + `Host: evil.example` | **421 Misdirected Request**（Caddy `strict_sni_host`） |
| 直连容器 `Host: evil.example` | **421**（nginx host allowlist） |
| 直连 API `Host: evil.example`（容器内） | **400**（FastAPI TrustedHostMiddleware） |
| 既有站点 `shuren.alienyan.store` | 200，不受影响（`2fa` 站点因服务停止为 502，符合预期） |

实现：Caddy 全局 `servers { strict_sni_host }` + 域名站点 Host 匹配；nginx `$host` allowlist；
后端 `TRUSTED_HOSTS`（Compose 透传）。换域名需同步改四处配置（见 `deploy/README.md` §12.1）。

### 9.2 MySQL / Redis 持久化实测

| 检查 | 结果 |
| --- | --- |
| MySQL 数据卷 | `ventasync_mysql_data` → `/var/lib/mysql`，宿主路径 `/var/lib/docker/volumes/ventasync_mysql_data/_data` |
| 数据量 | 225MB（中心库 + 2 个租户库） |
| 重启测试 | `docker compose restart mysql` 后行数完全一致：tenants=2、users=3、tenant_databases=2、demo2 products=1、demo products=1 |
| 重启后可用性 | `mysql health=healthy`、`/healthz` 200、`/api/v1/auth/csrf` 200 |
| 队列 Redis | `ventasync_redis_queue_data` → `/data`，AOF=yes、noeviction，重启保留任务 |
| 备份 | `/etc/cron.d/ventasync-backup` 每日 03:30；`/opt/backups` 已有中心库 + 全部租户库备份 |
| 风险提示 | `docker compose down -v` 会删除数据卷；日常只用 `down`（不带 `-v`），并保留每日备份 |

> 登录账号：平台管理员 `admin@ventasync.local`（密码在服务器 `/root/ventasync-credentials.txt`）；
> 演示租户负责人 `owner.demo2@example.com`。生产使用前请按需修改密码。

## 10. TikTok Shop 接入（Phase 1，2026-11-12）

原 `ventasync` 项目的 TikTok Shop 扩展为 PHP/Laravel 实现，新 ERP 按其协议移植了 Phase 1：

| 项 | 说明 |
| --- | --- |
| OAuth | `https://auth.tiktok-shops.com/oauth/authorize`，token 换取/刷新 `/api/v2/token/get`、`/api/v2/token/refresh` |
| Open API | `https://open-api.tiktokglobalshop.com`，HMAC-SHA256 签名（app_secret + path + 排序参数 + body + app_secret） |
| 回调地址 | `https://erp.alienyan.store/api/v1/integrations/tiktok/oauth/callback`（需加入 TikTok 应用白名单） |
| 店铺绑定 | 授权后读取 `/authorization/202309/shops`，保存店铺、加密 access/refresh token 与 `shop_cipher` |
| 平台配置 | 平台管理 → 渠道应用 → TikTok Shop，填写 App Key / App Secret（密文入库、不回显） |
| 迁移 | 中心库 `center_0003`（app_key/app_secret_cipher）；租户库 `tenant_0005`（store_credentials.shop_cipher_cipher） |
| 已验证 | 平台保存配置 200、发起授权返回 auth URL、无效 state 302 回前端错误、两租户迁移到 `tenant_0005` |
| Phase 2 | 订单拉取/落库、库存推送、商品映射；之后再补商品发布、退货、结算 |

> 新 ERP 与旧应用是两套独立系统：旧应用的 TikTok App Key/Secret/Token 存在旧库中，
> 本地代码没有 `.env` 与数据库，无法直接复用；需要在新 ERP 渠道应用中填写 TikTok 应用凭据，
> 或在 TikTok 后台新建应用后重新授权。

## 11. TikTok Shop Phase 2（订单 / 商品 / 库存，2026-11-12）

| 能力 | 实现 |
| --- | --- |
| 商品同步 | `POST /product/202309/products/search` → `channel_listings`，按 `seller_sku` 自动匹配本地 SKU |
| 店铺授权 | 新建店铺（待授权）→ 操作栏复制 `services.tiktokshop.com/open/authorize?service_id=...&state=...` 链接 → 回调绑定该店铺 |
| 订单拉取 | `POST /order/202309/orders/search` → `channels.service.fetch_orders` 渠道分支 → `platform_order_raw` → 归一化订单/明细/库存影响 |
| 订单状态 | UNPAID/ON_HOLD→pending、AWAITING_SHIPMENT/AWAITING_COLLECTION→paid、IN_TRANSIT/DELIVERED→shipped、COMPLETED→completed、CANCELLED→cancelled |
| 库存推送 | `POST /product/202309/products/{id}/inventory/update`，按仓库写入；仓库来自 `/logistics/202309/warehouses`，存 `stores.warehouse_id` |
| Token 刷新 | access token 过期前自动用 refresh token 刷新，失败标记 `reauth_required` |
| 数据隔离 | 复用租户库表与现有同步流水线；订单/库存幂等逻辑不变 |
| 迁移 | 租户库 `tenant_0006`（stores.warehouse_id） |
| 尚未实现 | TikTok Webhook 回调、退货/结算、商品发布（Phase 2 后续） |

> 实际联调需要 TikTok Shop 应用凭据与已授权店铺；当前平台渠道应用为空，
> 配置 App Key/Secret 并完成授权后，定时任务/手动同步即可走 TikTok 分支。

## 12. 多租户安全修复与同步恢复发布（2026-11-12）

业务代码提交 `5f6c729`，前端静态权限修复提交 `ecb2bd3`，均已推送到 `origin/main` 并部署至 `/opt/ventasync`。
本次未修改现有数据库密码、应用密钥或数据卷，未重建 MySQL/Redis 容器。

发布过程：本地 `npm run build` → 服务器构建 API/前端镜像 → 停止 scheduler/API/Worker →
备份中心库与全部租户库 → 独立 migrate 容器升级 → 更新 API/Worker/scheduler/nginx。
停写后记录迁移前关键表行数，发布后进行对比。前端曾因宿主机 umask 导致静态文件不可读，
已修复 `frontend/Dockerfile.runtime`，在镜像构建时统一文件读取与目录访问权限，并重新构建发布。

| 检查 | 实际结果 |
| --- | --- |
| 中心库 | `ventasync_center`：`center_0005` → `center_0006`，15 张业务表 |
| 两个租户库 | `vs_tenant_demo`、`vs_tenant_demo2`：`tenant_0007` → `tenant_0010`，各 29 张业务表 |
| 外键与注释 | 三个业务库外键均为 0；业务表和字段中文注释完整，修复了 `stores.store_code` 旧迁移丢失的注释 |
| 关键业务行数 | 用户、租户、租户映射、成员及现有商品、SKU、店铺、订单、库存余额、库存流水的行数与停写基线一致 |
| API / Worker | 容器内 `/readyz` 返回 200；Celery inspect ping 返回 1 node online |
| 公网 | 首页、`/login`、`/healthz`、`/api/v1/auth/csrf` 均为 200，CSRF Cookie 含 Secure |
| 前端产物 | 公网 index 与仓库 dist 一致，主 JS/CSS 的 SHA-256 与发布产物一致 |
| 数据备份 | 中心库 + 两个租户库的 `.sql.gz` 和 `.sha256` 全部校验通过 |

本次备份与校验记录位于服务器 `/opt/backups/releases/erp-5f6c729/`，数据库备份在其 `database/` 子目录。
同目录保存迁移前后结构与行数记录、公开站点检查结果、发布前 `.env`（权限 600）及前后提交号；这些文件不进入 Git。
旧后端/前端镜像保留为 `ventasync-backend:rollback-5f6c729` 和 `ventasync-frontend:rollback-5f6c729`。
数据库回退应先评估迁移与数据恢复，不能仅凭旧镜像认定回退完成。

升级后所有用户需重新登录；旧永久授权链接需在店铺页面重新生成，已保存的平台凭据保留。
本次没有执行真实平台库存写入，也没有认定 TikTok/Shopee 真实业务闭环已通过；仍按 `acceptance.md` 的独立环境清单继续验收。


## 13. SPU—SKU 商品中心发布（2026-11-12）

业务提交 `4bfe4f7`，迁移修复 `ebd211f`。服务器目录仍为 `/opt/ventasync`，使用基础 Compose 与 server override；新 API 挂载 `ventasync_catalog_media_data`，旧 MySQL/Redis 容器及其数据卷保留。

发布顺序：本地生产构建和 Git 推送 → 构建镜像并保留回退镜像 → 停止 API/scheduler/Worker → 备份中心库及两个租户库 → 独立迁移 → 更新应用容器 → 数据、接口和图片卷检查。

首次迁移在写入审计日志时遇到旧表的非空 `ip/request_id` 无 SQL 默认值，已修复 INSERT 并增加独立 SQL 回归。MySQL 的 DDL 已生效，而后续数据事务已回滚；因此没有直接重跑第一个库的全量 DDL。核对该库全部业务表和列、此次新增索引、空回填表及当前版本后，在迁移锁下重新执行修复后的数据部分，与版本确认同事务提交；然后通过标准 migrate 任务正常升级第二个库。

| 检查 | 结果 |
| --- | --- |
| 版本 | 中心库 center_0006；两个租户库 tenant_0011 |
| 结构 | 中心业务表15张，租户各32张；外键0，表及字段中文注释完整 |
| 数据 | 原有关键商品、SKU、订单、明细、库存和映射字段指纹一致；统一主编号登记核验通过 |
| 商品接口 | 对可用租户身份的列表、详情、SKU搜索、选项和日志 GET 请求均200；临时会话已删除 |
| 公网 | 首页、登录页、healthz、CSRF接口200，主JS/CSS哈希与发布产物一致 |
| 进程 | API/Worker/nginx/MySQL/Redis健康，scheduler运行，Celery ping成功 |
| 图片卷 | UID/GID 10001目录可读写，验证临时文件已删除；图片卷备份及校验通过 |
| 备份 | 发布前三库备份均校验通过；发布后两租户库及图片卷备份均校验通过 |

本次证据与备份目录：`/opt/backups/releases/erp-4bfe4f7/`。`database/` 为发布前备份，`post-publish/` 为发布后租户与图片备份；其他文件包含前后结构、业务指纹、接口结果、恢复脚本和权限600的环境配置备份。

旧镜像保留为 `ventasync-backend:rollback-4bfe4f7`、`ventasync-frontend:rollback-4bfe4f7`。页面刷新后进入新版 SPU 商品中心。本次不涉及真实平台调价或库存写入；高并发与真店闭环仍按验收清单推进。

## 14. 系统配置、云对象存储与商品设置发布（2026-11-12）

业务提交`3658871`已推送至`origin/main`，并发布到ERP服务器`121.37.45.132`的`/opt/ventasync`，入口为`https://erp.alienyan.store/`。

平台新增“系统配置”，可维护OSS、COS、OBS、AWS S3及S3兼容账号、测试桶连接、选择默认账号；新图片URL登记入库，旧本地图片继续兼容。租户新增“基础设置 → 商品设置”，集中维护供应商、品牌、分类、特性标签、包材；供应商联系人、联系方式、备注选填，停用保留历史关联。

发布前已通过相关后端回归、38项完整契约和前端`npm run build`。服务器阿里云PyPI镜像未同步`boto3==1.44.1`，官方源下载速度较慢，因此本地从官方源下载Python 3.12/Linux x86_64 wheels，检查Linux依赖闭包并上传校验；使用临时Dockerfile与wheel构建上下文完成离线构建，项目requirements及运行时Dockerfile逻辑保持一致。依赖包和构建记录保存在本次发布目录。

发布过程为构建镜像、保留回退镜像、停止API/Worker/scheduler、备份中心及全部租户库和图片卷、独立迁移、核对关键业务指纹和行数，再更新四个应用容器。MySQL和两个Redis容器及数据卷未重建，现有应用密钥和数据库凭据未更换。

| 检查 | 结果 |
| --- | --- |
| 迁移 | 中心库`center_0006 → center_0007`；`vs_tenant_demo`和`vs_tenant_demo2`均为`tenant_0011 → tenant_0012 → tenant_0013`，标准迁移正常完成 |
| 结构 | 中心17张业务表，租户各32张业务表；三个库外键0，表及字段中文注释完整 |
| 数据保留 | 关键账号、租户、成员、SPU资料、SKU成本及规格、订单、库存、映射、供应商、字典、图片和旧文件字段指纹及相关行数与停写前一致 |
| 公开访问 | 首页、两类登录页、`/settings/products`、`/platform/system-settings`、healthz、CSRF均200；首页与主资源及两个新页面JS/CSS的SHA-256与发布产物一致，CSP允许HTTPS图片 |
| 鉴权接口 | 可用租户身份的商品列表、详情、日志、SKU搜索及选项GET均200；选项含can_manage_options和供应商新字段；租户会话访问平台系统配置403；平台管理员GET系统配置200，五类厂商选项完整 |
| 进程与图片 | API readyz 200，Celery ping为1节点在线，应用容器正常；历史图片目录UID/GID 10001可读写，验证临时文件已删除 |
| 备份 | 三个数据库sql.gz与旧图片卷tar.gz均通过SHA-256及gzip校验；环境配置备份权限600 |
| 云账号边界 | 线上存储账号数量0、默认账号未配置；未请求真实云服务，需配置账号后实际上传并验证返回URL |

本次证据目录为`/opt/backups/releases/erp-3658871/`，包含`database/`、迁移前后结构及业务指纹、构建和迁移日志、鉴权及公开访问检查、部署脚本和回退脚本。鉴权检查仅创建临时Redis会话并在finally中清理，没有修改商品或供应商业务数据。

回退镜像为`ventasync-backend:rollback-3658871`和`ventasync-frontend:rollback-3658871`。回退脚本只切换应用镜像；数据库恢复应独立评估，不能因镜像可回退而直接执行降级或恢复旧备份。真实云上传、真实店铺调价及库存写入未纳入本次生产验证。

## 15. OBS连接检查故障修复（2026-11-12）

业务提交`90f8ced`已发布到同一ERP服务器。用户配置的OBS账号连接检查失败，真实诊断确认：中文地域名称不是SDK要求的区域编码，SDK尚未发出请求即抛InvalidRegionError；仅在内存修正区域后，原auto模式发向区域Endpoint的桶路径返回403，同套密钥使用桶域名发起HeadBucket返回200。

已在备份中心库后通过原配置服务保存正确区域编码及virtual寻址，保留原加密密钥、增加版本并记录审计。代码同时将OBS auto设为virtual，继续使用S3 V4与TLS证书验证；保存时拒绝中文Region、OBS path和带点桶名，前端区域标签及提示同步修改。已识别的错误使用固定中文消息和白名单provider_code，未知异常保持通用提示。

本次无依赖或数据库结构变更；复用上次已校验的Linux wheels完成后端构建，前端本地`npm run build`后打包，更新API/Worker/scheduler/nginx，MySQL/Redis及现有数据卷未重建。

| 检查 | 结果 |
| --- | --- |
| 本地 | 50项存储相关回归、npm run build及diff检查通过，未启动本地服务 |
| 真实桶连接 | 修正配置后的HeadBucket 200；新代码auto模式真实HeadBucket 200 |
| 平台接口 | 使用临时平台管理员会话与CSRF调用页面同一路径`POST /platform/storage-profiles/{profile_id}/test`返回200、ok=true；临时会话已清理 |
| 真实图片写入 | 68字节PNG上传成功，诊断目录及真实catalog目录的测试对象均已删除，删除返回204 |
| 匿名图片读取 | 常规商品目录图片URL匿名GET返回403、XML错误码AccessDenied；Bucket ACL包含AllUsers READ，不能据此认定对象可读 |
| 服务与产物 | 应用容器正常，readyz 200；公开页面、主JS/CSS和设置页面资源SHA-256与发布产物一致 |
| 备份及回退 | 中心库备份校验通过，环境配置备份权限600；旧镜像保留为backend/frontend的rollback-90f8ced标签 |

本次证据目录为`/opt/backups/releases/erp-90f8ced/`，包含配置纠正前的中心库备份、真实云诊断、代码发布后的接口检查、构建日志及公开资源检查。密钥仅在服务进程内解密使用，没有打印或写入Git。本次未修改桶或对象的公开访问策略；图片外链读取仍需配置相应对象权限或CDN回源访问。

OBS访问方式及兼容性依据：[华为OBS访问规则](https://support.huaweicloud.com/usermanual-obs/obs_41_0003.html)、[Boto3使用说明](https://support.huaweicloud.com/obs_faq/obs_faq_0274.html)。

桶ACL的匿名READ属于桶级权限，不能据此认定对象内容可读；对象外链可使用限定图片目录的GetObject桶策略或单独对象public-read ACL，不能直接扩大为整个桶公开。参考[官方ACL权限定义](https://support.huaweicloud.com/perms-cfg-obs/obs_40_0005.html)与[指定对象只读策略](https://support.huaweicloud.com/perms-cfg-obs/obs_40_0004.html)。

## 16. 租户内成员编号发布（2026-11-12）

业务提交`7cbe4b8`已推送至origin/main并发布到`/opt/ventasync`，保留此前邮件配置及登录页等已发布功能。新增`TenantMembership.member_no`及唯一约束`(tenant_id,member_no)`，租户`next_member_no`记录下一编号。三个成员创建入口统一锁租户行后在同事务分配，初始1、后续独立递增；停用、重邀、开通重试、激活及负责人转移均不改号。

中心库迁移`center_0008 → center_0009`按每租户`created_at,id`回填编号，高水位设为最大编号加1；三个租户库继续为`tenant_0013`。全局账号和内部成员主键不变，角色/范围、商品采购等人员、审计与任务关联仍使用原内部ID。成员页显示并复制本租户编号，响应在内部id旁新增member_no，避免破坏已有接口参数。

发布前通过79项身份/权限/商品相关回归、38项完整契约、npm run build及diff检查。镜像使用符合当前requirements的Linux wheels离线构建，前端使用受管理dist；停写后备份中心库、全部三个租户库及图片卷，再执行迁移并比较业务指纹。MySQL/Redis及数据卷未重建，应用密钥未更换。

| 检查 | 结果 |
| --- | --- |
| 成员编号 | 三租户分别为`[1,2]`、`[1]`、`[1]`，首号均1，高水位分别为3、2、2 |
| 数据保留 | 中心与三个租户库的关键业务指纹及行数一致，特别包括原成员id/user_id、成员角色、数据范围和商品人员引用 |
| 数据库规范 | 中心18张业务表、租户各32张，四个库外键0、中文表及字段注释完整 |
| 实际接口 | 可用租户身份成员列表、bootstrap、/me/tenants和商品选项GET均200；该租户首位成员member_no为1，内部成员ID保持4；临时会话已清理 |
| 前端与服务 | 首页、登录入口、设置页及健康接口200，成员页及主资源SHA-256与发布产物一致；应用容器正常，readyz 200 |
| 备份 | 中心、三个租户的sql.gz及图片卷tar.gz校验通过，发布环境配置备份权限600 |

证据目录为`/opt/backups/releases/erp-7cbe4b8/`，包含备份、迁移前后结构及业务指纹、编号核对、接口与公开资源结果、构建及迁移日志。旧镜像保留为`ventasync-backend:rollback-7cbe4b8`和`ventasync-frontend:rollback-7cbe4b8`。成员编号列为NOT NULL，回退需在停写状态下同步评估数据库结构与数据恢复，不能只切旧镜像后恢复旧版成员创建逻辑。
