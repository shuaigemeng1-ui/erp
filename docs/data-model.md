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
| `platform_channel_apps` | channels | channel, environment(production/test), partner_id, partner_key_cipher, partner_secret_cipher, redirect_url, status；唯一(channel,environment) |
| `channel_account_registry` | channels | channel, environment, external_account_id, tenant_id, store_id, status(bound/released), bound_at, released_at；唯一(channel,environment,external_account_id) |

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
| `stores` | tenant_id, channel, name, external_store_id, region, timezone, status(connected/reauth_required/disconnected), authorized_at, last_synced_at；唯一(tenant_id,channel,external_store_id) |
| `store_credentials` | tenant_id, store_id(唯一), access_token_cipher, refresh_token_cipher, access_token_expires_at, refresh_token_expires_at, status |
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
| `sync_schedules` | tenant_id, job_type, store_id(可空), interval_minutes, enabled, last_run_at, next_run_at；唯一(tenant_id,job_type,store_id) |
| `sync_runs` | tenant_id, job_type, store_id, status(pending/running/succeeded/failed/partial), triggered_by(manual/schedule/webhook/system), requested_by, started_at, finished_at, total_processed, error_message, cursor |
| `webhook_events` | tenant_id, channel, store_id, event_type, external_event_id, signature_ok, status(received/processed/ignored/failed), payload(JSON), received_at, processed_at；唯一(tenant_id,channel,external_event_id) |
| `outbox_events` | tenant_id, event_type, aggregate_type, aggregate_id, payload(JSON), status(pending/dispatched/failed), attempts, available_at, dispatched_at, last_error；索引(status,available_at) |

## 迁移基线

| 数据库 | 文件 | 修订号 | 内容 |
| --- | --- | --- | --- |
| 中心库 | `alembic/center/versions/0001_center_identity.py` | center_0001 | users/tenants/memberships/invitations/tenant_databases/provisioning_jobs/platform_roles/platform_user_roles/plans/plan_features/subscriptions/platform_audit_logs |
| 中心库 | `alembic/center/versions/0002_center_channels.py` | center_0002 | platform_channel_apps/channel_account_registry |
| 租户库 | `alembic/tenant/versions/0001_tenant_identity.py` | tenant_0001 | roles/role_permissions/member_roles/role_binding_scopes/tenant_settings/feature_settings/audit_logs/files/idempotency_records |
| 租户库 | `alembic/tenant/versions/0002_tenant_catalog.py` | tenant_0002 | products/product_skus/product_images/stock_places/inventory_balances/stock_movements |
| 租户库 | `alembic/tenant/versions/0003_tenant_channels.py` | tenant_0003 | stores/store_credentials/channel_listings/channel_listing_links |
| 租户库 | `alembic/tenant/versions/0004_tenant_sales.py` | tenant_0004 | orders/order_items/order_status_history/platform_order_raw/order_stock_effects/sync_schedules/sync_runs/webhook_events/outbox_events |

迁移文件中的列类型、可空性、默认值、注释必须与模型一致；`down_revision` 按上表顺序串联。
