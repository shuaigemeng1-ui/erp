# 真实服务器部署记录（2026-11-11）

> 目标：把本仓库部署到 `121.37.45.132`，MySQL 使用 Docker 容器。
> 本文只记录架构、步骤与验证证据，不含任何密码/密钥；服务器上的敏感值保存于
> `/root/ventasync-credentials.txt`（权限 600），请登录后立即修改。

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
公网 :80 ── Caddy（宿主机 systemd）
              └─ http://121.37.45.132  -> reverse_proxy 127.0.0.1:8081
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
2. **HTTPS 未启用**：当前通过 IP + HTTP 访问（Cookie `Secure=false`）。建议绑定域名后在 Caddy
   自动签发证书，并设置 `APP_ORIGIN=https://域名`、`SESSION_COOKIE_SECURE=true`。
3. **运维身份使用 root**：`PROVISIONER_DATABASE_URL` 当前为 MySQL root（仅用于建库/建号/迁移）。
   生产应改为最小权限账号。
4. **备份未定时化**：脚本已验证，但尚未配置 cron/systemd timer；恢复演练未执行。
5. **已停止 2fauth**：如需恢复：`docker update --restart=unless-stopped 2fauth && docker start 2fauth`
   （占用 8080，与当前应用不冲突）。
6. 测试租户/商品/审计数据仍在库中，正式试点前建议清理或改作演示数据。

## 7. 下一步计划（建议按优先级执行）

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
