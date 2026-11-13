# ERP 部署与运维（Docker Compose + Nginx）

本目录负责首版多租户 SaaS 的部署编排（plan.txt §1 / §5 P6）：MySQL 8.4、Redis（缓存/队列分离）、
API、Celery Worker、独立调度进程、独立迁移发布任务、Nginx 反向代理与前端静态托管、备份恢复脚本。

> 当前环境未启动任何容器：以下「运行时验证」需要在具备 Docker daemon 的部署机执行；
> 已完成的离线校验见本文末尾与 `docs/acceptance.md`。

## 1. 文件清单

| 文件 | 作用 |
| --- | --- |
| `docker-compose.yml` | 全部服务编排（mysql / redis-cache / redis-queue / api / worker / scheduler / nginx / migrate） |
| `.env.example` | 环境变量模板，复制为 `.env` 后修改 |
| `nginx/nginx.conf` | Nginx 站点：`/api` 反代、静态托管、gzip、安全响应头、长连接 |
| `scripts/backup.sh` | 单库 `mysqldump` 备份（支持按租户库遍历） |
| `scripts/restore.sh` | 单库恢复（重建库 → 导入 → 关闭推送 → 对账提示） |
| `backups/` | 默认备份目录（运行时创建，不提交仓库） |
| `../backend/Dockerfile` | 后端镜像（python:3.12-slim，非 root，多阶段 + 依赖层缓存） |
| `../frontend/Dockerfile` | 前端镜像（node:22-alpine 构建 → nginx:alpine 静态） |
| `../backend/scripts/migrate_all.py` | 发布迁移入口：中心库 + 全部租户库 `alembic upgrade head` |

## 2. 进程与网络模型

| 服务 | 镜像/构建 | 命令 | 网络 | 说明 |
| --- | --- | --- | --- | --- |
| `mysql` | `mysql:8.4` | 默认 | backend（internal） | InnoDB、utf8mb4、UTC；无宿主端口 |
| `redis-cache` | `redis:7` | `volatile-lru` | backend | 会话/缓存，允许淘汰 |
| `redis-queue` | `redis:7` | `noeviction` + AOF | backend | Celery broker/结果，禁止淘汰 |
| `api` | backend/Dockerfile | `uvicorn app.main:app` | edge + backend | 不迁移、不对外暴露端口 |
| `worker` | backend/Dockerfile | `celery -A app.workers.celery_app worker -Q default,outbox,scheduler,channel,sales` | edge + backend | 需要访问 Shopee，故接 edge |
| `scheduler` | backend/Dockerfile | `python -m app.workers.scheduler` | edge + backend | 独立调度进程，不放进 API 副本 |
| `migrate` | backend/Dockerfile | `python scripts/migrate_all.py` | backend | `profile=release`，仅发布时运行 |
| `nginx` | frontend/Dockerfile | `nginx -g 'daemon off;'` | edge | 唯一对外入口：`HTTP_PORT` → 容器 80 |

网络隔离：

* `edge`：公网入口网络，只有 nginx / api / worker / scheduler 接入；
* `backend`：`internal: true`，mysql 与 redis 只在此网络，无法访问外网；
* api 与 worker 同时接入两个网络，既被 nginx 访问又能访问数据库/Redis/外网。

数据卷：`ventasync_mysql_data`、`ventasync_redis_queue_data`（项目名可用 `COMPOSE_PROJECT_NAME` 覆盖）。

## 3. 首次部署

前置：Linux 服务器（建议 2C4G 起）、Docker Engine 24+、Docker Compose v2.20+、已备案域名（可选）。

```bash
# 1) 准备环境变量（必须修改所有 change-me 值）
cp deploy/.env.example deploy/.env
#   SECRET_KEY：openssl rand -base64 48
#   ENCRYPTION_KEY：openssl rand -base64 32（生产建议显式设置）
#   MYSQL_ROOT_PASSWORD / CENTER_DB_PASSWORD / BOOTSTRAP_PLATFORM_ADMIN_PASSWORD
#   APP_ORIGIN / CORS_ALLOW_ORIGINS / HTTP_PORT / SHOPEE_AUTH_REDIRECT_URL

# 2) 只校验编排，不启动容器（不需要 daemon）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env config -q

# 3) 构建镜像
docker compose -f deploy/docker-compose.yml --env-file deploy/.env build

# 4) 启动数据服务（首次会初始化中心库与账号）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d mysql redis-cache redis-queue

# 5) 发布迁移：中心库 + 全部租户库（此刻还没有租户，只升级中心库）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile release run --rm migrate

# 6) 创建首个平台管理员（仅执行一次；不开放公共管理员注册）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env run --rm api \
    python -m app.cli platform-init

# 7) 启动全部服务
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d

# 8) 检查
docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps
curl -fsS http://127.0.0.1:${HTTP_PORT:-8080}/healthz
curl -fsS http://127.0.0.1:${HTTP_PORT:-8080}/api/v1/auth/csrf
```

首次登录后由平台管理员创建租户、配置 Shopee 应用（生产/测试环境）、邀请租户负责人。

> `platform-init` 命令由 `backend/app/cli.py` 提供，执行前必须已完成第 5 步迁移；
> 重复执行会提示管理员已存在，不会创建第二个初始管理员。

## 4. 发布与回滚

### 4.1 标准发布流程

```bash
# ① 发布前备份中心库（租户库按需/全部）
deploy/scripts/backup.sh -d ventasync_center
deploy/scripts/backup.sh --all-tenants

# ② 构建并标记版本（APP_IMAGE_TAG 建议用 git 提交号/版本号）
APP_IMAGE_TAG=20260101-1 docker compose -f deploy/docker-compose.yml --env-file deploy/.env build

# ③ 执行迁移（独立发布任务；失败立即停止，不启动新版本）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile release run --rm migrate

# ④ 滚动更新应用进程（api / worker / scheduler / nginx）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --no-deps api worker scheduler nginx

# ⑤ 验证
docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps
curl -fsS http://127.0.0.1:${HTTP_PORT:-8080}/api/v1/auth/csrf
```

**API/Worker/调度器启动时绝不自动迁移**（plan.txt 实施约束）；迁移只能由 `migrate` 任务执行，
避免多副本并发执行 DDL。`migrate` 使用 MySQL 命名锁 `ventasync:migrate`，同一时刻只允许一个发布任务。

### 4.2 回滚

| 场景 | 操作 |
| --- | --- |
| 仅应用代码回归 | `APP_IMAGE_TAG=<旧版本> docker compose ... up -d --no-deps api worker scheduler nginx` |
| 迁移失败（DDL 未提交/部分失败） | 停止发布 → 用发布前备份恢复中心库/受影响租户库 → 回退镜像 |
| 需要降级迁移 | `alembic downgrade -1` 只在明确编写了 `downgrade()` 且经评审时使用；默认策略是「从备份恢复」而不是自动降级 |
| 数据被误操作 | `deploy/scripts/restore.sh -f <备份> -d <库名> ...`，恢复后先对账再开启推送（见第 5 节） |

回滚前必须确认：目标版本的代码与当前数据库 revision 兼容（新版本迁移可能新增了旧代码不认识的表/列）。
因此发布顺序固定为「备份 → 迁移 → 更新应用」，回滚顺序为「回退应用 → 视情况恢复数据」。

## 5. 备份与恢复

### 5.1 备份（单库）

```bash
# 中心库
deploy/scripts/backup.sh -d ventasync_center

# 全部租户库（逐个单库备份，互不影响）
deploy/scripts/backup.sh --all-tenants

# 单个租户库 / 自定义目录 / 保留 30 天
deploy/scripts/backup.sh -d vs_tenant_1 -o /data/backups -k 30
```

* 备份文件：`deploy/backups/<库名>_<UTC时间>.sql.gz`，旁路 `.sha256` 校验文件；
* `mysqldump` 参数：`--single-transaction --quick --hex-blob --routines --triggers --default-character-set=utf8mb4 --set-gtid-purged=OFF --no-tablespaces --databases`；
* 只做单库备份（不含 `--all-databases`），满足「单租户备份恢复不影响其他租户」；
* 建议 cron 定时执行，并把备份同步到另一台机器/对象存储（本脚本不负责异机复制）。

### 5.2 恢复（单库）

```bash
# 租户库（先备份，再恢复；建议提供 tenant-id 以校验租户状态）
deploy/scripts/backup.sh -d vs_tenant_1
deploy/scripts/restore.sh -f deploy/backups/vs_tenant_1_20260101T000000Z.sql.gz \
    -d vs_tenant_1 --tenant-id 1 --yes

# 中心库（影响全部租户，必须显式 --allow-center）
deploy/scripts/restore.sh -f deploy/backups/ventasync_center_20260101T000000Z.sql.gz \
    -d ventasync_center --allow-center --yes
```

安全机制：

* 恢复是破坏性操作：`DROP DATABASE` → `CREATE DATABASE` → 导入；必须 `--yes`（或交互输入库名）；
  仅支持恢复中心库或 `TENANT_DB_PREFIX` 开头的租户库，其他库名直接拒绝；
* 租户状态为 `active` 时拒绝恢复（先通过平台暂停租户，禁止业务访问与主动同步）；
* 备份旁路存在 `.sha256` 时先做完整性校验；
* 租户库恢复完成后自动执行
  `UPDATE tenant_settings SET stock_push_enabled = 0, order_sync_enabled = 0`，
  即 **恢复后先对账，再开启平台推送**；
* `--dry-run` 只打印恢复计划，不做任何修改。

### 5.3 恢复后对账（必须执行）

```bash
# 以下查询在恢复后的租户库执行（docker compose exec mysql mysql -uroot -p ...）
SELECT COUNT(*) FROM orders;
SELECT COUNT(*) FROM order_items;
SELECT COUNT(*) FROM order_stock_effects;
SELECT SUM(available_quantity) FROM inventory_balances;
SELECT COUNT(*) FROM outbox_events WHERE status = 'pending';
```

1. 订单数量与平台后台、恢复前备份一致；
2. `inventory_balances` 可用数量与 `stock_movements` 流水、`order_stock_effects` 的
   `applied_quantity/target_quantity` 差额一致；
3. 检查 `outbox_events` 中 `pending` 的旧事件：可能引用已不存在的对象，需人工确认后重试或标记失败；
4. 重启进程清空缓存：`docker compose -f deploy/docker-compose.yml restart api worker scheduler`；
5. 对账通过后，在平台页面或执行
   `UPDATE vs_tenant_N.tenant_settings SET stock_push_enabled = 1, order_sync_enabled = 1;`
   再重新启用租户，观察库存推送日志。

> 注意：恢复会回退备份时间之后的配置类数据（成员角色、权限、店铺凭据等），
> 需要在平台上复核后再开放使用；中心库恢复还需逐租户核对 `tenant_databases` 路由。

## 6. 新增租户库开通

首版流程（自动化）：平台管理员在「平台租户管理」创建租户 → 系统按
`TENANT_DB_PREFIX`/`TENANT_DB_USER_PREFIX` 建库建号 → 执行租户库迁移 → 初始化设置/角色/默认库存地点 →
邀请负责人 → 状态置为 `active`。每一步写入 `provisioning_jobs`，失败可重试且不会激活租户。

发布新租户迁移版本时，统一执行：

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile release run --rm migrate
```

脚本会从中心库 `tenant_databases` 读取全部租户库并逐个 `alembic upgrade head`，也可只处理指定租户：

```bash
docker compose ... run --rm migrate --tenant-id 3
docker compose ... run --rm migrate --database vs_tenant_3
docker compose ... run --rm migrate --target center     # 只升级中心库
docker compose ... run --rm migrate --list             # 只列出迁移目标（dry-run）
```

手工补救（仅运维验证使用，正常走开通任务）：

```bash
docker compose ... exec api python -m alembic -c alembic/tenant.ini \
  -x db_url="mysql+pymysql://用户:密码@mysql:3306/vs_tenant_3" upgrade head
```

暂停/启用租户：平台管理员在后台操作；暂停后禁止租户业务访问和主动同步，
已验签回调可暂存但不改变库存；重新启用时先做平台状态核对，再恢复推送。

## 7. 运维检查

```bash
# 服务状态与健康
docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps
docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs -f --tail=200 api worker

# 健康检查端点（容器内）
#   api:  /healthz（存活）、/readyz（中心库 + Redis 就绪）
#   nginx: /healthz（静态）
#   mysql / redis：compose healthcheck

# 手工确认数据库连接与字符集
docker compose ... exec mysql mysql -uroot -p -e "SHOW VARIABLES LIKE 'character_set_server'; SHOW VARIABLES LIKE 'collation_server';"

# 确认外键为零（中心库 + 每个租户库；验收项 8）
docker compose ... exec mysql mysql -uroot -p -e \
  "SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE CONSTRAINT_SCHEMA LIKE 'ventasync%' OR CONSTRAINT_SCHEMA LIKE 'vs_tenant_%';"
```

日志为 docker json-file（单文件 10MB×3）；生产建议接入集中日志（Loki/ELK）。
API 以单容器单进程运行，扩容用 `docker compose up -d --scale api=3`（Nginx upstream 会做 DNS 轮询；
如需固定 upstream 列表，可在 nginx.conf 中显式列出 ip 或改用 Swarm/K8s）。

## 8. 环境变量

全部变量见 `.env.example`，关键项：

| 变量 | 说明 |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | Compose 项目名，决定容器/数据卷命名 |
| `APP_IMAGE_TAG` | 镜像标签，发布回滚使用 |
| `HTTP_PORT` | 宿主机对外端口（默认 8080） |
| `APP_ORIGIN` / `CORS_ALLOW_ORIGINS` | 同源 Cookie 会话、CSRF/Origin 校验的对外地址 |
| `SECRET_KEY` / `ENCRYPTION_KEY` | 会话签名 / 平台密钥与店铺 Token 加密，生产必须替换 |
| `SESSION_COOKIE_SECURE` | HTTPS 部署时改为 `true` |
| `MYSQL_ROOT_PASSWORD` | 建库/建号/迁移身份密码（单机简化，见第 9 节） |
| `CENTER_DB_*` | 中心库库名与运行账号 |
| `TENANT_DB_PREFIX` / `TENANT_DB_USER_PREFIX` | 租户库与账号命名前缀 |
| `TENANT_POOL_SIZE` / `TENANT_MAX_OVERFLOW` | 每租户连接池，注意与 `MYSQL_MAX_CONNECTIONS` 匹配 |
| `CACHE_REDIS_MAXMEMORY` / `QUEUE_REDIS_MAXMEMORY` | 两个 Redis 实例内存上限 |
| `CELERY_CONCURRENCY` | Worker 并发度（IO 密集任务可适当提高） |
| `SHOPEE_AUTH_REDIRECT_URL` | 必须与 Shopee 应用后台登记的回调地址一致 |
| `BOOTSTRAP_PLATFORM_ADMIN_*` | 首次管理员，初始化后建议清空 |

## 9. 安全清单（上线前逐项确认）

- [ ] 修改 `SECRET_KEY`、`ENCRYPTION_KEY`、MySQL 密码、管理员初始密码；`deploy/.env` 权限 600 且不提交仓库；
- [ ] 生产环境把 `PROVISIONER_DATABASE_URL` 从 root 改为最小权限运维账号（仅 `CREATE`、`CREATE USER`、`GRANT OPTION` 等建库建号必需权限），
      应用运行账号只保留中心库权限，租户账号只保留各自租户库权限；
- [ ] 启用 HTTPS：Nginx 增加 443 与证书（参考 `nginx/nginx.conf` 末尾注释），设置 `SESSION_COOKIE_SECURE=true`、HSTS；
- [ ] 只暴露 Nginx；不额外发布 MySQL/Redis/API 端口；
- [ ] Redis 队列实例 `noeviction` + AOF，缓存实例独立；
- [ ] 备份任务已配置并验证过一次恢复演练；
- [ ] 关闭 Debug（`DEBUG=false`）、限制 `/docs`（生产环境已由后端关闭）；
- [ ] 真实平台推送默认关闭：完成店铺与 SKU 映射核对后再逐租户开启。

## 10. 已知依赖与离线校验

部署编排依赖以下模块入口（由后端各模块交付）：

| 依赖 | 状态说明 |
| --- | --- |
| `app.main:app` | 已提供 |
| `app.workers.celery_app` | 已提供，Celery 会自动查找模块内 `celery_app` 属性 |
| `app.workers.scheduler` | 已提供（`python -m app.workers.scheduler`，调度逻辑在 `app.modules.tasks.scheduler`） |
| `app.cli platform-init` | 已提供（`backend/app/cli.py`） |
| `frontend/package.json` + `npm run build` 产出 `dist` | 已提供；`vite.config.ts` 的 `build.outDir=dist`，与 `frontend/Dockerfile` 的 COPY 路径一致 |

离线校验（本仓库当前环境已完成，无需 Docker daemon）：

```bash
docker compose -f deploy/docker-compose.yml config -q                        # 使用 Compose 默认值
docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config -q
docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example config --profiles
```

运行时验证（需要 Docker daemon 与已安装前端依赖，部署机执行）：

```bash
docker compose ... build
docker compose ... up -d mysql redis-cache redis-queue
docker compose ... --profile release run --rm migrate
docker compose ... up -d
curl -fsS http://127.0.0.1:8080/api/v1/auth/csrf
```

真实环境验收项（两租户隔离、真实店铺闭环、备份恢复演练等）见 `docs/acceptance.md`。

## 11. 低配单机 + 已有 Caddy（2C2G 实测方案）

适用于中国大陆 2C2G 云服务器、且宿主机已有 Caddy 占用 80/443 的场景（本仓库已在
`121.37.45.132` 实际验证，见 `docs/real-deployment.md`）。

1. 构建产物准备：本地 `cd frontend && npm run build`，把整个项目（含 `frontend/dist`）上传到服务器；
2. 使用低配覆盖文件启动（不跑 Node 构建、限制内存、只绑定回环端口）：

   ```bash
   cp deploy/.env.example deploy/.env        # 修改全部 change-me；建议：
   # HTTP_BIND=127.0.0.1  HTTP_PORT=8081
   # APP_ORIGIN=http://<公网IP>  CORS_ALLOW_ORIGINS=http://<公网IP>
   # PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/  PIP_TRUSTED_HOST=mirrors.aliyun.com
   # CELERY_CONCURRENCY=1  MYSQL_INNODB_BUFFER_POOL_SIZE=128M  MYSQL_MAX_CONNECTIONS=100
   cd deploy
   docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env up -d --build
   docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env --profile release run --rm migrate
   docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env exec -T api python -m app.cli platform-init
   ```

3. 宿主机 Caddy 反代 + 自动 HTTPS（保留已有站点，仅追加域名与 IP 跳转站点；
   完整示例见 `deploy/caddy/Caddyfile.example`）：

   ```caddyfile
   erp.example.com {
       encode zstd gzip
       header {
           Strict-Transport-Security "max-age=31536000; includeSubDomains"
           X-Content-Type-Options "nosniff"
           X-Frame-Options "SAMEORIGIN"
           Referrer-Policy "strict-origin-when-cross-origin"
       }
       reverse_proxy 127.0.0.1:8081
   }

   http://203.0.113.10 {
       redir https://erp.example.com{uri} permanent
   }
   ```

   ```bash
   cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%Y%m%d-%H%M%S)
   # 追加站点后：
   caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   systemctl reload caddy
   ```

   启用 HTTPS 后同步修改 `deploy/.env`：
   `APP_ORIGIN=https://erp.example.com`、`CORS_ALLOW_ORIGINS=https://erp.example.com`、
   `SESSION_COOKIE_SECURE=true`、`SHOPEE_AUTH_REDIRECT_URL=https://erp.example.com/api/v1/integrations/shopee/oauth/callback`，
   然后 `docker compose ... up -d` 重建 api/worker/scheduler。

4. 首次登录后：平台管理 → 租户管理 → 创建租户 → 「生成邀请链接」，把链接发给负责人激活；
   若服务器已有其他应用，可按需 `docker stop <容器>` 并 `docker update --restart=no <容器>`
   释放内存（保留 Caddy 与 SSH）。

注意：
* `docker-compose.server.yml` 使用 `frontend/Dockerfile.runtime`，要求上传 `frontend/dist`；
* 2C2G 下建议 `CELERY_CONCURRENCY=1`、MySQL buffer pool 128M；实测常驻内存约 1.1GB；
* 前端镜像更新只需重新构建 `nginx` 服务：`docker compose ... build nginx && up -d nginx`。

### 11.1 运维要点（实测踩坑）

* **特殊字符密码**：MySQL root 密码含 `@ : / ? #` 时，Compose 拼接的连接串会被错误解析。
  必须在 `deploy/.env` 显式设置 `PROVISIONER_DATABASE_URL` 并对密码做 URL 编码
  （例如 `@` → `%40`），然后重建容器。
* **nginx 上游动态解析**：`deploy/nginx/nginx.conf` 使用 Docker DNS（`resolver 127.0.0.11 valid=10s`
  + 变量 `proxy_pass`），api 容器重建/IP 变化后不需要重启 nginx；若自定义配置，请保留该写法或用
  变量方式避免 502。
* **备份定时任务**：

  ```cron
  # /etc/cron.d/ventasync-backup
  30 3 * * * root cd /opt/ventasync && bash deploy/scripts/backup.sh --all-tenants -o /opt/backups -k 14 >> /var/log/ventasync-backup.log 2>&1
  ```

* **单租户恢复演练**（正式操作前先在测试租户执行）：

  ```bash
  # 1) 平台暂停目标租户（恢复脚本拒绝 active）
  # 2) 恢复（脚本自动校验 sha256、DROP+CREATE、恢复后关闭推送/同步）
  bash deploy/scripts/restore.sh -f /opt/backups/vs_tenant_1_xxx.sql.gz -d vs_tenant_1 --tenant-id 1 --yes
  # 3) 核对订单/库存/outbox 后重新开启推送/同步并启用租户
  ```

## 12. 只允许域名访问与数据持久化（已实测）

### 12.1 只允许域名访问（三层防线）

1. **Caddy（入口）**：`http://<IP>` → 301 跳转到域名；HTTPS 直连 IP 无证书、握手失败；
   全局 `servers { strict_sni_host }` → Host 与 TLS SNI 不一致直接 421；域名站点再加
   `@bad_host not host <域名>` → 421。
2. **nginx（静态与反代）**：`if ($host !~* ^(域名|localhost|127\.0\.0\.1)$) { return 421; }`
   （见 `deploy/nginx/nginx.conf`，换域名时同步修改）。
3. **FastAPI**：`TrustedHostMiddleware`，由 `TRUSTED_HOSTS` 控制；Compose 已把该变量透传到
   api/worker/scheduler（`.env` 中设置，未设置时默认不限制，便于本地开发）。

验证结果：域名 200；`http://IP` 301；`https://IP` 握手失败；`Host: evil` + SNI 域名 → 421；
直连容器 `Host: evil` → 421；直连 API `Host: evil` → 400。

> 更换域名时必须同步修改：Caddy 站点、nginx allowlist、`TRUSTED_HOSTS`、`APP_ORIGIN`、
> `CORS_ALLOW_ORIGINS`、`SHOPEE_AUTH_REDIRECT_URL`。

### 12.2 数据持久化

| 组件 | 持久化方式 | 实测 |
| --- | --- | --- |
| MySQL 8.4 | 命名卷 `ventasync_mysql_data` → `/var/lib/mysql` | 225MB；重启容器后中心库/租户库行数完全一致 |
| Celery 队列 Redis | 命名卷 `ventasync_redis_queue_data` → `/data`，AOF `yes` + `noeviction` | 重启保留未执行任务 |
| 缓存/会话 Redis | 不持久化（可接受：会话重建即可） | — |
| 业务备份 | `deploy/scripts/backup.sh` + 每日 03:30 cron，保留 14 天 | 中心库 + 全部租户库 `.sql.gz + .sha256` |

> `docker compose down` 不会删除命名卷；`docker compose down -v` 会删除数据卷，**禁止**在未备份时执行。

## 13. Git 管理（本地与服务器统一）

仓库：<https://github.com/shuaigemeng1-ui/erp.git>，默认分支 `main`。服务器 `/opt/ventasync`
是同一仓库的 git 工作树，更新只通过 git，不再用 tar 包覆盖。完整约定见
`docs/git-workflow.md`，服务器更新脚本：

```bash
cd /opt/ventasync
bash deploy/scripts/git_update.sh          # pull -> build -> up -d -> migrate
bash deploy/scripts/git_update.sh --skip-migrate
```

要点：

* `deploy/.env`、`backend/.env` 等敏感配置不入库，`git pull` 不会覆盖服务器配置；
* `frontend/dist` 已入库（服务器 2C2G 不跑 Node 构建），改前端后必须在本地
  `npm run build` 再提交；
* 发布打 tag（如 `v1.0.0`），回滚用 `git checkout <tag>` + `git_update.sh`；
* 仓库当前为 public，提交前确认没有真实密码/密钥。


## 14. SPU 商品图片持久化

本轮新增 API 图片卷 `catalog_media_data:/app/var/catalog-media`，具体卷名默认为 `ventasync_catalog_media_data`。API 以 UID/GID 10001 读写；本地可通过 `CATALOG_MEDIA_DIR` 指定目录，默认 `backend/var/catalog-media`（在 backend 目录启动时）。

`backup.sh --all-tenants` 在 API 已挂载图片卷时同时生成 `catalog_media_*.tar.gz` 和 SHA-256；API 容器停止时仍可通过 Docker 复制卷内容。首次升级前没有图片卷时明确跳过。中心库仍须另行单库备份。图片归档须配合同一批数据库恢复到图片卷，并校正 UID/GID 10001。备份脚本改动仅完成语法检查，本轮未在生产执行恢复演练。

商品中心新增 `tenant_0011`，应先备份、停止应用写入、运行在线迁移，再更新应用；不能直接启动新代码访问旧库。详情和本轮验收边界见 `docs/acceptance.md`。

## 备份、加密异地与恢复演练（批次3）

备份默认写入 **/var/lib/ventasync-backups**（Git 工作目录之外，目录 700 / 文件 600），
每日 **03:30（租户服务器本地时区）**由 systemd timer 触发，保留 **30 天**：

```bash
sudo install -m 644 deploy/systemd/ventasync-backup.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/ventasync-backup.timer  /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ventasync-backup.timer
```

- 备份内容：中心库（身份/权限/配置元数据）+ 全部租户业务库 + `deploy/.env` 与编排/网关配置 + 商品图片卷；
- 每个备份目录含 `manifest.json`（文件/SHA-256/校验结果）与加密包 `bundle.tar.gz.enc`
  （AES-256-CBC/PBKDF2，密钥 `/etc/ventasync/backup.key`，0600，请异地保管）；
- 异地存储独立私有：`BACKUP_OFFSITE_MODE=none|local|s3`，S3 使用独立 `BACKUP_S3_BUCKET`，
  **不得复用商品图片公开目录**；
- 状态记录在中心库 `backup_records`（`local_status/local_path/checksum_status/checksum/
  offsite_status/offsite_key/restore_status/restore_verified_at/failure_reason/stages`），
  平台接口：`GET /api/v1/platform/backups`、`POST /api/v1/platform/backups/{id}/restore-check`；
- 升级前额外备份：Agent 升级步骤已执行 `deploy/backup.sh`，后端登记 `trigger=pre_upgrade`；
- 隔离恢复演练：`bash deploy/scripts/restore_drill.sh --latest --record-latest`
  （自测：`--self-test`），自动校验账号/权限/商品/配置并生成 `drill-report.json`。

完整说明、演练清单与故障处理见 [`docs/backup-restore.md`](../docs/backup-restore.md)。

