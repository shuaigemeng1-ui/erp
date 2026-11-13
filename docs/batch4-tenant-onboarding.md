# 第二独立租户接入模板与迁移白名单（批次 4）

- 日期：2026-11-13；代码基线：`5c9dd53`（`fix(deploy): 传递租户/实例与 Worker 绑定环境变量`）
- 角色：deploy-qa（task-36）；**本批次只新增 deploy/scripts、backend/scripts、docs 模板，未连接/修改任何生产环境**
- 目录：`deploy/scripts/tenant-onboarding/`；工具：`backend/scripts/tenant_migration_whitelist.py`
- 关联：批次 2 租户模式边界（`docs/batch2-acceptance.md`）、批次 3 备份（`docs/backup-restore.md`）、
  恢复演练事故与闭环（`docs/incident-2026-11-13-restore-drill.md`，记录链已闭环）

> 本文件是“第二个独立租户”的可复制接入手册。真实域名切换、OAuth 回调重登记、店铺凭据迁移、
> Agent2 安装与 SSH 首指纹确认**尚未在第二租户真实执行**；执行前必须经 Lead 审批并完成第 4/5 节停写与对账。
> 恢复演练脚本旧版曾造成事故；修复版已在新备份通过 28 项检查，094919Z 演练完成记录闭环
> （`backup_records id=2`、`restore_status=verified`）；**`BACKUP_INSTANCE_ID` 必配**（详见
> `docs/incident-2026-11-13-restore-drill.md` 第 9 节与 `docs/backup-restore.md` 第 0.1 节）。

## 1. 目标与边界

1. 把新租户接入为**独立 tenant 实例**：固定 `TENANT_ID`/`INSTANCE_ID`，只服务该租户；
   `/platform/*`、`/agent/*` 统一 404，Worker/Scheduler 只调度本租户。
2. 复用同一套代码与模板：`install → config → migrate → provision`，每步默认 plan/dry-run。
3. 数据迁移只允许**白名单表/列**；`id`、`tenant_id`、`member_no`、`source_user_id` 原样保留，禁止重新编号。
4. 任一时刻同一租户**只有一个写入实例**：旧实例停写/暂停后才能切换域名与流量。
5. 不修改现有生产；模板的 `--apply` 只能在新租户实例、维护窗口、Lead 审批后执行。

## 2. 拓扑与写权限

| 组件 | 建议 | 写权限 |
| --- | --- | --- |
| 中心（center） | 平台管理、租户登记、发布/运维/监控 | 平台写；租户业务以路由为准 |
| 租户实例（tenant） | 本租户业务 API/Worker/DB/Redis/图片卷 | 仅本租户写；平台接口 404 |
| 旧实例 | 迁移期保留只读/停机 | 切换前必须停写，禁止双写 |
| Agent2 | 每台服务器独立接入令牌/节点 | 只执行本实例任务 |

Worker 绑定环境变量（必须与实例一致，任务 payload 不可覆盖）：

```dotenv
DEPLOYMENT_MODE=tenant
TENANT_ID=3
INSTANCE_ID=2
ERP_WORKER_TENANT_ID=3
ERP_WORKER_INSTANCE_ID=2
ERP_WORKER_STRICT_BINDING=true
BACKUP_INSTANCE_ID=2
```

## 3. 模板与参数

| 模板 | 入口 | 关键参数 | 默认行为 |
| --- | --- | --- | --- |
| 配置 | `config.sh --check/--render` | tenant/instance/domain/project/image-tag | 只校验；render 生成 600 `.env`，不覆盖旧文件 |
| 安装 | `install.sh` | env-file、repo-dir、Agent 令牌 | plan；`--apply` 仅备份目录/可选 Agent |
| 开通 | `provision.sh` | api-base、code/name/owner-email、Cookie+CSRF | plan；`--apply` 调中心平台 API |
| 迁移 | `migrate.sh` | env-file、confirm、`--stop-write` | plan；`--apply` 备份→停写→迁移→对账→恢复 |
| 复制 | `copy_data.sh` | source/target option file、源/目标库名 | plan；`--apply` 按白名单 dump/import |
| 对账 | `reconcile.sh` | snapshot/compare/stop-write/resume | plan/只读；停写需确认令牌 |
| 隔离验收 | `verify_isolation.sh` | center/tenant URL | plan；`--run` 只读 HTTP GET |

`.env` 关键占位符：`COMPOSE_PROJECT_NAME=ventasync-tenant-<id>`、`APP_IMAGE_TAG=<发布tag>`、
`APP_ORIGIN=https://<域名>`、`TRUSTED_HOSTS=<域名>`、`HTTP_BIND=127.0.0.1`、`HTTP_PORT=8081`；
数据库/Redis/图片卷/备份目录均本机独立，禁止复用中心实例。

## 4. 第二租户接入顺序（维护窗口执行）

### 4.0 前置

- [ ] Lead 审批接入计划与维护窗口；旧实例可停写；目标服务器已就绪（Docker/compose、磁盘、TLS 域名）。
- [ ] 目标 release 包含批次 2/3/4 代码；备份密钥 `/etc/ventasync/backup.key` 已异地保管。
- [ ] 已完成整个实例备份（含中心库 + 旧租户库），并验证备份可读；**不使用 restore_drill**（冻结）。

### 4.1 中心开通（provision）

```bash
bash deploy/scripts/tenant-onboarding/provision.sh --plan \
  --api-base https://<center> --code <code> --name "<租户名>" --owner-email <owner@example.com> \
  [--timezone Asia/Shanghai] [--plan-id <id>]

# 审批后执行（平台管理员 Cookie + CSRF，来自同一会话）
sudo bash deploy/scripts/tenant-onboarding/provision.sh --apply --api-base https://<center> \
  --cookie-file /root/.erp-admin-cookie --csrf <CSRF> \
  --code <code> --name "<租户名>" --owner-email <owner@example.com> \
  --confirm PROVISION-<code>
```

记录返回的 `tenant_id` 与 `instance_id`；确认开通任务 `succeeded`、租户 `active`。
失败（`provision_failed`）先按平台任务重试，不得手工改库。

### 4.2 目标实例配置（config）

```bash
sudo bash deploy/scripts/tenant-onboarding/config.sh --render \
  --tenant-id <id> --instance-id <id> --domain <tenant-domain> \
  --project ventasync-tenant-<id> --image-tag <release-tag> \
  --output /opt/ventasync/deploy/.env
sudo bash deploy/scripts/tenant-onboarding/config.sh --check --env-file /opt/ventasync/deploy/.env
```

校验失败必须修复（TENANT_ID/INSTANCE_ID/Worker 绑定、HTTPS、TRUSTED_HOSTS、备份实例 ID），不得绕过。

### 4.3 服务器初始化（install）

```bash
sudo bash deploy/scripts/tenant-onboarding/install.sh --env-file /opt/ventasync/deploy/.env
# 确认 plan 后：
sudo bash deploy/scripts/tenant-onboarding/install.sh --env-file /opt/ventasync/deploy/.env \
  --apply --confirm INSTALL-<tenant>-<instance> \
  [--install-agent --center-url <center> --agent-enroll-token <一次性令牌>]
```

人工完成：Caddy/Nginx TLS、DNS 解析、平台 SSH 账号保存、首次 SSH exec 后核对 SHA256 指纹
（`fingerprint_status=verified`）、备份目录权限与首次备份。

### 4.4 数据复制（copy_data，旧实例 → 新实例）

先决：源库停写、目标 schema 已迁移、源/目标 MySQL option file 600、已做备份。

```bash
bash deploy/scripts/tenant-onboarding/copy_data.sh --plan \
  --env-file /opt/ventasync/deploy/.env \
  --source-defaults-file /root/.my-source.cnf --source-tenant-db <源租户库> \
  --source-center-db ventasync_center [--target-tenant-db <目标库>]

sudo bash deploy/scripts/tenant-onboarding/copy_data.sh --apply \
  --confirm COPY-<tenant>-<源租户库> --source-defaults-file ... --target-defaults-file ... ...
```

规则：只导出白名单表（`tenant_local_users` 已废弃不迁移）；中心库只复制本租户行
（tenants/memberships/subscriptions 按 tenant_id；users 按成员关系；plans/plan_features 为引用数据）；
`--no-create-info` 只导数据，目标表结构由 Alembic 创建；ID/member_no 原样保留。
复制后必须做第 4.6 节对账与人工抽查（账号/权限/商品/配置/订单）。

### 4.5 停写与迁移（migrate/reconcile）

```bash
# 1) 平台暂停租户（或确认业务低峰）
# 2) 对账前快照（只读）
sudo bash deploy/scripts/tenant-onboarding/reconcile.sh --snapshot --apply \
  --env-file /opt/ventasync/deploy/.env --output /var/tmp/reconcile-pre.tsv
# 3) 停写（只停本实例 worker/scheduler；API 仍在但不应有业务写入）
sudo bash deploy/scripts/tenant-onboarding/reconcile.sh --stop-write --apply \
  --env-file /opt/ventasync/deploy/.env --confirm STOP-WRITE-<tenant>
# 4) 迁移（备份 → 白名单校验 → alembic → 后快照 → 比较 → 恢复写入）
sudo bash deploy/scripts/tenant-onboarding/migrate.sh --apply --stop-write \
  --env-file /opt/ventasync/deploy/.env --confirm MIGRATE-<tenant>-<tenant_db>
```

`migrate.sh --apply` 失败不会自动恢复写入，按输出提示人工核对后执行
`reconcile.sh --resume --apply --confirm RESUME-<tenant>`。

### 4.6 切换与验证

1. 域名/DNS 指向新实例；`APP_ORIGIN`、CORS、TRUSTED_HOSTS、OAuth 回调全部使用新域名；
2. 平台登记店铺凭据/渠道回调（独立于中心；网关如启用按批次 5 处理）；
3. `verify_isolation.sh --run` + 第 6 节人工清单；
4. 旧实例保持停机/只读，确认无回切需求后再下线；回切必须先冻结新实例、反向核对，
   不得双写。

### 4.7 日常升级流程（接入后）

1. 发布版本/commit/tag 先在中心登记 release（`verified`、`min_agent_protocol=2`、center/tenant DB 目标版本）；
2. 目标实例 `config.sh --check`，确认 `APP_IMAGE_TAG` 指向新版本；
3. 升级前备份（Agent 步骤或 `deploy/backup.sh`），确认备份 completed；
4. `migrate.sh --plan` 确认迁移清单；需要停写时按 4.5 执行 `reconcile.sh --stop-write`；
5. 拉取/切换镜像并滚动启动 API/Worker/Scheduler（`docker compose ... up -d`），外部 TLS 由 Caddy 保持；
6. 验证：`/api/v1/runtime` mode=tenant、`/healthz` 200、租户登录/商品/订单冒烟、版本=目标 tag；
7. 失败：停止发布、回退镜像；数据库回退只能从升级前备份恢复（Lead 审批），演练操作先 `--dry-run` 并核对 `restore_status=verified`；
8. 记录发布 job/审计/备份 ID 与验证结果；旧实例在确认无回切前保持停机/只读。

## 5. 迁移白名单与保留标识

`deploy/scripts/tenant-onboarding/migration-whitelist.json`：

- `preserve_columns`: `id`、`tenant_id`、`member_no`、`source_user_id` 原样保留；
- `center_tables`：仅 6 张（tenants/users/tenant_memberships/plans/plan_features/subscriptions），列白名单；
- `tenant_tables`：33 张租户业务表清单；`tenant_local_users` 标记 `migrate=false`（非认证来源，历史副本不迁），实际复制 32 张；
- `forbidden_center_tables`：平台密钥/令牌/审计/运维/节点/备份等表禁止进入迁移包；
- 禁止列模式：`*_cipher`、`*secret*`、`partner_key*`；只允许 `users.password_hash` 作为登录凭据哈希；
- `allowed_tenants`：示例第二租户（tenant_id=3，`zuhu.alienyan.store`；instance_id=0 表示以中心路由为准）。

校验与对账：

```bash
# 白名单结构 + tenant 实例绑定/Worker 约束
python backend/scripts/tenant_migration_whitelist.py check \
  --whitelist deploy/scripts/tenant-onboarding/migration-whitelist.json \
  --env-file /opt/ventasync/deploy/.env

# 生成只读对账 SQL（行数/最大ID/稳定表全部列指纹/关联检查）
python backend/scripts/tenant_migration_whitelist.py emit-sql \
  --whitelist deploy/scripts/tenant-onboarding/migration-whitelist.json \
  --tenant-id 3 --tenant-db <租户库> --center-db ventasync_center > /var/tmp/reconcile.sql

# 比较停写前后快照（稳定表完全一致；追加型表只允许增长；关联检查必须 0）
python backend/scripts/tenant_migration_whitelist.py compare-tsv \
  --whitelist ... --before /var/tmp/reconcile-pre.tsv --after /var/tmp/reconcile-post.tsv
```

对账口径：稳定表（角色/权限/成员角色/设置/商品/SKU/门店/库存地点/渠道映射等）行数+max(id)+全列指纹一致；
追加型表（订单/库存流水/审计/同步/outbox 等）只允许增长；关联检查（成员-用户、订单明细、库存影响、
渠道映射、负库存）必须为 0。

## 6. 双实例隔离验收清单

执行人：Lead/deploy-qa 双人核对；证据保存到验收记录。

### 6.1 账号与会话

- [ ] 中心平台账号无法登录租户实例 `/platform/*`（404）；租户账号无法访问中心平台接口；
- [ ] 同一邮箱在两实例的登录端/会话 Cookie 域独立，退出不影响另一实例；
- [ ] tenant 实例 `/api/v1/runtime` mode=tenant，中心 mode=center。

### 6.2 数据库与存储

- [ ] 中心库/租户库连接串、库名、MySQL 卷、Redis 队列、图片卷均独立（无共享卷/共享库）；
- [ ] 租户库 `roles/member_roles` 只含本租户 `tenant_id` 行；
- [ ] 备份目录、备份密钥、异地桶互不覆盖；`BACKUP_INSTANCE_ID` 与实例一致。

### 6.3 OAuth 回调与店铺凭据

- [ ] Shopee/TikTok 回调地址逐实例登记，域名不混用；state 只能兑换本实例授权；
- [ ] `store_credentials` 解密只在本实例可用；中心平台不回显密钥；
- [ ] 旧实例的授权令牌/回调已停用，避免回调落到旧库。

### 6.4 运维任务与唯一写实例

- [ ] 平台运维任务只操作本实例节点（`instance_id` 绑定）；租户界面无跨实例任务；
- [ ] Agent2 节点 UUID 唯一，双实例不会重复执行同一任务（lease/journal 去重）；
- [ ] 同一租户任一时刻只有一个写入实例：旧实例 worker/scheduler 停止或租户 `suspended/migrating`；
- [ ] 监控/告警按实例归属，不把租户实例指标记到中心。

## 7. 异常场景验收

### 7.1 管理中心断网

1. 模拟中心网络不可达（防火墙/维护窗口，必须有人值守，禁止直接断生产共享库）。
2. 独立实例（本地中心库副本）：登录/业务/Worker 应继续；备份记录写中心失败只告警。
3. 共享中心库拓扑：中心不可达时登录/权限刷新可能失败，记录影响与恢复时间；
   不得因断网自动切换写实例。
4. 恢复后：检查心跳/监控补报、备份记录补录、会话与租户状态。

### 7.2 Agent2 重启

1. `systemctl restart erp-agent erp-agent-executor`；
2. 节点心跳恢复、`protocol_version=2`、无重复任务；执行中任务按 lease/attempt 续跑或重试；
3. 校验 `/platform/monitoring/overview` 节点状态与最近任务事件。

### 7.3 发布失败

1. 发布任务失败时立即停止发布，保留日志/任务事件；不自动 downgrade 数据库；
2. 迁移前备份存在且可读 → 回退镜像 → 如需回库按 `docs/backup-restore.md` 人工恢复（Lead 审批）；
3. 恢复演练使用修复版脚本（先 `--dry-run`，再 `--latest --record-latest --yes`），核对 `restore_status=verified`；
   不得用旧版脚本或未配置 `BACKUP_INSTANCE_ID` 的实例验证。

### 7.4 恢复验证

1. 选择最近备份（记录 `backup_records.id`/路径/校验和），确认本地/异地状态；
2. 在隔离环境/人工核对下恢复；恢复后校验中心/租户表数、账号/权限/商品/订单数量与备份一致；
3. 依次启动 API/Worker/Scheduler，做登录/商品/订单冒烟；确认唯一写实例后再放流量；
4. 结论只写“恢复验证通过/失败”；演练执行通过不等于记录闭环，`restore_status=verified` 在 `backup_records`
   关联修复前不得写入。

## 8. 验收证据模板

```text
租户：tenant_id=... code=... 实例：instance_id=... 域名=...
发布：commit=... image_tag=...
配置：TENANT_ID/INSTANCE_ID/Worker 绑定校验通过（config.sh --check）
数据：copy_data 白名单表=33 张（tenant_local_users 除外）；ID/member_no 保留
停写：开始/结束时间；pre/post 快照路径；对账结果（稳定表一致、追加型增长、关联=0）
迁移：alembic tenant head=tenant_0014；迁移前后行数/指纹
隔离：runtime/healthz/404；账号/库/OAuth/凭据/运维任务/唯一写实例逐项
异常：中心断网/Agent 重启/发布失败/恢复验证 实测结果或“未执行”
剩余：...
```

## 9. 离线验证与已知边界

已执行（HEAD `5c9dd53`，未连接生产）：

- `bash -n`：8 个 shell 模板全部通过（Git Bash）；
- `tenant_migration_whitelist.py self-test` 通过；白名单结构 + 33 表覆盖校验通过；
- `config.sh --render/--check`：生成 600 配置、绑定校验通过，Worker 不一致会拒绝；
- `install/migrate/reconcile/verify_isolation/provision --plan` 均正常输出，不修改系统；
- 白名单拒绝未登记租户（TENANT_ID=99）、拒绝密钥列、对账比较能识别稳定表漂移。

未执行/待 Lead 决策：

- 第二租户真实开通、域名切换、OAuth 回调重登记、店铺凭据迁移；**第二租户仍未部署**；
- `copy_data.sh --apply`、`migrate.sh --apply`、stop-write/resume 真机演练；
- 中心断网、Agent 重启、发布失败、恢复验证的真实演练；
- 独立网关（task-35）代码已合入但**未部署**，且不纳入本模板链路；恢复演练记录关联已由 b485bee 修复并闭环。

生产现状补充（2026-11-13 17:40 CST）：

- `8.163.80.17` 已恢复业务库 `vs_tenant_kangkang_shop`（34 表、schema `tenant_0014`）、创建/授权 `vs_t_3`；
- `.env` 已修正 `TENANT_ID=3`/`ERP_WORKER_TENANT_ID=3`，登录 200 / products 200 / tenant 200；
- 新备份 `093925Z`；设置 `BACKUP_INSTANCE_ID=1` 后 094919Z 演练通过，自动匹配 `backup_records id=2` 并写
  `restore_status=verified`（`restore_verified_at=2026-11-13 09:49:54`）；**`BACKUP_INSTANCE_ID` 必配**。
