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
| POST | `/auth/login` | - | `{email, password, scope?}`；`scope=tenant`（默认）为租户 ERP 登录，`scope=platform` 仅平台管理员可登录；成功后轮换会话 Cookie 与 CSRF，返回 `{user, tenants, session_scope}` |
| POST | `/auth/logout` | 登录 | 撤销当前会话 |
| POST | `/auth/password` | 登录 | `{current_password, new_password}`；成功后撤销该用户其它会话 |
| GET | `/auth/invitations/{token}` | - | `{tenant_name, email, status, expires_at, requires_login}`；令牌不存在/过期/已用返回对应错误码 |
| POST | `/auth/invitations/accept` | - | `{token, password?, display_name?}`；新账号设置密码，已有账号必须先以匹配身份登录；不能通过邀请恢复全局停用账号；成功返回 `{user, tenants, session_scope}` |
| GET | `/me` | 登录 | `{id, email, display_name, status, is_platform_admin, locale, session_scope}` |
| GET | `/me/tenants` | 登录 | `[{id, code, name, status, timezone, member_status, roles: [code]}]` |

> 登录端隔离：租户登录会话只能访问 `/tenants/{tenant_id}/...` 业务接口；平台管理登录会话只能访问 `/platform/...` 接口。跨端访问返回 `403 SCOPE_DENIED`。

## 平台管理（中心库，平台管理员）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/platform/tenants?page&page_size&status&q` | 租户分页列表 |
| POST | `/platform/tenants` | `{code, name, timezone, plan_id?, owner_email}` → 202 `{task_id, tenant}` |
| GET | `/platform/tenants/{id}` | 租户详情（含开通步骤、数据库状态、负责人邀请摘要 `owner_invitation`，不含 token） |
| POST | `/platform/tenants/{id}/owner-invitation` | 重新签发负责人邀请：轮换令牌，返回 `{invitation_id, token, invitation_url, email, expires_at}`（明文只返回一次） |
| POST | `/platform/tenants/{id}/retry-provision` | 失败开通重试 → 202 |
| POST | `/platform/tenants/{id}/suspend` | `{reason}`；暂停后禁止租户业务访问与主动同步 |
| POST | `/platform/tenants/{id}/activate` | 重新启用（先核对平台状态） |
| GET | `/platform/provisioning-jobs/{id}` | 开通任务状态与失败原因 |
| GET | `/platform/plans` / `POST /platform/plans` | 套餐与 `features: [{feature_code, limit_value}]` |
| PATCH | `/platform/plans/{id}` | 调整套餐（降低额度不删除数据） |
| GET | `/platform/channel-apps/{channel}/{environment}` | 返回 `{partner_id, has_partner_key, has_partner_secret, redirect_url, status}`（密钥不回显） |
| PUT | `/platform/channel-apps/{channel}/{environment}` | `{partner_id, partner_key?, partner_secret?, redirect_url}` 加密保存 |
| GET | `/platform/audit-logs?page` | 平台管理审计 |
| GET | `/platform/system-settings` | 返回{default_storage_profile_id,version,profiles,providers:[{code,name}]}；profiles不含明文或密文凭据 |
| POST | `/platform/storage-profiles` | 新增存储账号；提交非敏感配置及access_key_id/secret_access_key，返回StorageProfile |
| PUT | `/platform/storage-profiles/{profile_id}` | 提交完整非敏感配置与version；同厂商空凭据保留旧值，换厂商须重填两项 |
| PUT | `/platform/system-settings/default-storage` | {profile_id,version}；选择已启用且凭据完整的账号，返回完整system-settings结构 |
| POST | `/platform/storage-profiles/{profile_id}/test` | 对已保存账号执行HeadBucket检查，成功返回{ok:true,message}；不上传测试图片、不修改桶读权限 |

### 平台系统配置与对象存储

所有系统配置接口都要求平台管理员及平台管理会话，租户会话不能调用。账号字段为 `name/provider/endpoint/bucket/region/public_base_url/key_prefix/addressing_style/enabled`；provider为 `oss/cos/obs/s3/s3_compatible`，addressing_style为 `auto/virtual/path`。Endpoint使用根HTTPS服务地址，不包含桶名、对象路径、查询参数或凭据；public_base_url允许CDN路径，但不能包含查询参数或临时签名。

StorageProfile响应为上述非敏感字段，加 `id/version/has_access_key/has_secret_key`。访问密钥ID及Secret都不回显；保存只接收写入值，审计不记录凭据。版本冲突返回409，默认账号不能直接停用。切换默认不改写已有文件URL。

平台管理员需要在云厂商配置图片公开读取或CDN长期访问地址。连接测试只检查桶访问能力，不等于已验证外部匿名读取。未设置默认账号返回422；云端上传或测试失败返回明确错误，不能返回成功URL，也不回落本地保存。实际云账号与权限仍需真实验收。

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
| POST | `/tenants/{tenant_id}/members/invitations/{invitation_id}/resend` | `member.invite` | 轮换令牌并返回 `{invitation_id, token, invitation_url, email, expires_at}`；旧令牌立即失效 |
| PATCH | `/tenants/{tenant_id}/members/{member_id}` | `member.invite` | `{status: active/disabled}`；停用立即撤销会话与未执行手动任务权限 |
| PUT | `/tenants/{tenant_id}/members/{member_id}/roles` | `role.assign` | `{role_bindings: [...]}` 整体覆盖；校验授予权限不得超出操作者 |
| POST | `/tenants/{tenant_id}/owner-transfer` | 当前所有者 | `{target_member_id}` 转移所有者（平台可用 `/platform/tenants/{id}/owner-transfer`）；最后有效所有者不可停用/移除角色 |
| GET | `/tenants/{tenant_id}/roles` | `role.manage` | 角色列表（含权限编码、系统标记） |
| POST | `/tenants/{tenant_id}/roles` | `role.manage` | `{code, name, permissions: [code], copy_from_role_id?}` |
| PATCH | `/tenants/{tenant_id}/roles/{role_id}` | `role.manage` | 修改名称与权限 |
| DELETE | `/tenants/{tenant_id}/roles/{role_id}` | `role.manage` | 仅非系统角色且无绑定；否则 409 |
| GET | `/tenants/{tenant_id}/permissions` | `role.manage` | 权限编码目录：`{groups: [{code, name, permissions: [{code, name}]}], permissions: [code]}`（角色编辑器按 `groups` 分组渲染，分组内条目字段是 `permissions`） |

### 商品与库存

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/tenants/{tenant_id}/products?page&q&status` | `catalog.product.read` + 全部范围 | SPU列表；支持编号/中英文名/SKU/别名搜索，以及销售状态/类型、品牌、分类、人员、供应商和标签筛选 |
| POST | `/tenants/{tenant_id}/products` | `catalog.product.create` + 全部范围 | 创建SPU和至少一个SKU；必填code/name/skus，name为中文名；SKU name可由SPU名补齐，旧售价字段默认0 |
| GET | `/tenants/{tenant_id}/products/{product_id}` | `catalog.product.read` + 全部范围 | SPU资料、SKU销售信息、别名、图片、成本/重量/状态汇总 |
| PATCH | `/tenants/{tenant_id}/products/{product_id}` | `catalog.product.update` + 全部范围 | 局部更新；支持version与嵌套SKU更新；三类SKU成本需catalog.cost.read；SPU cost_amount直接修改返回422 |
| POST | `/tenants/{tenant_id}/products/{product_id}/archive` | `catalog.product.update` | 归档（不物理删除） |
| GET | `/tenants/{tenant_id}/products/options` | `catalog.product.read` + 全部范围 | 返回本租户供应商、人员、品牌/分类/标签/包材选项及can_manage_options；人员ID为租户成员ID |
| POST | `/tenants/{tenant_id}/products/options` | `catalog.product.update` + 全部范围 | `{kind, name, parent_id?}`；kind为brand/category/tag/packaging；父级仅分类可用 |
| PATCH | `/tenants/{tenant_id}/products/options/{option_id}` | `catalog.product.update` + 全部范围 | 更新name/parent_id/status；已被商品引用的资料不可改名，分类禁止循环父级 |
| POST | `/tenants/{tenant_id}/products/suppliers` | `catalog.product.update` + 全部范围 | 必填code/name；contact_name/contact_details/remark选填 |
| PATCH | `/tenants/{tenant_id}/products/suppliers/{supplier_id}` | `catalog.product.update` + 全部范围 | 更新code/name/contact_name/contact_details/remark/status，停用保留历史关联 |
| POST | `/tenants/{tenant_id}/products/media` | `catalog.product.create`或`catalog.product.update` + 全部范围 | multipart file上传商品图片至平台默认对象存储，PNG/JPEG/WebP，最大10MB；成功保存厂商、对象键和长期URL，返回{id,url} |
| GET | `/tenants/{tenant_id}/products/media/{file_id}` | `catalog.product.read` + 全部范围 | 鉴权读取本租户历史本地图片，私有、不缓存；云文件307重定向至记录中已保存的长期URL |
| POST | `/tenants/{tenant_id}/products/{product_id}/skus` | `catalog.product.update` + 全部范围 | 创建SKU并受套餐额度限制；{sku_code,aliases?,specs?,cost_amount?,guidance_cost?,average_cost?,weight_g?,sales_status?,image_url?} |
| PATCH | `/tenants/{tenant_id}/products/{product_id}/skus/{sku_id}` | `catalog.product.update` + 全部范围 | 修改SKU/规格/状态/成本和完整有效别名集合；可带SKU version，成本另需catalog.cost.read |
| GET | `/tenants/{tenant_id}/products/{product_id}/logs` | `catalog.product.read` + 全部范围 | 分页返回{id,summary,actor_name,created_at,changes:[{entity,sku_id,field,label,old,new}]}，成本差异按权限过滤 |
| GET | `/tenants/{tenant_id}/inventory?page&sku_id&stock_place_id&q` | `inventory.read` | 余额列表（含商品名、SKU 编码、可用/占用数量） |
| POST | `/tenants/{tenant_id}/inventory/adjustments` | `inventory.adjust` | `{sku_id, stock_place_id, quantity_change, note}`；产生负库存返回 409 |
| GET | `/tenants/{tenant_id}/inventory/movements?page&sku_id` | `inventory.read` | 库存流水 |
| GET | `/tenants/{tenant_id}/inventory/stock-places` | `inventory.read` | 库存地点列表 |


### SPU—SKU 商品中心字段与规则

- SPU基本信息：`code/name/name_en`、`preferred_supplier_id`、`purchaser_id/developer_id/inquiry_buyer_id`、`hs_code/material_zh/material_en/usage`、`brand/category/feature_tags/packaging_name`、商品和包装后的`length_cm/width_cm/height_cm`、`sales_type/remark/features`、图片与系统时间。包装后尺寸分别为`package_length_cm/package_width_cm/package_height_cm`。
- `sales_type`固定为`normal/variation/bundle/assembly`；捆绑/组装本轮为分类标记。人员必须是当前租户有效成员，新选供应商必须属于本租户且有效；原供应商停用后可保留原选择并修改商品其他字段。
- 租户页面`/settings/products`集中维护供应商、品牌、分类、特性标签和包材，操作需要全租户商品编辑权限。供应商字段`contact_name/contact_details/remark`分别为联系人(100字符)、联系方式(255字符)、备注(2000字符)，均可省略或清空。资料使用`active/disabled`状态，停用不删除历史数据；已停用字典值不可新选，历史商品原值可保留。品牌/分类/标签/包材名称被商品引用时改名返回409，避免静默变更商品及其操作记录。
- SKU的`specs`为字符串键值JSON，`aliases`写入为字符串数组；响应别名为`[{id,identifier,is_active}]`，含停用历史。别名与所有SKU主编号共用租户内唯一空间。编号变更会将旧编号保留为别名，别名停用不删除历史映射。
- 图片`images[]`与SKU `image_url`支持最多2048字符的长期URL，新图片须先上传登记并校验租户归属；图片切换默认厂商后仍使用原保存URL。
- SKU支持`image_url/cost_amount/guidance_cost/average_cost/weight_g/sales_status/version`。销售状态为`on_sale/discontinued/trial/clearance`；旧`status=active/archived`只表示归档生命周期。
- SPU的`sales_status`按未归档SKU汇总：一致则原状态；混合依次优先`partial_clearance`、`partial_discontinued`，在售+试卖为`on_sale`。正常SPU至少一个未归档SKU。
- `weight_summary`及`cost_summary.{current,guidance,average}`返回`{state,value,has_missing}`；state为`uniform/multiple/missing/partial_missing`。只有uniform有统一value，部分缺失不能当作所有SKU相同。金额/重量JSON值为字符串。
- 无成本权限时不返回`cost_summary`和SKU三类成本；商品日志与全局审计同样隐藏敏感成本差异。SPU成本汇总只读，SKU三类成本本轮人工维护。
- `version`用于并发修改检查，不一致返回409；无实际字段变化不增加版本和操作日志。业务数据与字段旧值/新值日志同事务提交。
- SKU别名匹配优先使用已确认平台映射，再按主编号/有效别名精确识别；不通过标题或跨商品规格猜测。平台SKU ID与seller_sku分别保留，订单明细保存platform_sku_id；原生ID后补全时先匹配既有明细，避免重复扣库存。

### 店铺与平台商品映射

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/tenants/{tenant_id}/stores` | `store.read` | 店铺列表（凭据只返回状态与过期时间） |
| POST | `/tenants/{tenant_id}/stores` | `store.connect` | 新建店铺 `{channel, name}`，状态 `pending_authorization` |
| POST | `/tenants/{tenant_id}/stores/{store_id}/tiktok/authorize` | `store.connect` | 为待授权店铺生成 Service ID 授权链接 |
| POST | `/tenants/{tenant_id}/stores/{store_id}/shopee/authorize` | `store.connect` | 为待授权店铺生成 Shopee 授权链接 |
| POST | `/tenants/{tenant_id}/stores/shopee/authorize` | `store.connect` | 创建可撤销的长期授权入口 → `{auth_url, expires_at}`；打开入口后才生成 600 秒一次性 state |
| POST | `/tenants/{tenant_id}/stores/tiktok/authorize` | 登录 + `store.connect` | `{environment?, redirect_path?}`；返回 TikTok Shop 授权地址 `auth_url` |
| DELETE | `/tenants/{tenant_id}/stores/{store_id}/authorization-link` | `store.connect` + 店铺范围 | 撤销长期授权链接及其衍生会话 |
| POST | `/tenants/{tenant_id}/stores/{store_id}/disconnect` | `store.connect` | 断开授权（停止依赖该凭据的任务） |
| GET | `/tenants/{tenant_id}/listings?store_id&mapping_status&page` | `channel.listing.read` | 平台商品与映射状态 |
| POST | `/tenants/{tenant_id}/listings/sync` | `channel.listing.map` | `{store_id}` → 202 `{task_id}` |
| POST | `/tenants/{tenant_id}/listings/{listing_id}/link` | `channel.listing.map` | `{product_id, sku_id}`；SKU 必须属于本租户 |
| POST | `/tenants/{tenant_id}/listings/{listing_id}/unlink` | `channel.listing.map` | 解除映射 |

* `GET /tenants/{tenant_id}/listings` 的 `mapping_status` 过滤值取 `mapped` / `pending` / `ambiguous` / `missing`，另支持 `unmapped`（尚无映射记录）；不传返回全部。列表、总数与筛选使用同一店铺数据范围。
* 平台商品同步后，本次平台未返回的历史商品标记 `status=inactive`（stale），映射关系保留、不物理删除；人工映射 `mapping_source=manual` 不被自动匹配覆盖。

### 订单、任务、审计

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/tenants/{tenant_id}/orders?page&status&store_id&q&date_from&date_to` | `order.read` | 列表（按店铺数据范围过滤） |
| GET | `/tenants/{tenant_id}/orders/{order_id}` | `order.read` | 详情：明细、状态历史、库存影响 |
| POST | `/tenants/{tenant_id}/orders/sync` | `order.sync` | `{store_id?}` → 202 `{task_id}` |
| GET | `/tenants/{tenant_id}/sync-runs?page&job_type&status` | `task.read` | 任务记录 |
| GET | `/tenants/{tenant_id}/sync-runs/{id}` | `task.read` | 任务详情 |
| POST | `/tenants/{tenant_id}/sync-runs/{id}/retry` | `task.read` + 原任务业务权限及店铺范围 | 重试 → 202，保留当前发起人 |
| GET | `/tenants/{tenant_id}/sync-schedules` | `task.read` | 定时同步配置 |
| PUT | `/tenants/{tenant_id}/sync-schedules/{id}` | `task.schedule.manage` + 对应任务业务权限及范围 | `{enabled, interval_minutes}` |
| GET | `/tenants/{tenant_id}/sync-settings` | `tenant.setting.manage` + 全部范围 | `{order_sync_enabled, stock_push_enabled}` |
| PUT | `/tenants/{tenant_id}/sync-settings` | `tenant.setting.manage` + 全部范围 | 修改同步总开关；开启时同时检查对应业务全部范围，写审计并递增权限版本 |
| GET | `/tenants/{tenant_id}/tasks/{task_id}` | `task.read` + 店铺范围 | 仅查询租户库已登记数字任务 ID；无归属 Celery UUID 返回 404 |
| GET | `/tenants/{tenant_id}/audit-logs?page&action` | `audit.read` | 租户审计 |

### 平台回调（不使用用户会话）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/integrations/authorization/{token}` | 可复制的长期入口：校验邀请及发起人当前授权，生成短期一次性 state，302 到平台；必要时恢复待保存结果 |
| GET | `/integrations/authorization-selection?token&shop_id` | TikTok 多店铺选择，校验一次性选择凭据并只接受平台已返回的店铺 |
| GET | `/integrations/tiktok/oauth/callback?code&state` | 消费短期 state 后换取凭据；多店铺进入选择页面，单店铺保存后跳转前端 |
| GET | `/tiktok/auth/callback?code&state` | TikTok 历史回调路径兼容入口，使用同样的 state 校验 |
| GET | `/integrations/shopee/oauth/callback?code&shop_id&state` | 校验一次性授权会话后换取 Token，成功 302 跳转前端店铺页；失败 302 带错误码 |
| POST | `/integrations/shopee/webhook` | Shopee 推送：验签 + 事件防重后写 `webhook_events`，返回 200；无效签名 401 |

* Webhook 语义：签名校验失败返回 `401 CHANNEL_SIGNATURE_INVALID`；同一事件按 `(tenant_id, channel, external_event_id)` 幂等，重复推送返回 `200` 且不再触发业务处理。
* 租户暂停期间：已验签推送只暂存 `webhook_events`（`status=received`），不触发订单同步、不改变库存；重新启用后由补偿任务处理。
* 未处理超过 2 分钟的订单类事件由渠道补偿任务安全重投（最多 5 次），仍失败标记 `status=failed`，等待人工处理。

## 权限编码

`catalog.product.read/create/update`、`catalog.cost.read`、`inventory.read/adjust`、`store.connect/read`、
`channel.listing.read/map`、`order.read/sync`、`stock.push`、`member.read/invite`、`role.manage/assign`、
`audit.read`、`task.read`、`task.schedule.manage`、`tenant.setting.manage`。

## 前端行为要求

* 每个请求显式携带目标租户（路径中的 `tenant_id`）。
* 租户切换时：取消旧请求（AbortController）、清空旧数据与勾选、重新加载 bootstrap 权限、丢弃迟到的旧租户响应。
* 不同标签页可同时处理不同租户（不把租户存全局单例）。
* 按钮 / 菜单按有效权限显示，但后端始终独立授权。
## 任务与授权边界

* 任务列表、总数、详情、状态和重试均检查租户及店铺范围；全租户任务仅对显式全部范围可见。
* 定时任务配置写入使用单独权限；只读角色不自动获得管理能力。
* 手动任务不与自动化或其他用户的 pending 任务合并，Worker 缺失原始发起人时拒绝执行。
* TenantSettings 的同步总开关是最终门禁，优先于 FeatureSetting；关闭开关后，尚未执行的 Outbox/Worker 不能继续调用相关平台接口。
* 长期授权入口保持可复制，重新生成会撤销旧入口；旧的永久签名 state 不再直接用于回调。真实 state 仅有效 600 秒且原子消费。
* 已连接 ERP 店铺只能重新授权同一外部身份，替换店铺必须走明确业务操作。跨库保存失败时使用加密恢复记录，不暴露令牌。
