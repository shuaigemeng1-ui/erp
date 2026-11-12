# 数据模型基线（首版）

约定：

* 每张表都有 `id BIGINT 自增主键`、`created_at`、`updated_at`（UTC）。
* 所有表/字段必须带中文注释；禁止外键；业务唯一性用唯一约束。
* 租户库每张表都带 `tenant_id`（便于审计、队列、缓存定位），但跨租户隔离由数据库本身承担。
* 金额/汇率 `DECIMAL(18,4)` / `DECIMAL(18,8)`；ID 在 JSON 中序列化为字符串。
* 状态字段统一 `VARCHAR(32)`，取值在代码中定义枚举，不用数据库 ENUM。

## 中心库（`CenterBase`）

| 表 | 归属模块 | 关键字段 |
| --- | --- | --- |
| `users` | identity | email(唯一), password_hash, display_name, status(active/disabled), locale, last_login_at |
| `tenants` | identity | code(唯一), name, status(provisioning/active/suspended/provision_failed), timezone, plan_id, owner_user_id, suspended_at |
| `tenant_memberships` | identity | tenant_id, user_id, status(active/invited/disabled), is_owner, joined_at；唯一(tenant_id,user_id) |
| `tenant_invitations` | identity | tenant_id, email, token_hash, role_ids(JSON), status(pending/accepted/revoked/expired), expires_at, invited_by, accepted_at |
| `tenant_databases` | identity | tenant_id(唯一), host, port, database_name, username, password_cipher, credentials_version, schema_version, status |
| `provisioning_jobs` | identity | tenant_id, step, status(pending/running/succeeded/failed), attempts, error_message, started_at, finished_at |
| `platform_roles` | identity | code(唯一), name |
| `platform_user_roles` | identity | user_id, role_id；唯一(user_id,role_id) |
| `plans` | platform | code(唯一), name, status, description |
| `plan_features` | platform | plan_id, feature_code, limit_value；唯一(plan_id,feature_code) |
| `subscriptions` | platform | tenant_id, plan_id, status, starts_at, ends_at |
| `platform_audit_logs` | platform | actor_user_id, action, resource_type, resource_id, tenant_id, summary, payload(JSON), ip, request_id |
| `platform_channel_apps` | channels | channel, environment(production/test), partner_id, partner_key_cipher, partner_secret_cipher, app_key, app_secret_cipher, app_id, region, redirect_url, status；唯一(channel,environment) |
| `channel_account_registry` | channels | channel, environment, external_account_id, tenant_id, store_id, status(bound/released), bound_at, released_at；唯一(channel,environment,external_account_id) |

| `channel_authorization_links` | channels | tenant_id, member_id, store_id, channel, token_hash(唯一), status(active/revoked/pending_save), context(JSON), result_cipher；长期链接只存摘要，跨库保存失败时暂存加密结果 |

## 租户库（`TenantBase`）

### 权限与设置（identity / access）

| 表 | 关键字段 |
| --- | --- |
| `roles` | tenant_id, code, name, description, is_system, copied_from_role_id, status；唯一(tenant_id,code) |
| `role_permissions` | tenant_id, role_id, permission_code；唯一(tenant_id,role_id,permission_code) |
| `member_roles` | tenant_id, role_id, member_id(中心库成员 ID), assigned_by；唯一(tenant_id,member_id,role_id) |
| `role_binding_scopes` | tenant_id, member_role_id, scope_type(all/stores/stock_places), scope_id；唯一(tenant_id,member_role_id,scope_type,scope_id)；索引(member_role_id) |
| `tenant_settings` | tenant_id(唯一), permission_version, timezone, order_sync_enabled, stock_push_enabled |
| `feature_settings` | tenant_id, feature_code, enabled, config(JSON)；唯一(tenant_id,feature_code) |

### 审计与支撑（audit）

| 表 | 关键字段 |
| --- | --- |
| `audit_logs` | tenant_id, actor_id, action, resource_type, resource_id, summary, payload(JSON), ip, request_id |
| `files` | tenant_id, object_key, filename, content_type, size_bytes, uploaded_by |
| `idempotency_records` | tenant_id, idempotency_key, endpoint, request_hash, status(processing/completed), response_code, response_body(JSON), expires_at；唯一(tenant_id,idempotency_key) |

### 商品与库存（catalog / inventory）

| 表 | 关键字段 |
| --- | --- |
| `products` | tenant_id, code, name, brand, category, status(active/archived), cost_amount, cost_currency, base_price, description；唯一(tenant_id,code) |
| `product_skus` | tenant_id, product_id, sku_code, name, specs(JSON), price, cost_amount, status；唯一(tenant_id,sku_code)；索引(product_id) |
| `product_images` | tenant_id, product_id, sku_id(可空), url, sort_order |
| `stock_places` | tenant_id, code, name, is_default, status；唯一(tenant_id,code) |
| `inventory_balances` | tenant_id, sku_id, stock_place_id, available_quantity, reserved_quantity, version；唯一(tenant_id,sku_id,stock_place_id) |
| `stock_movements` | tenant_id, sku_id, stock_place_id, quantity_change, balance_after, movement_type, reference_type, reference_id, operator_id, note；索引(tenant_id,sku_id,created_at) |

### 店铺与平台商品（channels）

| 表 | 关键字段 |
| --- | --- |
| `stores` | tenant_id, channel, name, external_store_id, region, timezone, store_code, environment, warehouse_id, status(pending_authorization/connected/reauth_required/disconnected), authorized_at, last_synced_at；唯一(tenant_id,channel,external_store_id) |
| `store_credentials` | tenant_id, store_id(唯一), access_token_cipher, refresh_token_cipher, shop_cipher_cipher, access_token_expires_at, refresh_token_expires_at, status |
| `channel_listings` | tenant_id, store_id, external_item_id, external_sku_id, title, price, currency, remote_stock, raw(JSON), status(active/inactive/mapping_pending), synced_at；唯一(tenant_id,store_id,external_item_id,external_sku_id) |
| `channel_listing_links` | tenant_id, listing_id(唯一), product_id, sku_id, link_status(mapped/pending/ambiguous/missing), mapping_source(auto/manual), created_by |

### 订单、库存影响与任务（sales / tasks）

| 表 | 关键字段 |
| --- | --- |
| `orders` | tenant_id, store_id, channel, external_order_no, status(normalized), platform_status, currency, total_amount, buyer_name, placed_at, raw_id, abnormal_reason；唯一(tenant_id,store_id,external_order_no) |
| `order_items` | tenant_id, order_id, external_item_id, external_sku_id, sku_id(可空), quantity, unit_price, discount_amount, item_status, mapping_status；索引(order_id) |
| `order_status_history` | tenant_id, order_id, from_status, to_status, platform_status, source, changed_at |
| `platform_order_raw` | tenant_id, channel, store_id, external_order_no, payload(JSON), fetched_at；索引(tenant_id,store_id,external_order_no) |
| `order_stock_effects` | tenant_id, order_id, order_item_id, sku_id, stock_place_id, applied_quantity, target_quantity, status(pending/applied/skipped/failed), last_task_id；唯一(tenant_id,order_item_id,stock_place_id) |
| `order_sync_checkpoints` | tenant_id, store_id, channel, environment, watermark, window_start, window_end, cursor；唯一(tenant_id,store_id,channel,environment)，只有完整成功才推进 watermark |
| `sync_schedules` | tenant_id, job_type, store_id(可空), interval_minutes, enabled, last_run_at, next_run_at；唯一(tenant_id,job_type,store_id) |
| `sync_runs` | tenant_id, job_type, store_id, status(pending/running/succeeded/failed/partial), triggered_by(manual/schedule/webhook/system), requested_by, started_at, finished_at, total_processed, error_message, cursor |
| `webhook_events` | tenant_id, channel, store_id, event_type, external_event_id, signature_ok, status(received/processed/ignored/failed), payload(JSON), received_at, processed_at；唯一(tenant_id,channel,external_event_id) |
| `outbox_events` | tenant_id, event_type, aggregate_type, aggregate_id, payload(JSON), status(pending/dispatched/failed), attempts, available_at, dispatched_at, last_error；索引(status,available_at) |

## 迁移基线

以下是文档对照；实际版本图由 `ScriptDirectory` 发现，脚本必须检查全部迁移，文档遗漏即失败。

| 数据库 | 文件 | 修订号 | 内容 |
| --- | --- | --- | --- |
| 中心库 | `alembic/center/versions/0001_center_identity.py` | center_0001 | 中心库基线：身份、租户、开通、平台套餐与平台审计（center_0001）。 |
| 中心库 | `alembic/center/versions/0002_center_channels.py` | center_0002 | 中心库迁移：平台渠道应用与外部店铺归属登记。 |
| 中心库 | `alembic/center/versions/0003_center_tiktok_app.py` | center_0003 | 中心库迁移：平台渠道应用增加 TikTok Shop 的 App Key / App Secret 字段。 |
| 中心库 | `alembic/center/versions/0004_center_tiktok_app_id.py` | center_0004 | 中心库迁移：平台渠道应用增加 TikTok Shop App ID。 |
| 中心库 | `alembic/center/versions/0005_center_tiktok_region.py` | center_0005 | 中心库迁移：TikTok Shop 应用增加授权市场 region（ROW/US）。 |
| 中心库 | `alembic/center/versions/0006_center_authorization_links.py` | center_0006 | 中心库：可撤销授权邀请和跨库写入恢复记录，禁止外键。 |
| 租户库 | `alembic/tenant/versions/0001_tenant_identity.py` | tenant_0001 | 租户库基线：角色、权限、绑定范围、租户设置、功能开关、审计与支撑表（tenant_0001）。 |
| 租户库 | `alembic/tenant/versions/0002_tenant_catalog.py` | tenant_0002 | 租户库：商品、SKU、图片、库存地点、库存余额、库存流水。 |
| 租户库 | `alembic/tenant/versions/0003_tenant_channels.py` | tenant_0003 | 租户库迁移：店铺、店铺凭据、平台商品与 SKU 映射。 |
| 租户库 | `alembic/tenant/versions/0004_tenant_sales.py` | tenant_0004 | 租户库迁移：订单、订单明细、状态历史、平台原始数据、订单库存影响、任务与 Outbox。 |
| 租户库 | `alembic/tenant/versions/0005_tenant_tiktok_store_cipher.py` | tenant_0005 | 租户库迁移：店铺凭据增加 TikTok Shop shop_cipher 密文字段。 |
| 租户库 | `alembic/tenant/versions/0006_tenant_store_warehouse.py` | tenant_0006 | 租户库迁移：stores 增加 TikTok Shop 仓库ID（库存推送使用）。 |
| 租户库 | `alembic/tenant/versions/0007_tenant_store_code.py` | tenant_0007 | 租户库迁移：stores 增加 ERP 店铺编码，支持先建店铺后授权。 |
| 租户库 | `alembic/tenant/versions/0008_tenant_task_schedule_permission.py` | tenant_0008 | 仅为系统所有者和管理员补齐定时任务管理权限。无外键、无新增业务表。 |
| 租户库 | `alembic/tenant/versions/0009_tenant_order_sync_checkpoints.py` | tenant_0009 | 订单同步检查点按店铺、渠道、环境隔离；不使用外键。 |
| 租户库 | `alembic/tenant/versions/0010_tenant_restore_store_code_comment.py` | tenant_0010 | 修复旧 MODIFY 丢失的店铺编码中文注释，不使用外键。 |

模型业务表：中心库 15 张、租户库 29 张（均不计 Alembic 版本表）。当前末端为中心库 `center_0006`、租户库 `tenant_0010`。

`tenant_0008` 仅补授系统 owner/admin 的 `task.schedule.manage`，不扩张普通角色。`tenant_0010` 修复旧 MODIFY 丢失的 store_code 中文注释，适用于现有数据库的增量升级。
