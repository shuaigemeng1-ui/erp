# API 契约（首版 `/api/v1`）

## 通用约定

* 认证：同源 Cookie 会话（`vs_session`，HttpOnly）。写请求必须带 `X-CSRF-Token`，值与可读 Cookie `vs_csrf` 一致。
* 请求来源：写请求的 `Origin`/`Referer` 必须在允许列表内。
* 响应包裹：成功 `{"data": ..., "request_id": "..."}`；错误 `{"error": {"code","message","details"}, "request_id": "..."}`。
* 分页响应：`{"data": {"items": [...], "total": n, "page": 1, "page_size": 20}, "request_id": "..."}`。
* 大整数 ID 一律字符串返回。
* 异步操作返回 `202` + `{"data": {"task_id": "123"}}`，前端轮询 `GET /tenants/{tenant_id}/tasks/{task_id}`。
* 错误码：401 未登录 / 403 无权限 / 404 对象不可见 / 409 业务冲突 / 422 参数错误，具体码见 `backend/app/core/errors.py`。

## 认证与本人

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/auth/csrf` | - | 签发匿名 CSRF 令牌，同时写 `vs_csrf` Cookie；`data.csrf_token` |
| POST | `/auth/login` | - | `{email, password}`；成功后轮换会话 Cookie 与 CSRF，返回 `{user, tenants}` |
| POST | `/auth/logout` | 登录 | 撤销当前会话 |
| POST | `/auth/password` | 登录 | `{current_password, new_password}`；成功后撤销该用户其它会话 |
| GET | `/auth/invitations/{token}` | - | `{tenant_name, email, status, expires_at}`；令牌不存在/过期/已用返回对应错误码 |
| POST | `/auth/invitations/accept` | - | `{token, password, display_name}`；返回 `{user, tenants}` |
| GET | `/me` | 登录 | `{id, email, display_name, status, is_platform_admin, locale}` |
| GET | `/me/tenants` | 登录 | `[{id, code, name, status, timezone, member_status, roles: [code]}]` |

## 平台管理（中心库，平台管理员）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/platform/tenants?page&page_size&status&q` | 租户分页列表 |
| POST | `/platform/tenants` | `{code, name, timezone, plan_id?, owner_email}` → 202 `{task_id, tenant}` |
| GET | `/platform/tenants/{id}` | 租户详情（含开通步骤、数据库状态） |
| POST | `/platform/tenants/{id}/retry-provision` | 失败开通重试 → 202 |
| POST | `/platform/tenants/{id}/suspend` | `{reason}`；暂停后禁止租户业务访问与主动同步 |
| POST | `/platform/tenants/{id}/activate` | 重新启用（先核对平台状态） |
| GET | `/platform/provisioning-jobs/{id}` | 开通任务状态与失败原因 |
| GET | `/platform/plans` / `POST /platform/plans` | 套餐与 `features: [{feature_code, limit_value}]` |
| PATCH | `/platform/plans/{id}` | 调整套餐（降低额度不删除数据） |
| GET | `/platform/channel-apps/{channel}/{environment}` | 返回 `{partner_id, has_partner_key, has_partner_secret, redirect_url, status}`（密钥不回显） |
| PUT | `/platform/channel-apps/{channel}/{environment}` | `{partner_id, partner_key?, partner_secret?, redirect_url}` 加密保存 |
| GET | `/platform/audit-logs?page` | 平台管理审计 |

## 租户内

所有路径包含 `{tenant_id}`；服务端必须校验成员关系与路径租户一致。

### 启动数据

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/tenants/{tenant_id}/bootstrap` | 成员 | `{tenant, member: {id, roles, bindings}, permissions: [code], feature_flags: {code: bool}, permission_version, server_time}` |

### 成员、角色、数据范围

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/tenants/{tenant_id}/members?page&q&status` | `member.read` | 成员列表（含角色与数据范围摘要） |
| POST | `/tenants/{tenant_id}/members/invitations` | `member.invite` | `{email, role_bindings: [{role_id, scopes: [{scope_type, scope_ids}]}]}` → `{invitation_id, expires_at}` |
| PATCH | `/tenants/{tenant_id}/members/{member_id}` | `member.invite` | `{status: active/disabled}`；停用立即撤销会话与未执行手动任务权限 |
| PUT | `/tenants/{tenant_id}/members/{member_id}/roles` | `role.assign` | `{role_bindings: [...]}` 整体覆盖；校验授予权限不得超出操作者 |
| GET | `/tenants/{tenant_id}/roles` | `role.manage` | 角色列表（含权限编码、系统标记） |
| POST | `/tenants/{tenant_id}/roles` | `role.manage` | `{code, name, permissions: [code], copy_from_role_id?}` |
| PATCH | `/tenants/{tenant_id}/roles/{role_id}` | `role.manage` | 修改名称与权限 |
| DELETE | `/tenants/{tenant_id}/roles/{role_id}` | `role.manage` | 仅非系统角色且无绑定；否则 409 |
| GET | `/tenants/{tenant_id}/permissions` | `role.manage` | 权限编码目录（分组、中文名） |

### 商品与库存

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/tenants/{tenant_id}/products?page&q&status` | `catalog.product.read` | 列表；无 `catalog.cost.read` 时过滤成本字段 |
| POST | `/tenants/{tenant_id}/products` | `catalog.product.create` | `{code, name, brand?, category?, cost_amount?, base_price, description?, skus: [{sku_code, name, price, cost_amount?, specs?}], images?: [url]}` |
| GET | `/tenants/{tenant_id}/products/{product_id}` | `catalog.product.read` | 详情（含 SKU、图片、映射摘要） |
| PATCH | `/tenants/{tenant_id}/products/{product_id}` | `catalog.product.update` | 局部更新；成本字段需 `catalog.cost.read` 才能读写 |
| POST | `/tenants/{tenant_id}/products/{product_id}/archive` | `catalog.product.update` | 归档（不物理删除） |
| GET | `/tenants/{tenant_id}/inventory?page&sku_id&stock_place_id&q` | `inventory.read` | 余额列表（含商品名、SKU 编码、可用/占用数量） |
| POST | `/tenants/{tenant_id}/inventory/adjustments` | `inventory.adjust` | `{sku_id, stock_place_id, quantity_change, note}`；产生负库存返回 409 |
| GET | `/tenants/{tenant_id}/inventory/movements?page&sku_id` | `inventory.read` | 库存流水 |
| GET | `/tenants/{tenant_id}/inventory/stock-places` | `inventory.read` | 库存地点列表 |

### 店铺与平台商品映射

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/tenants/{tenant_id}/stores` | `store.read` | 店铺列表（凭据只返回状态与过期时间） |
| POST | `/tenants/{tenant_id}/stores/shopee/authorize` | `store.connect` | 生成一次性授权会话 → `{auth_url, expires_at}` |
| POST | `/tenants/{tenant_id}/stores/{store_id}/disconnect` | `store.connect` | 断开授权（停止依赖该凭据的任务） |
| GET | `/tenants/{tenant_id}/listings?store_id&mapping_status&page` | `channel.listing.read` | 平台商品与映射状态 |
| POST | `/tenants/{tenant_id}/listings/sync` | `channel.listing.map` | `{store_id}` → 202 `{task_id}` |
| POST | `/tenants/{tenant_id}/listings/{listing_id}/link` | `channel.listing.map` | `{product_id, sku_id}`；SKU 必须属于本租户 |
| POST | `/tenants/{tenant_id}/listings/{listing_id}/unlink` | `channel.listing.map` | 解除映射 |

### 订单、任务、审计

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/tenants/{tenant_id}/orders?page&status&store_id&q&date_from&date_to` | `order.read` | 列表（按店铺数据范围过滤） |
| GET | `/tenants/{tenant_id}/orders/{order_id}` | `order.read` | 详情：明细、状态历史、库存影响 |
| POST | `/tenants/{tenant_id}/orders/sync` | `order.sync` | `{store_id?}` → 202 `{task_id}` |
| GET | `/tenants/{tenant_id}/sync-runs?page&job_type&status` | `task.read` | 任务记录 |
| GET | `/tenants/{tenant_id}/sync-runs/{id}` | `task.read` | 任务详情 |
| POST | `/tenants/{tenant_id}/sync-runs/{id}/retry` | `task.read` | 重试 → 202 |
| GET | `/tenants/{tenant_id}/sync-schedules` | `task.read` | 定时同步配置 |
| PUT | `/tenants/{tenant_id}/sync-schedules/{id}` | `task.read` | `{enabled, interval_minutes}` |
| GET | `/tenants/{tenant_id}/tasks/{task_id}` | 成员 | 异步任务状态：`{task_id, type, status, result, error}` |
| GET | `/tenants/{tenant_id}/audit-logs?page&action` | `audit.read` | 租户审计 |

### 平台回调（不使用用户会话）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/integrations/shopee/oauth/callback?code&shop_id&state` | 校验一次性授权会话后换取 Token，成功 302 跳转前端店铺页；失败 302 带错误码 |
| POST | `/integrations/shopee/webhook` | Shopee 推送：验签 + 事件防重后写 `webhook_events`，返回 200；无效签名 401 |

## 权限编码

`catalog.product.read/create/update`、`catalog.cost.read`、`inventory.read/adjust`、`store.connect/read`、
`channel.listing.read/map`、`order.read/sync`、`stock.push`、`member.read/invite`、`role.manage/assign`、
`audit.read`、`task.read`、`tenant.setting.manage`。

## 前端行为要求

* 每个请求显式携带目标租户（路径中的 `tenant_id`）。
* 租户切换时：取消旧请求（AbortController）、清空旧数据与勾选、重新加载 bootstrap 权限、丢弃迟到的旧租户响应。
* 不同标签页可同时处理不同租户（不把租户存全局单例）。
* 按钮 / 菜单按有效权限显示，但后端始终独立授权。
