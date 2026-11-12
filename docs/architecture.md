# 架构与协作契约

## 技术栈

FastAPI + Pydantic 2 + SQLAlchemy 2（同步 Session）+ PyMySQL + Alembic；Redis（会话/缓存）与独立
Redis 队列（Celery）；Vue 3 + TypeScript + Vite + Element Plus；Docker Compose + Nginx。
中心库 + 每租户独立业务库，全部表禁外键、带中文注释。

## 后端目录与分层

```text
backend/app/
  core/      配置、日志、错误码、权限编码、安全、Redis、上下文、权限计算（纯函数）
  db/        Base/MetaData、中心库与租户库 Session、Engine 缓存、模型注册表
  api/       deps（登录用户 / 租户上下文 / 权限依赖）、router 汇总
  modules/   业务模块：identity access platform catalog inventory channels sales tasks audit
  workers/   Celery 应用、调度进程
backend/alembic/center|tenant  两套迁移（独立发布任务执行）
```

每个模块内部固定分层：

```text
router.py        FastAPI 路由：参数模型、权限依赖、调用 service、返回统一响应
schemas.py       Pydantic 请求/响应模型（ID 序列化字符串）
repository.py    只做 SQLAlchemy 查询，不写业务规则、不开事务
service.py       业务规则 + 事务边界 + 审计/Outbox 写入；抛 AppError
models_center.py / models_tenant.py   模型（禁止 ForeignKey）
```

规则：

* 阻塞数据库的接口使用普通 `def`；每个请求 / 任务独立创建、关闭 Session。
* 事务由 Service 管理：一个业务用例一个事务；外部 HTTP 请求在事务提交后执行。
* 路由层不做业务判断；Repository 不提交事务。
* 写操作必须写审计或 Outbox（至少其一），且与业务数据同事务。
* 所有租户库查询必须带 `tenant_id` 条件；列表 / 总数 / 搜索 / 导出使用同一数据范围。
* 敏感字段用响应模型白名单；修改字段用请求模型白名单。
* 列表接口使用 `PageParams` / `paged()`；大整数 ID 用字符串序列化。

## 租户上下文

`RequestContextMiddleware` 装载会话 → `api/deps.py`：

1. `current_user`：Redis 会话 + 中心库用户，校验状态。
2. `tenant_context`：校验成员关系（中心库）→ 校验租户状态 → 读取租户库路由 → 加载 `AccessSnapshot`
   （权限版本 + 有效角色绑定 + 数据范围 + 功能开关）→ 构造不可变 `TenantContext` 放入 ContextVar，
   同时把租户库 Session 挂到 `request.state.tenant_db`。
3. `RequirePermission(code)`：功能权限；数据范围在 Service 中用 `EffectivePermissions.covers()` 校验。
4. Worker / 手动任务：使用 `app.core.context` 重建 `TenantContext`，执行前用
   `app.api.deps.ensure_permission()` 复核成员权限。

跨租户不信任请求体中的 `tenant_id`；回调只接受 600 秒且原子消费的一次性授权 state。
长期可复制链接记录在中心库 `channel_authorization_links`，只存摘要，可撤销和轮换。它是生成短期 state 的入口，不直接充当永久回调 state。回调重新校验租户、成员、权限与目标店铺；多店铺授权显式选择。跨库保存失败时中心库保存加密结果，重试入口可恢复。

任务列表、详情、状态及重试按店铺范围过滤；全租户任务只对明确全部范围可见。`task.read` 只读，定时配置修改需要 `task.schedule.manage`。任务 ID 必须先登记到租户库，不能凭任意 Celery UUID 读取后端结果。

`tenant_settings.order_sync_enabled/stock_push_enabled` 为同步最终开关，优先于功能展示配置。API 创建、调度、Outbox、Worker 和外部操作前均复核。手动任务始终使用发起人并复核当前范围，不转换成所有者；自动任务使用限定能力。

订单同步检查点由 `(tenant_id, store_id, channel, environment)` 唯一确定，固定窗口和游标跨批次保存，只有完整成功才推进水位。

## 跨模块接口（修改时同步调用方与契约）

`identity/service.py`（backend-identity 实现）：

```python
load_authenticated_user(center, user_id) -> AuthenticatedUser | None
load_tenant_access(center, tenant_id, user_id) -> TenantAccessInfo | None
load_tenant_db_route(center, tenant_id) -> TenantDbRoute | None
is_platform_admin(center, user_id) -> bool
```

`access/service.py`（backend-identity 实现）：

```python
@dataclass AccessSnapshot: permission_version, effective: EffectivePermissions, feature_flags
load_access_snapshot(db, tenant_id, member_id) -> AccessSnapshot
bump_permission_version(db, tenant_id) -> int          # 权限/绑定/成员变更后调用
```

`audit/service.py`（backend-identity 实现）：

```python
record(db, *, tenant_id, action, resource_type, resource_id=None, actor_id=None,
       summary="", payload=None, request_id="", ip="") -> int
```

`tasks/service.py`（backend-sales 实现）：

```python
create_sync_run(db, *, tenant_id, job_type, store_id=None, triggered_by="manual",
                requested_by=None) -> int
enqueue_outbox(db, *, tenant_id, event_type, aggregate_type, aggregate_id,
               payload=None, available_delay_seconds=0) -> int
```

`inventory/service.py`（backend-catalog 实现，供 sales 调用）：

```python
ensure_stock_place(db, tenant_id, stock_place_id) -> None
apply_balance_delta(db, *, tenant_id, sku_id, stock_place_id, quantity_change,
                    movement_type, reference_type="", reference_id=None,
                    operator_id=None, note="", allow_negative=False) -> int  # 返回调整后可用数量
```

`channels/service.py`（backend-channels 实现，供 sales 调用）：

```python
load_store(db, tenant_id, store_id) -> StoreInfo | None      # id, channel, name, status, region
mark_store_synced(db, tenant_id, store_id, at=None) -> None
```

`sales/service.py`（backend-sales 实现，供 channels 回调调用）：

```python
enqueue_order_sync(db, *, tenant_id, store_id, triggered_by="webhook") -> int   # 返回 sync_run_id
```

## 验收命令（不启动服务、不连接生产）

```powershell
cd C:\Users\user\Desktop\ERP\backend
python -m compileall -q app
python -c "from app.main import app; print('routes', len(app.routes))"
python -m alembic -c alembic/center.ini upgrade head --sql            # 输出中心库 DDL
python -m alembic -c alembic/tenant.ini -x db_url=mysql+pymysql://u:p@127.0.0.1:3306/vs_demo upgrade head --sql
python -m pytest tests/unit -q                                        # 纯逻辑测试，无数据库
python scripts/verify_contracts.py                                    # 无外键 / 中文注释 / 权限码 / 路由
cd ..\frontend; npm run build
cd ..\deploy; docker compose config -q                                # 只做配置校验，不启动容器
```

数据库真实验收（需授权环境，默认不执行）：

```sql
SELECT COUNT(*) FROM information_schema.REFERENTIAL_CONSTRAINTS
WHERE CONSTRAINT_SCHEMA IN ('ventasync_center', 'vs_tenant_1');  -- 必须为 0
```

## 版本与验证范围

迁移主线以实际 ScriptDirectory 为准。当前业务表中心库 15、租户库 29；版本末端为 center_0006 / tenant_0010。不得通过引用早期迁移文档跳过新增迁移。零外键和字段中文注释同时验证模型与最终 SQL，MODIFY 清除注释必须由后续增量迁移修复。

本轮离线证据与真实环境待验收内容见 `acceptance.md`。历史 `real-deployment.md` 不作为当前代码已上线的证明。
