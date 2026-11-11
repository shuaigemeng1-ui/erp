# 计划落地对照表（plan.txt → 代码）

> 目标：任何一条 `plan.txt` 要求都能在此找到落地位置与验证方式。
> 状态口径：**已实现** = 代码完成且离线验证通过；**需真实环境** = 需 MySQL/Redis/Shopee 授权店铺才能验证。

## 1. 首版范围

| plan.txt 首版项 | 落地位置 | 状态 |
| --- | --- | --- |
| 登录 / 会话 | `identity/router.py`（csrf/login/logout/password）、`identity/rate_limit.py`、`core/auth.py`、`core/redis.py` | 已实现 |
| 租户开通 | `identity/provisioning.py`（登记→建库建号→租户迁移→初始化→邀请→active）、`identity/tasks.py`（Celery 任务）、`platform/router.py`（202） | 已实现（真实开通需 MySQL） |
| 成员与角色 | `identity/router.py`（members/roles/owner-transfer）、`access/service.py` | 已实现 |
| 数据范围 | `access/service.py`、`core/access.py`（`ScopeGrant`/`EffectivePermissions`） | 已实现 |
| 主商品与 SKU | `catalog/*` | 已实现 |
| Shopee 店铺授权 | `channels/shopee/oauth.py`、`channels/router.py`、`platform/router.py`（应用凭据） | 已实现（真实授权需应用与回调域名） |
| 平台商品读取及映射 | `channels/shopee/client.py`、`channels/service.py`、`channels/router.py`（listings） | 已实现（真实接口联调需店铺） |
| 订单同步 | `sales/order_mapping.py`、`sales/service.py`、`sales/tasks.py` | 已实现（真实拉单需店铺） |
| 库存变化 | `inventory/service.py`（行锁+流水）、`sales`（订单库存影响差额） | 已实现 |
| 库存推送 | `channels/tasks.py`（按 store+sku 串行、读最新库存、最低 0）、`tasks/outbox.py` | 已实现（真实推送需店铺） |
| 任务记录 | `tasks/router.py`、`tasks/tasks.py`、`tasks/outbox.py`、`tasks/scheduler.py` | 已实现 |
| 审计 | `audit/service.py`、`audit/router.py`、`platform/service.py`（平台审计） | 已实现 |

## 2. 技术与工程组织

| plan.txt 选型 | 实际落地 |
| --- | --- |
| FastAPI / Pydantic 2 / SQLAlchemy 2 / PyMySQL | `backend/app`、`requirements.txt`（FastAPI 0.135、Pydantic 2.12、SQLAlchemy 2.0） |
| Alembic，中心库与租户库分别管理 | `backend/alembic/center.ini`、`tenant.ini`、两套 `env.py`；`backend/scripts/migrate_all.py` 发布任务 |
| MySQL 8.4 / InnoDB / utf8mb4 | `deploy/docker-compose.yml`（mysql:8.4，utf8mb4_0900_ai_ci）；模型 `table_options()` 统一 InnoDB |
| Redis 缓存与登录会话 | `core/redis.py`（会话/Session/CSRF/缓存/锁） |
| Celery + 独立 Redis 队列 + 独立调度进程 | `workers/celery_app.py`、`workers/scheduler.py`；Compose 中 api/worker/scheduler 分进程 |
| 平台 HTTP 客户端 HTTPX | `channels/shopee/client.py` |
| Vue 3 / TS / Vite / Router / Pinia / Element Plus | `frontend/`（Vue 3.5、Vite 7、Element Plus 2.14） |
| S3 兼容对象存储 | `core/config.py` 预留 S3 配置；`files` 表已建（上传接口列入后续阶段） |
| Docker Compose / Nginx / API-Worker-调度器分开 | `deploy/`（含 migrate release profile、备份恢复脚本） |

分层：`router → service → repository`，事务在 Service；`docs/architecture.md` 有完整约定。

## 3. 多租户与数据库

| 要求 | 落地 |
| --- | --- |
| 中心库 + 每租户独立业务库 | `identity/models_center.py`、各模块 `models_tenant.py`；`tenant_databases` 路由表 |
| 无外键（模型/迁移/SQL） | 所有模型无 `ForeignKey`；`scripts/verify_contracts.py` 检查元数据与迁移 SQL；`deploy/verification/no_foreign_keys.sql` 真实库验收 |
| 表和字段中文注释 | 模型 `comment=` + 迁移 `COMMENT`；`schema_comments.sql` 真实库验收 |
| 租户上下文（不可变、ContextVar） | `core/context.py`、`api/deps.py`（async 生成器依赖，修复过跨线程 ContextVar 缺陷） |
| Engine 按租户+版本缓存 | `db/session.py`（`TenantEngineCache`，LRU + dispose） |
| 运行账号隔离 / 运维身份建库 | `db/session.py`（`provisioner_engine`）；`identity/provisioning.py` 建库建号 |
| 生命周期 provisioning/active/suspended/provision_failed | `identity/constants.py`、`provisioning.py`、`platform/service.py`（暂停/启用） |
| 暂停租户禁止业务访问与主动同步；回调暂存 | `api/deps.py` 状态校验；`channels/tasks.py` 补偿跳过暂停租户 |
| 金额 DECIMAL / 时间 UTC / 大整数 ID 字符串 | `db/base.py`（Money/ExchangeRate/UTC）、各 schemas 序列化；`test_contracts.py` 固化 |

## 4. 身份、权限、API 与 Vue

| 要求 | 落地 |
| --- | --- |
| 全局用户→租户成员→角色绑定→权限+范围 | `identity/*` + `access/service.py` + `core/access.py` |
| 预置角色可复制修改、最后所有者保护 | `identity/permission_catalog.py`、`access` 预置角色；`service.transfer_owner` + 停用 409 |
| 权限编码清单 | `core/permissions.py`（20 个，文档/目录/预置角色/路由字面量一致） |
| 多角色并集规则（局部编辑+全局查看不合成） | `core/access.py` `EffectivePermissions`；`tests/unit/test_contracts.py` 固化 |
| 权限版本 + 缓存键含租户/成员/版本 | `access/service.py`、`tenants_settings.permission_version` |
| 同源 Cookie 会话 + CSRF + 来源校验 | `core/auth.py`、`main.py` 中间件；`tests/contract/test_api_contract.py` 覆盖 401/403 |
| API 族（auth/me/platform/tenants/integrations） | `api/router.py` 汇总 67 条路由；`docs/api-contract.md` 61 条 |
| 202 + 任务 ID、错误码、request_id | `core/http.py`、`core/errors.py`；任务轮询见 `tasks/router.py`、`platform/router.py` |
| Vue 首版页面 | `frontend/src/views`（20 个视图：登录/激活/租户/平台/成员/角色/商品/库存/店铺/映射/订单/任务/审计） |
| 租户切换取消旧请求、丢弃迟到响应 | `frontend/src/api/http.ts`、`stores/tenant.ts`、`router` 守卫 |
| 前后端一致授权 | 后端 `RequirePermission` + 前端 `v-permission`/菜单；契约测试覆盖越权 |

## 5. Shopee 订单与库存闭环

| 要求 | 落地 |
| --- | --- |
| 平台统一配置应用凭据（加密保存） | `platform/service.py`（GET 不回显/PUT 加密）、`channels/models_center.py` |
| 一次性授权会话、回调换 Token、归属唯一 | `channels/shopee/oauth.py`（Redis state 含 tenant）、`channel_account_registry` 唯一约束 |
| Token 刷新串行 / 失效标记 | `channels/shopee/client.py`、`channels/tasks.py`（reauth_required） |
| 主商品与可售 SKU 分开；无规格也有 SKU | `catalog`；开通初始化默认库存地点 |
| 平台商品读取与明确映射 | `channels/service.py`（唯一匹配才自动，否则 pending/ambiguous；人工映射校验归属） |
| 平台订单原始数据→统一订单明细 | `sales/order_mapping.py`、`platform_order_raw`、订单唯一键 |
| 未映射/未知状态不猜库存 | `sales/service.py`（不扣库存、标异常） |
| 订单库存影响差额（应扣-已扣） | `order_stock_effects` + `inventory.apply_balance_delta` 同事务 |
| 外部订单允许负库存；推送最低 0 | `sales`（allow_negative + abnormality）、`channels/tasks.py` clamp |
| Outbox + Worker 重建租户上下文 | `tasks/outbox.py`、`tasks/tenancy.py`、`channels/tasks.py` |
| 手动任务复核权限；定时任务受限身份 | `tasks/tasks.py`（ensure_permission）、`tasks/tenancy.py`（自动化身份） |
| 回调验签、防重、补偿 | `channels/router.py`（webhook）、`channels/tasks.py`（120s 后补偿，最多 5 次） |
| 库存推送串行、读最新库存 | `channels/tasks.py`（store+sku 锁 + 查询最新可售） |
| 队列/缓存 Redis 分开 | `deploy/docker-compose.yml` 两个 Redis 服务 |

## 6. 实施顺序与验收

| 阶段 | 状态 |
| --- | --- |
| P0 工程骨架 | 完成：可构建、无外键、中文注释、双库迁移 |
| P1 身份与租户 | 完成（离线）；真实两租户开通需 MySQL |
| P2 权限与页面框架 | 完成（后端离线 + 前端构建通过）；真实越权联调需环境 |
| P3 商品与库存 | 完成（离线单测 + SQLite 冒烟）；并发需真实 MySQL |
| P4 Shopee 连接 | 完成（离线签名/映射单测）；真实授权需应用/域名/店铺 |
| P5 订单库存闭环 | 完成（离线单测 + SQLite 幂等冒烟）；真实闭环需环境 |
| P6 首版交付 | 完成编排/脚本/页面/审计；真实试点、备份恢复演练待执行 |

验收证据与逐条结果：`docs/acceptance.md`。离线命令：

```bash
cd backend
python -m compileall -q app
python -c "from app.main import app; print(len(app.routes))"     # 67
python -m pytest tests/unit tests/contract -q                    # 253 passed
python scripts/verify_contracts.py                               # 36 项全通过
python -m alembic -c alembic/center.ini upgrade head --sql       # 15 表，0 外键
python -m alembic -c alembic/tenant.ini -x db_url=... upgrade head --sql  # 29 表，0 外键
cd ../frontend && npm run build                                   # 通过
cd .. && docker compose -f deploy/docker-compose.yml config -q    # 通过（不启动容器）
```

## 7. 与 plan.txt 的取舍与后续阶段

* 商品刊登、价格营销、售后协商、平台发货/运单、结算对账、其他渠道、在线支付、完整仓储采购：按 plan.txt 列入后续阶段，本期未实现。
* S3 文件上传接口、导出台阶：`files` 表与 S3 配置已就绪，接口未开放（首版页面不需要）。
* 真实环境验收（MySQL/Redis/Shopee 授权店铺/备份恢复演练/HTTPS）必须由部署机执行，离线结果不替代（见 `docs/acceptance.md` §5）。
* 旧项目 `C:\Users\user\Desktop\ventasync` 未被修改，仅作为 Shopee 业务参考。
