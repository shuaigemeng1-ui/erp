# 备份加密、异地存储与恢复演练（批次3）

> ## ⛔ STOP：恢复演练脚本安全冻结（2026-11-13 事故后）
>
> 当前 `deploy/scripts/restore_drill.sh` **禁止在任何生产机运行任何模式**
> （`--latest`、`--bundle`、`--record-latest`、`--self-test` 均暂停用于生产环境）。
>
> 2026-11-13 约 15:53 CST，在 8.163.80.17 执行恢复演练时误清空主 MySQL 的 `ventasync_center`（0 表）
> 并停止主业务容器；已从备份恢复（中心库 30 表、租户 HTTP 200），事故与恢复证据见
> `docs/batch3-acceptance.md`“恢复演练事故与处置”。
>
> 根因：脚本未真正隔离独立卷/安全断言不足，旧版本“独立项目、独立卷、不碰生产卷、无需停生产”的断言**不成立**。
> 修复（独立卷 + 启动前安全断言）并重新验收前，**恢复演练状态 = 阻断/未通过**；
> 生产恢复只能走“备份 + 人工核对 + Lead 审批”的应急流程，不得使用当前 `restore_drill.sh`。

本文是批次3「备份加密异地与恢复演练」的实现说明与验收依据。目标：

1. 备份数据**移出 Git 工作目录**，目录 700 / 文件 600；
2. 覆盖**身份库（中心库）+ 各租户业务库 + 必要配置 + 本地文件**；
3. 默认**租户时区 03:30** 执行、保留 **30 天**；升级前额外备份由 Agent 步骤触发、后端登记；
4. **加密上传独立私有备份存储**，不复用商品图片公开目录；完整记录
   本地生成 / 完整性校验 / 异地上传 / 恢复验证四类状态；
5. 提供**隔离环境恢复脚本**与演练清单（⚠️ 当前脚本因事故安全冻结，修复前不可用于任何生产环境，
   见本页顶部 STOP 与第 8 节）。

相关文件：

| 文件 | 作用 |
| --- | --- |
| `deploy/scripts/backup.sh` | 备份主脚本：导出数据库/配置/本地文件 → 清单校验 → 加密 → 异地 → 记录中心库 → 清理过期 |
| `deploy/backup.sh` | Agent/手工入口（兼容旧路径，默认 `scheduled`，可用 `BACKUP_TRIGGER` 覆盖） |
| `deploy/systemd/ventasync-backup.{service,timer}` | 每日 03:30（服务器本地时区）执行，保留 30 天 |
| `deploy/scripts/restore_drill.sh` | ⛔ **安全冻结（2026-11-13 事故），禁止生产运行**；待修复独立卷/安全断言后重新验收 |
| `deploy/scripts/restore.sh` | 旧版单库应急恢复（保留，用于单库手工恢复） |
| `backend/app/modules/ops/backup_cli.py` | 容器内 CLI：上报备份清单 / 恢复演练结果 / 查询最新记录 |
| `backend/alembic/center/versions/0018_center_backup_recovery.py` | `backup_records` 状态字段扩展 |

---

## 0. 部署前提与权限初始化（必做一次）

Agent 的执行进程用户是 **erp-agent-exec**（共享组 **erp-agent**），升级前备份与每日备份都以
该用户运行。因此备份根目录与密钥目录必须由 root 预先创建并授权，否则升级前备份会因权限失败
而**中止升级**（这是预期保护：备份失败不得继续升级）。

```bash
# 在租户服务器（以及需要备份的中心服务器）root 执行一次
sudo bash deploy/scripts/install_backup_dirs.sh

# 校验
stat -c '%a %U:%G %n' /var/lib/ventasync-backups /var/lib/ventasync-backups/releases /etc/ventasync /etc/ventasync/backup.key
sudo -u erp-agent-exec test -w /var/lib/ventasync-backups && echo "备份目录可写"
sudo -u erp-agent-exec test -r /etc/ventasync/backup.key && echo "密钥可读"
```

脚本行为（可重复执行）：

* 创建组 `erp-agent`，把 `erp-agent` / `erp-agent-exec` 加入该组（用户不存在时给提示，Agent 安装后重跑）；
* 创建 `/var/lib/ventasync-backups`、`/releases`、`/offsite`、`/drill-reports`、`/etc/ventasync`，
  权限 **2770 root:erp-agent**（setgid 让子目录继承组）；
* 安装 `/etc/tmpfiles.d/ventasync-backups.conf`，重启后保持权限；
* 密钥缺失时生成 `/etc/ventasync/backup.key`（**640 root:erp-agent**）；
* 检查 `erp-agent-exec` 是否在 docker 组、docker compose v2 是否可用（缺失只告警）。

> 说明：备份产物本身仍按「目录 700 / 文件 600」创建（属主为执行备份的用户），
> 根目录 2770 只用于让 Agent 用户能创建本次备份目录。

## 1. 备份目录与权限

默认根目录：`/var/lib/ventasync-backups`（**不在 Git 工作目录内**），可用 `BACKUP_ROOT` 覆盖。

```text
/var/lib/ventasync-backups/
  releases/<UTC时间>/
    ventasync_center.sql.gz         # 身份库：账号/权限/平台管理员/租户/配置元数据
    vs_tenant_<id>.sql.gz           # 各租户业务库：商品/订单/库存/渠道/权限/设置
    config/.env                     # 必要配置（含密钥，文件 600）
    config/docker-compose*.yml
    config/nginx.conf
    config/caddy/Caddyfile*
    catalog_media.tar.gz            # 本地文件：商品图片卷（存在时）
    manifest.json                   # 清单：数据库/配置/文件/大小/sha256/校验结果
    bundle.tar.gz.enc               # 加密归档（异地传输单元）
    bundle.tar.gz.enc.sha256
  offsite/<instance>/<UTC时间>/     # local 异地模式镜像（目录 700 / 文件 600）
```

权限约定：

* `DEPLOY_DIR/.gitignore` 已加入 `backups/`、`*.sql.gz`、`*.enc` 等规则；实际备份目录在
  `/var/lib/ventasync-backups`，不进入仓库；
* 脚本启动即 `umask 077`，创建目录使用 `install -d -m 700`，结束前统一
  `find ... -type d -exec chmod 700`、`find ... -type f -exec chmod 600`；
* 权限建议每季度复核：`stat -c '%a %U:%G %n' /var/lib/ventasync-backups /etc/ventasync/backup.key`。

## 2. 备份内容

| 类别 | 内容 | 来源 |
| --- | --- | --- |
| 身份库 | `ventasync_center`（users、platform_user_roles、tenants、tenant_memberships、tenant_databases、ops 元数据等） | `mysqldump --single-transaction` |
| 业务库 | `tenant_databases` 中全部租户库 | 遍历中心库 `tenant_databases` |
| 必要配置 | `deploy/.env`、`docker-compose.yml`、`docker-compose.server.yml`、`nginx/nginx.conf`、`caddy/Caddyfile*` | 直接复制到 `config/` |
| 本地文件 | 商品图片卷 `/app/var/catalog-media` | `docker cp` 打包（卷未配置时跳过并记录） |
| 清单 | 文件、大小、SHA-256、数据库列表、校验结果、触发来源 | `manifest.json` |

导出参数：`--single-transaction --quick --hex-blob --routines --triggers --set-gtid-purged=OFF`，
与单库恢复脚本一致；gzip 后做 `gzip -t` 校验，并统计 `CREATE TABLE` 数量。
本地校验失败（缺中心库、缺配置、SHA-256 异常）直接退出码 1，不进入加密/上传阶段。

## 3. 加密与密钥

* 算法：`AES-256-CBC + PBKDF2（200000 次）+ salt`（`openssl enc`）；
* 密钥文件：`/etc/ventasync/backup.key`（`BACKUP_ENCRYPTION_KEY_FILE`），首次运行自动生成
  `openssl rand -base64 32`，权限 600；**密钥不进入备份包、不写数据库、不进日志**；
* 加密后立即做**解密回读校验**（解密 → `tar -tzf` 列目录），确保密钥可用、包未损坏；
* 异地传输单元为 `bundle.tar.gz.enc`，校验值 `sha256:<hex>` 记录在清单与
  `backup_records.checksum`；
* **密钥必须异地单独保管**（如密码管理器/密钥托管）；密钥丢失则异地包不可恢复，
  但本地明文数据库导出仍在保留期内可用于恢复。

## 4. 异地私有存储（不复用商品图片目录）

`BACKUP_OFFSITE_MODE`：

| 模式 | 行为 | 说明 |
| --- | --- | --- |
| `none`（默认） | 仅本地 | 目录/权限/加密/记录仍执行，`offsite_status=skipped` |
| `local` | 复制到 `BACKUP_OFFSITE_DIR/<instance>/<stamp>/` | 独立私有目录，校验值复核后 `offsite_status=uploaded` |
| `s3` | 上传到 `BACKUP_S3_BUCKET`（独立私有备份桶，前缀 `BACKUP_S3_PREFIX`） | 客户端已加密；不复用商品图片 `S3_BUCKET`；上传后 `head_object` 核对大小 |

要求：

* **不得**把备份写入 `S3_BUCKET`/`/storage` 等商品图片公开目录或 nginx 静态目录；
* S3 桶策略建议：仅备份账号可 `PutObject/GetObject`，关闭公开读，开启版本/对象锁与生命周期；
* 异地失败（退出码 2）会保留本地包并记录 `offsite_status=failed` + `failure_reason`，
  **升级流程会因此中止**（Agent 备份步骤失败即停止升级）；
* 本地/异地镜像按 `BACKUP_RETENTION_DAYS`（默认 30）清理；S3 对象由桶生命周期策略清理。

## 5. 调度：03:30 / 30 天

默认租户时区 03:30（服务器本地时区 = 租户业务时区，见 `deploy/README.md` 时区章节）：

```bash
sudo install -m 644 deploy/systemd/ventasync-backup.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/ventasync-backup.timer  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ventasync-backup.timer
systemctl list-timers ventasync-backup.timer     # 下次触发时间
journalctl -u ventasync-backup.service -n 50     # 最近一次结果
```

* `OnCalendar=*-*-* 03:30:00`、`Persistent=true`（停机错过补跑一次）、
  `RandomizedDelaySec=300`（0-5 分钟抖动，避免多实例同时打满存储）；
* 保留 30 天：`BACKUP_RETENTION_DAYS=30`（`0` 表示不自动清理）；
* 手工触发：`bash deploy/backup.sh --trigger manual`；
* 升级前额外备份：Agent 升级流程第 5 步执行 `deploy/backup.sh`（输出
  `BACKUP_KEY/BACKUP_SIZE`），后端在升级成功后从「升级前备份」步骤登记
  `backup_records.trigger=pre_upgrade`，并按存储目录去重复用同一条记录。

## 6. backup_records 状态契约（批次3）

中心库 `backup_records` 新增（`center_0018`，无外键、中文注释、旧记录兼容）：

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `local_status` | pending/generated/verified/failed | 本地生成状态（verified=完整性校验通过） |
| `local_path` | 路径 | 本地备份目录（Git 工作目录之外） |
| `checksum_status` | pending/verified/failed | 校验状态 |
| `checksum` | `sha256:<hex>` | 加密包/归档校验值 |
| `offsite_status` | pending/uploaded/failed/skipped | 异地上传状态 |
| `offsite_key` | `s3://...` / `file:...` | 异地对象键 |
| `restore_status` | not_tested/verified/failed | 隔离恢复验证状态 |
| `restore_verified_at` | UTC 时间 | 恢复验证通过时间 |
| `failure_reason` | 文本 | 失败原因（本地/校验/异地/恢复任一阶段） |
| `stages` | JSON 数组 | 阶段流水（local/checksum/offsite/restore） |

辅助字段（非契约必填）：`trigger`（manual/scheduled/pre_upgrade）、`verified_at`、
`offsite_bucket`、`encryption`、`restore_notes`、`manifest_json`（清单摘要）。

写入路径：

* 备份脚本结束调用容器内 CLI：`python -m app.modules.ops.backup_cli apply`（清单 JSON 经环境变量传入，幂等按 `local_path` 去重）；
* Agent `backup` 任务终态：`agent_job_event` 更新同一条记录（按 `storage_key` 去重）；
* 升级成功：`_record_pre_upgrade_backup` 登记 `pre_upgrade`；
* 恢复演练：**当前安全冻结，禁止生产执行**；旧 `restore_drill.sh --record-id/--record-latest` 回写流程暂停，
  待独立卷/安全断言修复并重新验收后恢复；
* 平台接口：`GET /api/v1/platform/backups`、`GET /api/v1/platform/backups/{id}`、
  `POST /api/v1/platform/backups/{id}/restore-check`（平台管理员）。

## 7. 命令参考

```bash
# 每日定时（systemd）或手工
bash deploy/backup.sh --trigger manual

# 指定异地与保留
bash deploy/scripts/backup.sh --trigger scheduled --offsite s3 --keep-days 30 --instance-id 3

# 仅本地调试（不写中心库、不上传）
bash deploy/scripts/backup.sh --no-offsite --no-record

# 中心服务器：只备份中心库（租户库在远端实例时；脚本也会自动跳过本机不存在的租户库）
bash deploy/scripts/backup.sh --trigger manual --center-only

# dry-run：只打印计划
bash deploy/scripts/backup.sh --dry-run --trigger pre_upgrade

# ⛔ 恢复演练：当前 restore_drill.sh 已安全冻结（2026-11-13 事故），禁止在生产机执行任何模式；
#    修复独立卷/安全断言并重新验收后，再由 Lead 发布新的演练命令说明。
#    原 `--self-test` / `--latest --record-latest` / `--bundle ... --record-id` 命令暂时撤回。
```

`backup_cli.py` 支持：`apply`（上报清单）、`restore-check`（恢复演练结果）、`latest`（查询最新记录）。

## 8. 恢复演练（已安全冻结，禁止生产执行）

> **STOP**：2026-11-13 约 15:53 CST，在 8.163.80.17 执行旧版 `restore_drill.sh` 时误清空主 MySQL
> `ventasync_center`（0 表）并停止主业务容器；随后从备份恢复（中心库 30 表、租户 HTTP 200）。
> 当前脚本“独立项目/独立卷/不碰生产卷/无需停生产”的断言已证伪，**禁止在任何生产机运行任何模式**。
> 详细事故与恢复证据见 `docs/batch3-acceptance.md`。

### 8.1 当前状态与禁用范围

- 状态：**恢复演练 = 阻断/未通过**（不得记录 `restore_status=verified`，不得写“恢复演练通过”）；
- 禁用：`restore_drill.sh` 的全部运行模式（`--latest`/`--bundle`/`--record-latest`/`--self-test`）用于生产环境；
- 旧版本节“真实机执行前提”“执行命令”“隔离规则”“通过标准”已整体撤回，不能再按旧文档执行；
- 生产数据库恢复：仅允许 Lead 审批的应急流程（停写 → 备份最新状态 → 用已验证备份人工恢复中心/租户库
  → 校验 → 恢复服务），不得使用当前 `restore_drill.sh`。

### 8.2 修复要求（backend-sales 修复后重新验收）

1. **独立卷**：演练使用显式独立的 compose 项目名与命名卷；启动前断言项目名/卷名/容器名/数据目录与生产不重合；
2. **安全断言**：校验 `.env`/compose 输入不指向生产、目标 MySQL 不发布生产端口、演练库名/卷名白名单；
3. **危险操作防护**：禁止 down/volume 删除生产资源；失败自动清理演练资源并保留报告；
4. **dry-run/确认**：默认 dry-run，真实执行需显式确认令牌与隔离主机标记；
5. 修复后重新编写“隔离主机演练”清单，先在非生产主机验证通过，再由 Lead 审批恢复演练流程。

## 9. 验收证据记录模板

> 当前 `restore_drill.sh` 安全冻结：**禁止产生 `restore_status=verified` 或“恢复演练通过”记录**；
> 以下模板仅在修复并重新验收后使用。

```text
日期：YYYY-MM-DD
环境：实例 ID / 域名 / 版本
备份：run_id=...  trigger=scheduled/pre_upgrade  local_status=verified
       checksum=sha256:...  offsite_status=uploaded (s3://bucket/key)
记录：backup_records.id=...  local_path=...  restore_status=<冻结期间不得写 verified>
演练：<冻结期间不得写 passed=true>；修复后按新验收流程记录报告路径与明细
异常：无 / 失败项与跟进人
```

## 10. 故障处理

| 现象 | 处理 |
| --- | --- |
| 本地校验失败（退出码 1） | 查看日志中缺失项（中心库/配置/SHA）；修复 mysql 连通性或配置后重跑；失败不覆盖已有备份 |
| 异地失败（退出码 2） | 本地包保留；检查 `BACKUP_OFFSITE_*`/桶权限/网络；修复后重跑或手工补传；`offsite_status=failed` 记录原因 |
| 中心库不可达导致记录失败 | 备份有效，仅告警；恢复中心库后按清单手工补录（`backup_cli apply`） |
| 密钥文件丢失 | 异地包无法解密；改用保留期内的本地明文库导出恢复；立即重建密钥并重做异地备份 |
| 恢复演练异常/事故 | 立即停止演练并上报 Lead；**禁止运行当前 restore_drill.sh**，按“备份 + 人工核对”的应急恢复流程处置；恢复后重新备份并核对服务健康；修复独立卷/安全断言后重新验收 |
| 磁盘空间不足 | 调低 `BACKUP_RETENTION_DAYS`、清理旧 releases、把 `BACKUP_ROOT` 挂到独立数据盘 |

## 11. center_0018 迁移、验证与回滚（中心服务器 121.37.45.132）

> 顺序固定：**先备份 → 再迁移 → 再启动新版本**；迁移失败立即停止发布，用备份恢复，不自动降级。

```bash
cd /opt/ventasync
# 0) 拉取包含批次3的代码（task-29/30 落盘后的目标 commit）
git fetch origin && git log --oneline -3        # 确认 commit
# 1) 迁移前备份中心库（--center-only；租户库在远端时自动跳过）
sudo -u erp-agent-exec bash deploy/backup.sh --trigger manual --center-only
#    记录输出中的 BACKUP_DIR/BACKUP_CHECKSUM；如需异地把 --center-only 去掉并配置 BACKUP_OFFSITE_MODE
# 2) 发布迁移（独立 migrate 容器，含 MySQL 命名锁，幂等）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env --profile release run --rm migrate --target center
# 3) 校验中心库 revision（应为 center_0018）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec -T mysql \
  sh -c 'export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?}"; mysql -uroot -N -B -e "SELECT version_num FROM ventasync_center.alembic_version"'
# 4) 校验新列（应列出 10 个契约字段）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec -T mysql \
  sh -c 'export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?}"; mysql -uroot -N -B -e "SELECT column_name FROM information_schema.columns WHERE table_schema=\"ventasync_center\" AND table_name=\"backup_records\" ORDER BY ordinal_position"'
# 5) 重启 API/Worker/Scheduler（按发布流程滚动）
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml --env-file deploy/.env up -d api worker scheduler nginx
```

回滚（仅在迁移失败且**尚未写入新业务数据**时；否则从备份恢复）：

```bash
cd /opt/ventasync
# 1) 先停 worker/scheduler，避免继续写库
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml --env-file deploy/.env stop worker scheduler
# 2) 降级中心库迁移（center_0018 → center_0017，会删除 batch3 新列，数据备份先行）
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec -T api \
  alembic -c alembic/center.ini downgrade center_0017
# 3) 校验 revision=center_0017
docker compose -f deploy/docker-compose.yml --env-file deploy/.env exec -T mysql \
  sh -c 'export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?}"; mysql -uroot -N -B -e "SELECT version_num FROM ventasync_center.alembic_version"'
# 4) 回退应用镜像到上一版本后启动
#    APP_IMAGE_TAG=<上一版本标签> docker compose ... up -d api worker scheduler nginx
```

**恢复优先策略**：若迁移中途失败/DDL 已部分生效，停止发布 → 用步骤 1 的迁移前备份恢复中心库
（`deploy/scripts/restore.sh -f <中心库.sql.gz> -d ventasync_center --allow-center --yes`，
或解密 `bundle.tar.gz.enc` 后取对应 `.sql.gz`）→ 回退镜像 → 重新按顺序发布。
禁止在未备份的情况下 downgrade 生产库。

租户服务器（8.163.80.17）迁移：随其自身发布执行 `migrate --target tenant` 或整机 migrate；
其中心库若为远端共享库，center_0018 只需在中心服务器执行一次。

---

附：旧版单库恢复（仍可用）

```bash
deploy/scripts/restore.sh -f <单库备份.sql.gz> -d <库名> --tenant-id <ID> --yes
```

新版目录式备份的应急恢复：解密 `bundle.tar.gz.enc` → 取对应 `.sql.gz` →
`restore.sh -f` 单库恢复（中心库加 `--allow-center`），顺序为先中心库、后租户库。
