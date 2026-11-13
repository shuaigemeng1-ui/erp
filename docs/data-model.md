# 数据模型基线（含 SPU—SKU、平台对象存储与统一运维中心）

约定：

* 常规表都有 `id BIGINT 自增主键`、`created_at`、`updated_at`（UTC）。
* **追加型表例外**：只追加、不原地修改的历史/样本表不要求 `updated_at`，允许以下表缺省：
  `node_metric_samples`、`node_checks`、`operation_job_steps`、`backup_records`（缺 `created_at`/`updated_at`），
  `node_metric_aggregates`（缺 `updated_at`，保留 `created_at`）。
  它们以业务时间字段作为追加时间：`sampled_at`（指标样本）、`checked_at`（公网探测）、
  `started_at`/`finished_at`（任务步骤、备份）、`bucket_start`（指标聚合）。
  其余表仍必须同时包含 `id`、`created_at`、`updated_at`。
* 所有表/字段必须带中文注释；禁止外键；业务唯一性用唯一约束。
* 租户库每张表都带 `tenant_id`（便于审计、队列、缓存定位），但跨租户隔离由数据库本身承担。
* 金额/汇率 `DECIMAL(18,4)` / `DECIMAL(18,8)`；ID 在 JSON 中序列化为字符串。
* 状态字段统一 `VARCHAR(32)`，取值在代码中定义枚举，不用数据库 ENUM。

## 中心库（`CenterBase`）

| 表 | 归属模块 | 关键字段 |
| --- | --- | --- |
| `users` | identity | email(唯一), password_hash, display_name, status(active/disabled), locale, last_login_at |
| `tenants` | identity | code(唯一), name, status(provisioning/active/suspended/provision_failed), timezone, plan_id, owner_user_id, suspended_at, next_member_no(下一租户内成员编号，初始1) |
| `tenant_memberships` | identity | tenant_id, user_id, member_no(租户内成员编号，从1递增), status(active/invited/disabled), is_owner, joined_at；唯一(tenant_id,user_id)及(tenant_id,member_no)，id保留全局内部关联 |
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
| `storage_profiles` | storage | name(唯一), provider(oss/cos/obs/s3/s3_compatible), endpoint, bucket, region, access_key_id_cipher, secret_access_key_cipher, public_base_url, key_prefix, addressing_style, enabled, version；平台统一配置，访问密钥ID和Secret均加密且不回显 |
| `platform_system_settings` | storage | 固定id=1，default_storage_profile_id(逻辑关联), version；当前默认存储账号，切换时锁定单例行 |
| `email_profiles` | email | name(唯一), provider(aliyun_directmail/tencent_ses), region, from_address, from_name, access_key_id_cipher, secret_access_key_cipher, enabled, is_default, version；AccessKey/Secret 加密保存，接口不回显明文 |

### 统一运维中心（ops）

中心库运维表均无外键，实例/节点/任务之间只存逻辑 ID；追加型表的例外见文首“约定”。

| 表 | 归属模块 | 关键字段 |
| --- | --- | --- |
| `tenant_instances` | ops | tenant_id(唯一), name, domain(唯一), environment, status(pending/active/suspended), app_version, actual_version, target_version, agent_version, executor_version, db_version, upgrade_status(pending/building/maintenance/migrating/verifying/succeeded/failed/unknown), notes |
| `server_nodes` | ops | instance_id, tenant_id, node_uuid(唯一), hostname, os_name, arch, cpu_cores, memory_total_mb, disk_total_gb, internal_ips(JSON), configured_public_ip, observed_public_ip, agent_version, executor_version, protocol_version, capabilities_json(JSON), db_version, app_version, status(pending/online/offline/unknown), enrolled_at, last_seen_at, last_heartbeat_at, last_metrics_at, token_hash(唯一), token_version, enrollment_expires_at, ssh_host, ssh_port, ssh_username_cipher, ssh_password_cipher；索引(instance_id)、(last_seen_at,status) |
| `release_versions` | ops | version(唯一), image_tag, image_digest, status(draft/active/retired), commit_sha(完整40/64位hex，旧记录为空), build_config_summary, min_agent_protocol, center_db_version, tenant_db_version, rollback_compatible(旧应用兼容声明), build_status(built/verified/failed), notes, created_by；负责升级目标版本可信登记 |
| `operation_jobs` | ops | instance_id, node_id(可空), job_type(deploy/upgrade/restart/backup/logs/config), status(pending/leased/running/succeeded/failed/unknown), idempotency_key(唯一), target_version, actual_version, agent_version, agent_protocol_version, db_version, upgrade_status, attempt_id, lease_token, last_heartbeat_at, event_seq, checks_json(数据库版本/服务readiness/业务只读校验结果), checks_status(passed/failed/partial/missing/空), payload_json, result_json, error, attempts, lease_expires_at, requested_by, started_at, finished_at；索引(instance_id,status)、(node_id,status) |
| `operation_job_steps` | ops | job_id, step_no, name, status(pending/running/succeeded/failed/skipped), message, started_at, finished_at；唯一(job_id,step_no) |
| `node_metric_samples` | ops | node_id, sampled_at, received_at, cpu_percent, load1/load5/load15, memory_total_mb, memory_used_mb, memory_available_mb, memory_percent, swap_total_mb, swap_used_mb, disks(JSON), network(JSON), services(JSON), public_ip；索引(received_at)、(node_id,sampled_at)；追加型，默认保留7天 |
| `node_metric_aggregates` | ops | node_id, resolution(5m/1h), bucket_start, sample_count, cpu_avg/cpu_max, memory_avg/memory_max, load_avg, disk_max, created_at；唯一(bucket_start,node_id,resolution)；追加型，不要求updated_at |
| `node_checks` | ops | node_id, checked_at, target(root/healthz/api), url, ok, status_code, latency_ms, cert_days_left, error；索引(node_id,checked_at)；追加型 |
| `alert_events` | ops | node_id, instance_id, alert_type, severity(warning/critical), status(open/acknowledged/resolved), message, metric_value, threshold, first_seen_at, last_seen_at, acknowledged_at, resolved_at, notify_count, last_notified_at；索引(node_id,status)、(last_seen_at) |
| `backup_records` | ops | instance_id, node_id(可空), job_id(可空), status(running/succeeded/failed), storage_key, size_bytes, started_at, finished_at, retention_until, error；索引(instance_id,started_at)；追加型 |
| `maintenance_windows` | ops | instance_id, starts_at, ends_at, reason, created_by；索引(instance_id,starts_at,ends_at) |

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
| `tenant_local_users` | tenant_id, source_user_id(来源中心用户ID), email, password_hash, display_name, member_no, is_owner, status, synced_at(最近同步UTC)；唯一(tenant_id,email)、(tenant_id,source_user_id)；独立实例模式下的本地登录账号，与中心账号逻辑关联、无外键 |

### 审计与支撑（audit）

| 表 | 关键字段 |
| --- | --- |
| `audit_logs` | tenant_id, actor_id, actor_name_snapshot, product_id, sku_id, action, resource_type, resource_id, summary, payload(JSON字段旧值/新值), ip, request_id；索引(tenant_id,product_id,created_at,id) |
| `files` | tenant_id, object_key, filename, content_type, size_bytes, uploaded_by, storage_provider(local/oss/cos/obs/s3/s3_compatible), storage_profile_id(中心库逻辑ID，无外键), url(VARCHAR2048，上传时保存的长期地址) |
| `idempotency_records` | tenant_id, idempotency_key, endpoint, request_hash, status(processing/completed), response_code, response_body(JSON), expires_at；唯一(tenant_id,idempotency_key) |

### 商品与库存（catalog / inventory）

| 表 | 关键字段 |
| --- | --- |
| `products` | tenant_id, code(SPU编号), name(中文名), name_en, preferred_supplier_id, purchaser_id/developer_id/inquiry_buyer_id(租户成员ID), hs_code, material_zh/material_en, usage, brand, category, feature_tags(JSON), packaging_name, length_cm/width_cm/height_cm, package_length_cm/package_width_cm/package_height_cm, sales_type, remark, features, version, status(active/archived), cost_amount(历史保留), cost_currency, base_price, description；唯一(tenant_id,code) |
| `product_skus` | tenant_id, product_id, sku_code, name, specs(JSON键值对), price, cost_amount(当前成本), guidance_cost, average_cost, weight_g, image_url(VARCHAR2048), sales_status(on_sale/discontinued/trial/clearance), version, status(active/archived)；唯一(tenant_id,sku_code)；索引(product_id) |
| `sku_identifiers` | tenant_id, sku_id, identifier(原文), normalized_identifier(strip+casefold，utf8mb4_bin), kind(primary/alias), is_active；唯一(tenant_id,normalized_identifier)，主编号和别名共用唯一空间，停用记录保留 |
| `suppliers` | tenant_id, code, name, contact_name(选填联系人), contact_details(选填联系方式), remark(选填备注), status；唯一(tenant_id,code)，商品首选供应商简档 |
| `catalog_options` | tenant_id, kind(brand/category/tag/packaging), name, parent_id, status；唯一(tenant_id,kind,name)，分类父级同租户服务校验 |
| `product_images` | tenant_id, product_id, sku_id(可空), url(VARCHAR2048), sort_order |
| `stock_places` | tenant_id, code, name, is_default, status；唯一(tenant_id,code) |
| `inventory_balances` | tenant_id, sku_id, stock_place_id, available_quantity, reserved_quantity, version；唯一(tenant_id,sku_id,stock_place_id) |
| `stock_movements` | tenant_id, sku_id, stock_place_id, quantity_change, balance_after, movement_type, reference_type, reference_id, operator_id, note；索引(tenant_id,sku_id,created_at) |

### 店铺与平台商品（channels）

| 表 | 关键字段 |
| --- | --- |
| `stores` | tenant_id, channel, name, external_store_id, region, timezone, store_code, environment, warehouse_id, status(pending_authorization/connected/reauth_required/disconnected), authorized_at, last_synced_at；唯一(tenant_id,channel,external_store_id) |
| `store_credentials` | tenant_id, store_id(唯一), access_token_cipher, refresh_token_cipher, shop_cipher_cipher, access_token_expires_at, refresh_token_expires_at, status |
| `channel_listings` | tenant_id, store_id, external_item_id, external_sku_id(VARCHAR128), title, price, currency, remote_stock, raw(JSON), status(active/inactive/mapping_pending), synced_at；唯一(tenant_id,store_id,external_item_id,external_sku_id) |
| `channel_listing_links` | tenant_id, listing_id(唯一), product_id, sku_id, link_status(mapped/pending/ambiguous/missing), mapping_source(auto/manual), created_by |

### 订单、库存影响与任务（sales / tasks）

| 表 | 关键字段 |
| --- | --- |
| `orders` | tenant_id, store_id, channel, external_order_no, status(normalized), platform_status, currency, total_amount, buyer_name, placed_at, raw_id, abnormal_reason；唯一(tenant_id,store_id,external_order_no) |
| `order_items` | tenant_id, order_id, external_item_id, external_sku_id(历史卖家编码), platform_sku_id(原生平台SKU ID), sku_id(可空), quantity, unit_price, discount_amount, item_status, mapping_status；索引(order_id) |
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
| 中心库 | `alembic/center/versions/0007_center_system_storage.py` | center_0007 | 平台系统配置与多个云对象存储账号，凭据密文保存，固定默认配置行，无外键。 |
| 中心库 | `alembic/center/versions/0008_center_email_profiles.py` | center_0008 | 平台邮件服务账号：阿里云 DirectMail / 腾讯云 SES，密钥密文保存，默认账号唯一。 |
| 中心库 | `alembic/center/versions/0009_center_tenant_member_numbers.py` | center_0009 | 按租户历史创建顺序回填member_no并初始化计数器，租户内从1递增，原内部ID与业务关联保留，无外键。 |
| 中心库 | `alembic/center/versions/0010_center_ops.py` | center_0010 | 统一运维管理：租户实例、服务器节点、节点指标样本、公网探测与告警事件；无外键。 |
| 中心库 | `alembic/center/versions/0011_center_ops_operations.py` | center_0011 | 发布版本、运维任务与任务步骤、备份记录；任务幂等键与租约字段；无外键。 |
| 中心库 | `alembic/center/versions/0012_center_ops_maintenance.py` | center_0012 | 实例维护窗口：按实例配置维护开始/结束时间与原因；无外键。 |
| 中心库 | `alembic/center/versions/0013_center_ops_cert.py` | center_0013 | 公网探测记录增加 HTTPS 证书剩余天数字段；无外键。 |
| 中心库 | `alembic/center/versions/0014_center_ops_metrics_agg.py` | center_0014 | 节点指标 5 分钟/小时聚合表（bucket_start、样本数与 CPU/内存/负载/磁盘统计）；无外键。 |
| 中心库 | `alembic/center/versions/0015_center_server_ssh.py` | center_0015 | 服务器节点 SSH 主机/端口及用户名、密码密文字段；无外键。 |
| 中心库 | `alembic/center/versions/0016_center_release_upgrade.py` | center_0016 | 批次 1：发布版本清单与升级校验字段（完整 commit、构建配置摘要、最低 Agent 协议、center/tenant DB 目标版本、回退兼容、构建状态），实例/节点实际版本与升级状态，任务执行批次/租约/心跳/事件水位/升级检查结果；全部新增列可空或带默认值，无外键。center_0016 随批次 1 发布生效。 |
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
| 租户库 | `alembic/tenant/versions/0011_tenant_spu_sku_catalog.py` | tenant_0011 | SPU资料、SKU成本重量与状态、统一编号别名、供应商、商品字典、字段操作日志及平台SKU编码扩容与订单原生SKU标识。 |
| 租户库 | `alembic/tenant/versions/0012_tenant_cloud_file_urls.py` | tenant_0012 | files记录云厂商、配置ID和长期URL；商品及SKU图片URL扩至2048，旧文件保留local，无外键。 |
| 租户库 | `alembic/tenant/versions/0013_tenant_supplier_contacts.py` | tenant_0013 | 供应商增加选填联系人、联系方式和备注，保留原供应商及商品关联，无外键。 |
| 租户库 | `alembic/tenant/versions/0014_tenant_local_users.py` | tenant_0014 | 租户本机账号表（独立实例模式本地登录）：来源中心用户ID、邮箱、密码哈希、成员编号、负责人标记与同步时间；唯一(tenant_id,email)、(tenant_id,source_user_id)，无外键。 |

模型业务表：中心库 29 张、租户库 33 张（均不计 Alembic 版本表），完整版本图共 30 个迁移文件（center 16 + tenant 14）。当前末端为中心库 `center_0016`（批次 1 发布后生效）、租户库 `tenant_0014`。

追加型表例外：`node_metric_samples`、`node_checks`、`operation_job_steps`、`backup_records`、`node_metric_aggregates` 按文首“约定”不要求完整三时间列，以 `sampled_at`/`checked_at`/`started_at`/`bucket_start` 等业务时间字段作为追加时间；其余业务表仍必须包含 `id`、`created_at`、`updated_at`。

`tenant_0008` 仅补授系统 owner/admin 的 `task.schedule.manage`，不扩张普通角色。`tenant_0010` 修复旧 MODIFY 丢失的 store_code 中文注释，适用于现有数据库的增量升级。

## SPU / SKU 事实来源与升级约束

- 主商品 `code/name` 分别作为 SPU 编号/中文名；现有商品、SKU、库存与订单关联主键保持。
- SPU 成本和重量由未归档 SKU 各字段独立汇总；停售 SKU 仍参与。所有值一致显示数值，不同值显示多个价格/重量；缺失与零分别处理，不计算 SKU 间平均值。
- SKU 三类成本本轮人工维护，币种使用所属 SPU 的 `cost_currency`；SPU 历史 `cost_amount` 不再是新增业务事实来源。
- SPU 销售状态全部一致时原样显示，否则优先 `partial_clearance`，再 `partial_discontinued`，在售+试卖归为 `on_sale`。归档生命周期状态与销售状态分开。
- `tenant_0011` 在 DDL 前预检历史 SKU 编号标准化冲突；在线迁移按 Python `strip().casefold()` 登记唯一主编号，不能静默跳过冲突。离线 SQL 用于结构审阅；非 ASCII 历史编号回填应执行在线 Alembic 迁移。
- 历史 SKU 当前成本为空且主商品有成本时才回填；指导成本、平均成本、重量没有可靠历史值时保持空。新增销售类型默认 normal，旧有效 SKU 为 on_sale，归档 SKU 为 discontinued 并保留归档状态。
- 新上传图片使用当前默认云对象存储，`files` 保存完整对象键、厂商、配置ID及长期URL，商品图与SKU图保存返回的URL。历史local文件继续使用原持久化目录并经鉴权读取；图片关联校验租户归属。
- 任何已支持的商品修改入口由同一服务写字段差异日志，业务数据、版本和日志同事务提交；旧日志没有旧值时不补造差异。

## 对象存储配置与文件地址

- 系统配置由平台管理员统一维护，支持阿里云OSS、腾讯云COS、华为云OBS、AWS S3和S3兼容服务；租户不能读取平台密钥或选择任意上传Endpoint。
- 默认存储通过固定系统配置行串行切换，修改账号和默认配置均检查version。默认账号不能直接停用；新账号保存后须显式设为默认。
- AccessKey ID与Secret密文存入中心库，接口只返回has_access_key/has_secret_key。相同厂商编辑留空保留旧值，切换厂商时必须重新提供两项凭据。
- 上传对象键带租户目录和随机标识，账号可配置额外key_prefix。上传成功后保存当时的长期HTTPS URL；修改账号或切换默认只影响新上传，不重写历史文件地址。
- public_base_url必须由管理员配置为长期可访问的桶域名或CDN地址；读权限与域名由云厂商配置。本系统不自动放开桶权限，不将临时签名URL保存为商品图片地址。
- 未设置默认账号、账号不可用或上传失败时明确返回错误；新上传不回落本地。既有本地卷与文件不删除，云端失败也不创建成功文件记录。
- tenant_0012只扩展元数据和URL长度，不批量迁移旧本地图片；回退前检查长URL和云文件记录，防止删除仍需使用的云存储信息。真实云连通性与权限必须用实际账号验收。
