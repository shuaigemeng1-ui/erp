# 验收说明与证据（首版）

本文逐条对应 `plan.txt` §5「实施顺序、验收与交付约束」与全部实现约束，给出**验证方式**与**当前结果**。

> 口径：
> * **离线通过** = 在研发机执行自动化命令得到通过结果，不启动容器、不连接 MySQL / Redis、不访问真实 Shopee。
> * **需真实环境** = 必须部署 Docker + MySQL 8.4 + Redis + 已授权测试店铺（含回调域名）后才能验证，
>   离线结果不能替代，**不得把模拟结果标为真实通过**。
> * 当前结果会随模块收尾更新；最终结论以 Lead 通知后的全量重跑输出为准。

## 0. 验收环境与命令

研发机（Windows）离线校验：

```powershell
cd C:\Users\user\Desktop\ERP\backend
python -m compileall -q app
python -c "from app.main import app; print('routes', len(app.routes))"
python -m pytest tests/unit tests/contract -q
python scripts/verify_contracts.py
python -m alembic -c alembic/center.ini upgrade head --sql
python -m alembic -c alembic/tenant.ini -x db_url=mysql+pymysql://u:p@127.0.0.1:3306/vs_demo upgrade head --sql

cd C:\Users\user\Desktop\ERP\frontend
npm run build

cd C:\Users\user\Desktop\ERP
docker compose -f deploy/docker-compose.yml config -q
docker compose -f deploy/docker-compose.yml --env-file deploy/.env.example --profile release config -q
```

真实环境验收（部署机，需 Docker daemon、MySQL、Redis、已授权测试店铺）：

```bash
cp deploy/.env.example deploy/.env      # 修改全部 change-me
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile release run --rm migrate
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec -T mysql \
  mysql -uroot -p < deploy/verification/no_foreign_keys.sql
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec -T mysql \
  mysql -uroot -p < deploy/verification/schema_comments.sql
```

## 1. plan.txt §5 验证项 1–9

| # | 验收项（plan.txt 原文摘要） | 验证方式 | 当前结果 |
| --- | --- | --- | --- |
| 1 | 两租户存在相同商品、订单 ID 时，请求、缓存、文件、任务均不串租户 | 契约层：租户库每表强制 `tenant_id`、所有租户路由路径含 `{tenant_id}`、`TenantContext` 不可变、缓存键含租户/成员/权限版本；真实层：开两个租户构造同 ID 商品/订单，交叉请求与任务日志核对 | 离线契约通过（`verify_contracts.py`、`test_contracts.py`）；真实两租户环境待执行 |
| 2 | 「局部编辑 + 全局查看」不能获得全局编辑权 | `tests/unit/test_contracts.py::test_partial_edit_plus_global_read_cannot_compose_global_edit`、`tests/unit/test_identity.py` 授权用例 | 离线通过 |
| 3 | 无成本权限的用户在查询和输出文件中都看不到成本 | 契约层：`catalog.cost.read` 权限 + 商品响应成本字段白名单；接口层需 MySQL；真实层：无权限账号查商品/导出核对 | 权限契约离线通过；接口级需真实环境 |
| 4 | 成员停用或权限撤销后，后续请求和未执行的手动任务不能越权 | 契约层：`permission_version` + 权限快照 + 手动任务执行前复核；真实层：停用成员/撤销权限后调用接口与 Worker 任务 | 契约离线通过；真实环境待执行 |
| 5 | 重复拉单、回调和重试不重复创建订单、扣减或恢复库存 | 单元测试：订单防重键、库存差额（应扣-已扣）、Outbox 幂等；真实层：重复回调/重试与平台订单核对 | `tests/unit/test_sales.py` 离线通过；真实环境待执行 |
| 6 | 两个订单同时影响一个 SKU 时，库存变化完整保留 | 实现层：`SELECT ... FOR UPDATE` 行锁、固定顺序、余额/流水/订单库存效果同事务；真实层：并发压测后核对余额与流水 | 需真实环境（MySQL 并发） |
| 7 | Worker 中断后能够恢复未完成工作；库存推送不能被旧值覆盖 | 实现层：Celery `acks_late`、Outbox、推送任务按 store+SKU 串行并读取最新库存；真实层：kill Worker 恢复、旧任务重放核对远端库存 | 需真实环境（Redis/Celery/Worker） |
| 8 | 中心库和全部租户库外键数量为零 | 离线：`scripts/verify_contracts.py`（模型元数据 + Alembic 离线 SQL）；真实：`deploy/verification/no_foreign_keys.sql` | **离线通过（中心库 + 租户库，0 外键）**；真实库待执行 SQL |
| 9 | 单租户备份恢复不影响其他租户；恢复后先对账，再开启平台推送 | 离线：`deploy/scripts/backup.sh`/`restore.sh` 只做单库 `mysqldump`/恢复，恢复后自动关闭 `stock_push_enabled`/`order_sync_enabled`；真实：单租户恢复演练 | 脚本离线校验通过（`bash -n`）；真实恢复演练待执行 |

## 2. 阶段验收（plan.txt §5 P0–P6）

| 阶段 | 验收标准 | 验证方式 | 当前结果 |
| --- | --- | --- | --- |
| P0 工程骨架 | 能构建；新迁移不含外键，应用表与字段有中文注释 | `compileall`、`app.main` 导入、两套 Alembic 离线 SQL、`verify_contracts.py` | 离线通过 |
| P1 身份与租户 | 两租户独立开通，一个用户可加入两家，失败开通可重试 | 单测 + 真实环境开两租户；开通流水线 Worker 化：创建/重试返回 202 + task_id，`identity/tasks.py` 执行开通并落 `provisioning_jobs`，失败不激活、可重试；broker 不可用时降级同步并记录 warning | 代码与单测离线通过；真实开通需环境 |
| P2 权限与页面框架 | 前后端一致授权；越权 ID、角色组合、迟到响应不泄漏数据 | 授权纯逻辑单测、API 契约测试、前端权限指令与租户切换实现；新增 2 条 owner-transfer 路由、最后有效所有者保护（停用/移除角色 409）、登录失败限流（Redis 5 次/15 分钟 → 429 AUTH_RATE_LIMITED，成功清零，Redis 不可用降级放行） | 后端离线通过；前端 `npm run build` 通过；页面手工验收需真实环境 |
| P3 商品与库存 | SKU 映射清晰；重复请求与并发修改不重复/丢失数量 | 库存差额/幂等/行锁实现 + `tests/unit/test_inventory.py`；真实并发验证 | 离线单测通过；并发需真实 MySQL |
| P4 Shopee 连接 | 店铺归属唯一、凭据隔离、异常映射可人工处理 | 签名/状态校验/映射唯一性单测；真实测试店铺授权 | 离线单测通过；真实授权与回调需域名/应用 |
| P5 订单库存闭环 | 同订单不重复扣库存、任务重试可恢复、远端库存可核对 | 订单防重/库存差额/Outbox 单测；真实店铺全链路 | 离线单测通过；真实闭环需环境 |
| P6 首版交付 | 可独立开通、维护和暂停租户；完成真实店铺闭环验收 | 部署编排校验、备份恢复脚本、真实店铺试点 | 部署编排离线通过；真实试点待执行 |

## 3. 最终离线全量验收结果（2026-11-11，增量重跑，最新文件）

执行环境：Windows 研发机，Python 3.12.10、Node/pnpm 前端依赖已安装；**未启动 Docker 容器、未连接可用 MySQL/Redis**。
以下命令均真实执行，退出码与关键输出如下：

| 校验项 | 命令 | 最终结果 |
| --- | --- | --- |
| 语法编译 | `python -m compileall -q app` | 通过（exit 0） |
| 应用入口 | `python -c "from app.main import app; print('routes', len(app.routes))"` | 通过，`routes 67` |
| 单元 + 契约测试 | `python -m pytest tests/unit tests/contract -q` | **253 passed**（1 条第三方 StarletteDeprecationWarning，无失败） |
| 全量契约脚本 | `python scripts/verify_contracts.py` | **36 项通过，0 失败，0 警告**；exit 0；含 docs/api-contract.md 61 条路由全部存在（含 2 条 owner-transfer） |
| 中心库离线 SQL | `python -m alembic -c alembic/center.ini upgrade head --sql` | exit 0；`CREATE TABLE` 15（含 `alembic_version`，业务表 14，与 data-model 基线一致）；FOREIGN KEY 0、REFERENCES 0；表注释 14、字段注释 130 |
| 租户库离线 SQL | `python -m alembic -c alembic/tenant.ini -x db_url=mysql+pymysql://u:p@127.0.0.1:3306/vs_demo upgrade head --sql` | exit 0；`CREATE TABLE` 29（含 `alembic_version`，业务表 28，与基线一致）；FOREIGN KEY 0、REFERENCES 0；表注释 28、字段注释 304 |
| 迁移文件与 revision 链 | `verify_contracts.py` 内校验 | 6 个迁移文件齐全；center 末端 `center_0002`、tenant 末端 `tenant_0004`，down_revision 链路连续 |
| Docker Compose | `docker compose -f deploy/docker-compose.yml config -q`；另加 `--env-file deploy/.env.example`、`--profile release` | 三种方式均 exit 0；`config --profiles` 输出 `release`（不启动容器、不需要 daemon 运行） |
| 备份/恢复脚本 | `bash -n deploy/scripts/backup.sh` / `restore.sh` | 语法通过（exit 0；LF、`set -euo pipefail`） |
| 前端构建 | `cd frontend; npm run build` | **exit 0**；`vue-tsc --noEmit` + `vite build`；vite 构建 4.88s（命令总耗时 8.73s）；`dist/` 37 个文件、1.50 MB（gzip 后入口 353 kB） |
| Celery 任务注册 | `from app.workers.celery_app import celery_app; celery_app.loader.import_default_modules()` | `app.modules.identity.tasks` 已加入 `include`，注册 `provision_tenant_job`、`retry_provision_job`；两者按默认队列消费。`celery list` 需连接 broker，离线未执行 |
| 发布迁移入口 | `python scripts/migrate_all.py --help` | exit 0，参数说明正常 |
| 发布迁移无库行为 | `python scripts/migrate_all.py --list` | 需连接中心库；当前环境无授权库，脚本明确中止：`迁移失败：(pymysql.err.OperationalError) (1045, "Access denied ...")`，exit 1；未修改任何数据 |

### 3.1 部署编排一致性核对（离线）

| 核对项 | 结果 |
| --- | --- |
| `deploy/docker-compose.yml` 引用的 `backend/scripts/migrate_all.py` | 存在，命令为 `python scripts/migrate_all.py` |
| `frontend/Dockerfile` 的 COPY 路径 `/app/dist` 与 `vite.config.ts` 的 `build.outDir: 'dist'` | 一致；构建后 `frontend/dist/index.html` 存在 |
| `deploy/nginx/nginx.conf` | `location /api/` + `proxy_pass http://ventasync_api`；`try_files $uri $uri/ /index.html` SPA 回退；`gzip on`；安全响应头；`proxy_http_version 1.1` + `keepalive 32` 长连接 |
| `backend/Dockerfile` | `python:3.12-slim` 多阶段、`USER app`（非 root uid 10001）、仅装 `requirements.txt` |
| `frontend/Dockerfile` | `node:22-alpine` 构建 → `nginx:alpine` 静态；默认 `dist` |
| 对外端口 | Compose 仅 `nginx` 发布 `HTTP_PORT:80`；mysql/redis/api 无宿主机端口 |

### 3.2 收尾补强项验收（task-9 / task-10，离线）

| 补强项 | 实现/验证方式 | 当前结果 |
| --- | --- | --- |
| 开通流水线 Worker 化 | `app/modules/identity/tasks.py` 新增 `provision_tenant_job`、`retry_provision_job`；已加入 `celery_app.include`（默认队列）；路由返回 202 + task_id，broker 不可用降级同步；`provisioning_jobs` 记录步骤、失败可重试且不激活 | 导入与任务注册通过；离线单测通过；真实 Worker 需环境 |
| 登录失败限流 | 同邮箱 + IP 连续失败 5 次 / 15 分钟 → 429 `AUTH_RATE_LIMITED`；成功登录清零；Redis 不可用时降级放行并记录日志 | 纯逻辑/替身单测通过；真实 Redis 需环境 |
| 所有者转移与最后所有者保护 | 新增租户内 `POST /tenants/{tenant_id}/owner-transfer` 与平台 `POST /platform/tenants/{id}/owner-transfer`（docs 路由 61 条已覆盖）；停用成员/移除角色不得使租户无有效所有者（409） | 路由契约与单测离线通过；真实租户需环境 |
| Webhook 补偿 / 远端删除标记 | channels 模块补充 received 事件补偿扫描与 listing stale/inactive 标记（详情见模块汇报） | 离线单测通过；真实回调需环境 |

被测对象与证据位置：

| 交付物 | 位置 | 验收说明 |
| --- | --- | --- |
| 部署编排 | `deploy/docker-compose.yml`、`deploy/nginx/nginx.conf`、`backend/Dockerfile`、`frontend/Dockerfile` | 进程分离、非 root、仅 Nginx 对外、迁移独立 profile |
| 发布迁移 | `backend/scripts/migrate_all.py` | 中心库 + 全部租户库 `upgrade head`，MySQL 命名锁串行 |
| 备份恢复 | `deploy/scripts/backup.sh`、`deploy/scripts/restore.sh` | 单库备份/恢复；恢复后自动关闭推送与同步 |
| 库级验收 SQL | `deploy/verification/no_foreign_keys.sql`、`deploy/verification/schema_comments.sql` | 需真实 MySQL 执行 |
| 契约校验 | `backend/scripts/verify_contracts.py` | 零外键、中文注释、表清单、权限码、路由 |
| API 契约测试 | `backend/tests/contract/test_api_contract.py` | OpenAPI 路由、统一错误响应、ID 字符串、UTC |
| 授权/金额/时间契约 | `backend/tests/unit/test_contracts.py` | 授权并集、空范围、Decimal、UTC、错误码、异步租户上下文回归 |

## 4. 实施约束核对（plan.txt §5 末尾）

| 约束 | 落实情况 |
| --- | --- |
| 不擅自启动前后端项目或连接生产环境 | 本仓库验收仅离线命令；容器启动步骤写入 `deploy/README.md`，未执行 |
| 前端保留 `npm run dev`，提交前 `npm run build` 通过 | `npm run dev`/`npm run build` 脚本保留；`npm run build`（含 `vue-tsc --noEmit`）已真实执行通过 |
| 后端保留 `app.main:app` 入口，服务器通过 Docker 部署 | 已保留；`backend/Dockerfile` + Compose 使用 uvicorn `app.main:app` |
| 迁移使用独立发布任务，不由每个 API 副本启动时自动迁移 | `migrate` 服务 `profile=release`；API/Worker 启动命令不含迁移 |
| 不执行原 Laravel 建表迁移，从新的无外键基线创建 | 中心库 `center_0001` / 租户库 `tenant_0001` 起全新基线 |
| 无历史数据迁移、双写或旧系统在线切换 | 未包含相关任务；旧项目 `ventasync` 只读参考 |
| 首版按租户试点；真实平台推送默认关闭，完成映射核对后再启用 | Compose 默认不自动开启；恢复脚本恢复后自动关闭推送 |
| 异步操作返回 202 + 任务 ID（plan §3） | 开通/重试走 `identity/tasks.py` Worker（默认队列），路由返回 202 `{task_id, tenant}`；`provisioning_jobs` 可轮询；broker 不可用时降级同步 |
| 登录安全与限流（plan §3） | 同源 Cookie + CSRF；登录失败限流 5 次/15 分钟 → 429；Redis 不可用降级放行并记录日志 |
| 禁止删除最后一个有效所有者（plan §3） | owner-transfer 2 条路由 + 停用成员/移除角色时最后所有者保护（409）；权限变更递增 `permission_version` |
| 平台回调/Webhook 可靠性（plan §4） | 验签 + 事件唯一约束防重；Webhook 补偿扫描与 `listing` stale 标记（task-10） |

## 5. 需真实环境 / 外部前置条件的验收项（不得用模拟结果替代）

| 项目 | 外部前置条件 | 执行方式 |
| --- | --- | --- |
| 两租户隔离（验证项 1） | Docker、MySQL、Redis、两个已激活租户 | 两租户构造同 ID 商品/订单；交叉请求、任务日志、缓存键核对 |
| 成本字段不可见（验证项 3） | 已开通租户 + 无 `catalog.cost.read` 账号 | 查询商品列表/详情/导出，核对响应无成本字段 |
| 停用/撤权即时生效（验证项 4） | 已登录会话 + Worker | 停用成员/撤销权限后立即请求、触发手动任务 |
| 订单/库存闭环（验证项 5、6） | 已授权测试店铺、回调域名、平台应用 | 重复拉单/回调/重试；两订单并发影响同一 SKU 后对账 |
| Worker 恢复与推送顺序（验证项 7） | Celery Worker、Redis、测试店铺 | kill Worker 后恢复；旧任务重放核对远端可售库存 |
| 零外键真实库（验证项 8） | 已执行迁移的中心库 + 全部租户库 | `deploy/verification/no_foreign_keys.sql`，期望全部 0 |
| 备份恢复演练（验证项 9） | 至少两个租户、备份文件 | 只恢复租户 A 后核对租户 B 不受影响；对账后再开启推送 |
| 真实店铺闭环（P6） | Shopee 应用审批、回调域名、已授权测试店铺 | 按 `deploy/README.md` 试点流程执行并记录结果 |
| HTTPS / 域名 / Cookie Secure | 域名与证书 | 按 `deploy/nginx/nginx.conf` 注释启用 443 + HSTS，`SESSION_COOKIE_SECURE=true` |

## 6. 遗留与说明

1. **前端构建**：`frontend/` 已交付，`vite.config.ts` 的 `build.outDir=dist` 与 `frontend/Dockerfile` 的 COPY 路径一致；
   `npm run build` 已真实执行通过（第 3 节），最终交付前沿用该命令复跑即可。
2. **调度进程入口**：`backend/app/workers/scheduler.py` 已提供 `python -m app.workers.scheduler`
   入口（调度逻辑在 `app.modules.tasks.scheduler`）；运行时验证需真实 MySQL/Redis。
3. **平台初始化**：`backend/app/cli.py` 已提供 `python -m app.cli platform-init`，执行前需完成迁移并配置
   `BOOTSTRAP_PLATFORM_ADMIN_*`（详见 `deploy/README.md` 第 3 节）。
4. **真实环境项**：第 5 节全部标记为「需真实环境」，本次离线验收不宣称其通过；
   完成部署与真实店铺联调后，把实测结果、命令输出与截图/日志路径补充到本文件。
5. **平台推送开关**：首版默认关闭；完成店铺及 SKU 映射核对、恢复演练对账后按租户开启。
6. **增量重跑记录**：identity（task-9）与 channels（task-10）已收尾；本次为当前最新文件的增量重跑
   （2026-11-11）：`pytest` 253 passed、`verify_contracts.py` 36/36、docs 路由 61、应用路由 67、
   `npm run build` exit 0。后续若再改动代码/文档，需重跑第 3 节命令并同步更新本文件数字。
7. **未连接真实服务**：本节以上的离线验收未启动容器、未连接可用 MySQL/Redis、未访问 Shopee。
   `migrate_all.py --list` 需要授权中心库，本机默认配置连接被拒绝（exit 1，未接触任何业务数据）；
   MySQL/Redis 集成、Shopee 沙箱/真实店铺、真实备份恢复演练均留待第 5 节真实环境执行，**不标为通过**。

## 7. 真实环境验收结果（2026-11-11，服务器 121.37.45.132）

部署架构、步骤与完整证据见 [real-deployment.md](real-deployment.md)。本节只更新第 5 节各「需真实环境」项的状态：

| 原「需真实环境」项 | 真实环境结果 |
| --- | --- |
| 两租户隔离（验证项 1） | **通过**：真实开通 `demo`/`demo2` 两租户；跨租户访问均 404 `TENANT_NOT_FOUND`；商品数据互不可见 |
| 零外键真实库（验证项 8） | **通过**：中心库 + 2 个租户库 `REFERENTIAL_CONSTRAINTS` 合计 **0 条**；表/字段中文注释齐全 |
| 备份与恢复（验证项 9） | **通过**：每日 03:30 cron 全量备份；单租户恢复演练完成（29 表/tenant_0004），另一租户数据不变，恢复后自动关推送并重新启用 |
| 租户生命周期 | **通过**：暂停后业务访问 403 `TENANT_SUSPENDED`，启用后恢复 active |
| 邀请与权限 | **通过**：邀请链接签发 → 未登录预览 → 接受 → membership active → owner 登录 bootstrap 返回 20 项权限；令牌重复使用 422；已激活后重发 409 |
| 商品/库存 | **通过**：商品创建、库存 +25、超扣 409 `INVENTORY_NEGATIVE_FORBIDDEN`、流水/审计落库 |
| 登录限流 | **通过**：连续 5 次错误密码后 429 `AUTH_RATE_LIMITED` |
| 成本字段不可见（验证项 3） | 权限契约已通过；**真实无权限账号的接口级验证仍未执行** |
| 停用/撤权即时生效（验证项 4） | 契约通过；**真实停用成员后请求/Worker 验证仍未执行** |
| 订单/库存闭环（验证项 5、6） | **未验证**：需 Shopee 应用审批 + 已授权测试店铺 |
| Worker 恢复与推送顺序（验证项 7） | **未验证**：需真实店铺推送场景与 kill Worker 演练 |
| 备份恢复演练（验证项 9 后半） | **未执行**：需在试点租户上做恢复并核对另一租户不受影响 |
| 真实店铺闭环（P6） | **未验证**：Shopee 应用/回调域名/测试店铺均未就绪 |
| HTTPS / 域名 / Cookie Secure | **通过**：`https://erp.alienyan.store`（Let's Encrypt 自动续期 + HSTS），HTTP 301 跳转，Cookie 已为 Secure |

结论：首版系统已真实部署并完成「开通 → 邀请 → 登录 → 商品/库存 → 隔离 → 暂停/启用 → HTTPS → 备份 → 恢复演练」闭环验证；
**真实 Shopee 店铺闭环**仍依赖外部前置条件（Partner 凭据与测试店铺授权），不得视为已通过。

