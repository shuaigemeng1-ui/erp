# 第二独立租户接入模板（批次 4）

本目录把“第二租户”接入从手工步骤固化为可审计模板。**默认全部 plan/dry-run**；
任何 `--apply` 都必须显式确认令牌，且只允许在新独立租户实例执行。

> 真实执行手册、双实例验收清单、断网/Agent 重启/发布失败/恢复验证流程见
> [`docs/batch4-tenant-onboarding.md`](../../../docs/batch4-tenant-onboarding.md)。

## 文件

| 文件 | 作用 |
| --- | --- |
| `tenant.env.example` | 新租户实例 `.env` 模板（TENANT_ID / INSTANCE_ID / Worker 绑定 / 域名 / 备份） |
| `config.sh` | 校验现有 `.env`，或从模板渲染新 `.env`（随机密码、600 权限、不覆盖旧文件） |
| `install.sh` | 新服务器初始化：目录/备份权限、可选 Agent2 安装、TLS/域名/SSH 指纹检查清单 |
| `provision.sh` | 调用中心平台 API 创建/开通租户并轮询进度（管理员 Cookie+CSRF） |
| `migrate.sh` | 备份 → 停写 → 白名单校验 → Alembic 迁移 → 对账 → 恢复写入 |
| `copy_data.sh` | 按白名单从旧实例复制租户库数据 + 租户范围中心库记录（保留 ID/member_no） |
| `reconcile.sh` | 停写前/后只读快照（行数、最大 ID、稳定表指纹、关联检查）与比较 |
| `verify_isolation.sh` | 双实例隔离只读验收（runtime/healthz + 人工清单） |
| `migration-whitelist.json` | 迁移表白名单/禁止表/保留列/关联检查定义 |
| `_common.sh` | 公共参数解析、确认令牌、env 读取、compose 封装 |

配套工具：`backend/scripts/tenant_migration_whitelist.py`（白名单校验、对账 SQL 生成、TSV 比较、离线自测）。

## 快速顺序

```bash
# 1) 中心开通租户（plan → apply；管理员 Cookie + CSRF）
bash deploy/scripts/tenant-onboarding/provision.sh --plan --api-base https://<center> \
  --code <tenant_code> --name "<租户名>" --owner-email <owner@example.com>

# 2) 目标实例生成配置（示例 tenant_id=3 / instance_id=2）
sudo bash deploy/scripts/tenant-onboarding/config.sh --render \
  --tenant-id 3 --instance-id 2 --domain <tenant-domain> \
  --project ventasync-tenant-3 --image-tag <release-tag> \
  --output /opt/ventasync/deploy/.env
sudo bash deploy/scripts/tenant-onboarding/config.sh --check --env-file /opt/ventasync/deploy/.env

# 3) 服务器初始化（目录/备份/Agent/TLS/SSH 清单）
sudo bash deploy/scripts/tenant-onboarding/install.sh --env-file /opt/ventasync/deploy/.env
sudo bash deploy/scripts/tenant-onboarding/install.sh --env-file /opt/ventasync/deploy/.env \
  --apply --confirm INSTALL-3-2 --install-agent --center-url <center> --agent-enroll-token <token>

# 4) 数据复制（旧实例 → 新实例；先 plan，维护窗口 apply）
bash deploy/scripts/tenant-onboarding/copy_data.sh --plan --env-file /opt/ventasync/deploy/.env \
  --source-defaults-file /root/.my-source.cnf --source-tenant-db <源租户库>

# 5) 停写对账 + 迁移（先 plan，再维护窗口 apply）
bash deploy/scripts/tenant-onboarding/migrate.sh --plan --env-file /opt/ventasync/deploy/.env
sudo bash deploy/scripts/tenant-onboarding/migrate.sh --apply --stop-write \
  --env-file /opt/ventasync/deploy/.env --confirm MIGRATE-3-vs_tenant_kangkang_shop

# 6) 双实例隔离验收（只读 HTTP；人工清单见 docs）
bash deploy/scripts/tenant-onboarding/verify_isolation.sh --run --env-file /opt/ventasync/deploy/.env \
  --center-url https://<center> --tenant-url https://<tenant-domain> \
  --report /var/tmp/isolation-report.json
```

## 安全边界

- 不在中心生产机执行 install/migrate/reconcile/stop-write；`COMPOSE_PROJECT_NAME` 含 `drill` 或等于 `ventasync` 会被拒绝。
- `migrate.sh --apply` 必须同时提供 `--stop-write` 和确认令牌；失败不会自动恢复写入，按输出提示人工核对。
- `reconcile.sh --stop-write/--resume` 只停/启本实例 `worker scheduler`，不操作 MySQL 数据与中心库。
- `provision.sh --apply` 需要平台管理员 Cookie + CSRF；不打印 Cookie/令牌/密码。
- `config.sh --render` 不覆盖既有 `.env`（除非 `--force`，且自动备份）；生成文件 600，密码随机且不回显。
- **禁止在任何生产机运行当前 `restore_drill.sh`**（2026-11-13 事故后冻结，见 `docs/incident-2026-11-13-restore-drill.md`）。

## 离线校验

```bash
# Shell 语法（Git Bash / Linux）
for f in deploy/scripts/tenant-onboarding/*.sh; do bash -n "$f"; done
# 白名单/环境/对账逻辑自测（不连数据库）
python backend/scripts/tenant_migration_whitelist.py self-test
python backend/scripts/tenant_migration_whitelist.py check \
  --whitelist deploy/scripts/tenant-onboarding/migration-whitelist.json --no-metadata

# 所有模板 plan 模式（不会修改系统）
bash deploy/scripts/tenant-onboarding/config.sh --check --env-file /tmp/tenant-test.env
bash deploy/scripts/tenant-onboarding/install.sh --env-file /tmp/tenant-test.env
bash deploy/scripts/tenant-onboarding/migrate.sh --plan --env-file /tmp/tenant-test.env
bash deploy/scripts/tenant-onboarding/reconcile.sh --plan --env-file /tmp/tenant-test.env
bash deploy/scripts/tenant-onboarding/verify_isolation.sh --plan --env-file /tmp/tenant-test.env
```

## 已知边界

- 白名单默认示例为第二租户（tenant_id=3 / `zuhu.alienyan.store`）；新租户须追加并经 Lead 审核，`instance_id=0` 表示以中心库实际路由为准。
- 真实第二租户的域名切换、OAuth 回调重登记、店铺凭据迁移、Agent2 安装与 SSH 首指纹确认尚未执行；模板只提供流程与离线校验。
- 恢复演练（restore_drill）仍处于事故后安全冻结状态，不纳入本模板链路。
