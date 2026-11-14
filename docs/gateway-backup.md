# 网关 SQLite 备份、加密异机保存与恢复演练（task-45）

> 适用：独立集成网关 `gateway/gateway_app.py`（SQLite `/data/gateway.db`）。
> 本任务只新增脚本/部署示例/文档；**未修改 backend Gateway 核心，未重启或改动生产容器/数据卷**。

## 1. 为什么不能直接 `cp` / `tar`

网关 SQLite 使用 WAL 模式。直接 `cp /data/gateway.db` 或对 volume 做 `tar` 会得到
缺少已提交 WAL 内容、甚至撕裂的副本；RPO/一致性不可验证。本方案使用 Python
`sqlite3.Connection.backup()`（与 `sqlite3 .backup` 相同的在线备份 API）：

```text
只读连接源库（mode=ro）
  → 分步 copy 到临时文件（pages=256，避免长时间锁）
  → fsync 临时文件
  → PRAGMA integrity_check + schema 指纹 + 关键行数
  → gzip（压缩减小备份体积）
  → AES-256-CTR + HMAC-SHA256 加密
  → fsync + 原子 rename 到私有备份目录
  → 写 manifest（SHA-256/大小/schema/事件计数/offsite 状态）
```

临时文件与最终文件都在 `GATEWAY_BACKUP_ROOT` 下；生产 `/data` 只被只读打开。

## 2. 脚本

| 文件 | 作用 |
| --- | --- |
| `scripts/gateway_backup.py` | `snapshot` / `restore-drill` / `retention` / `status` / `disk-budget` / `self-test` |
| `deploy/gateway/docker-compose.backup.yml` | 可选 sidecar：只读挂载网关数据卷，每 15 分钟快照；默认 profile=backup 不启动 |
| `deploy/gateway/backup.env.example` | 备份根目录、密钥目录、异机目录、保留、频率 |
| `deploy/gateway/README.md` §8 | 部署与恢复操作 |
| `docs/gateway-deployment.md` §7 | 生产实际网关备份/风险更新 |

依赖：Python 3.10+；加密需 `cryptography`（网关镜像已内置）。不 import backend 包。

## 3. 快速使用（离线/隔离环境）

```bash
# 1) 生成密钥（只在网关主机的独立密钥目录；权限 600，绝不能和备份放同一块盘）
install -d -m 700 /opt/erp-gateway/keys
python3 scripts/gateway_backup.py snapshot \
  --key-file /opt/erp-gateway/keys/backup.key --init-key

# 2) 手动快照（源库默认可通过 --source 指定；生产由 sidecar 自动执行）
python3 scripts/gateway_backup.py snapshot \
  --source /data/gateway.db \
  --backup-root /opt/erp-gateway/backups \
  --key-file /opt/erp-gateway/keys/backup.key \
  --recent-hours 24 --daily-days 30 \
  --offsite local --offsite-dir /opt/erp-gateway/offsite \
  --json

# 3) 隔离恢复演练（临时目录；不碰生产 volume）
python3 scripts/gateway_backup.py restore-drill \
  --backup /opt/erp-gateway/backups/gateway-YYYYMMDDTHHMMSSZ.db.enc \
  --key-file /opt/erp-gateway/keys/backup.key \
  --work-dir /tmp/gateway-drill \
  --report-dir /tmp/gateway-drill-reports --json

# 4) 只读状态/磁盘预算
python3 scripts/gateway_backup.py status \
  --database /data/gateway.db --backup-root /opt/erp-gateway/backups --json
python3 scripts/gateway_backup.py disk-budget \
  --database /data/gateway.db --backup-root /opt/erp-gateway/backups --json
```

异机/独立私有目录：

* `--offsite local`：复制到独立挂载盘/NFS/远端同步目录（`GATEWAY_BACKUP_OFFSITE_DIR`）；
* `--offsite rsync`：`--rsync-target user@backup-host:/srv/gateway-backups`，
  需宿主机已配置 SSH key/known_hosts；脚本不透传/不记录口令。

## 4. 15 分钟快照与保留

### 4.1 快照保留（磁盘）

* 每 15 分钟一份（`GATEWAY_BACKUP_INTERVAL_SECONDS=900`）；
* 最近 `GATEWAY_BACKUP_RECENT_HOURS`（默认 **24h**）全部保留；
* 更早 `GATEWAY_BACKUP_DAILY_DAYS`（默认 **30 天**）每天最多保留一份（UTC 日期）；
* 其余自动删除（先加密文件后 manifest；`--dry-run` 只报告不删）；
* 最新快照永不删除；删除只影响备份目录，不影响 `/data`。

### 4.2 事件保留（Gateway SQLite 内）

`retention` 子命令默认 **dry-run**，在维护窗口显式 `--apply`：

| 数据 | 默认 | 行为 |
| --- | --- | --- |
| 已完成事件（`delivered`/`dead`/`unroutable`）原文 | 7 天 | 清空 `payload_cipher`/`payload_sha256`，保留行用于去重/审计 |
| 已完成事件去重行 | 30 天 | 删除整行（`UNIQUE(channel,notification_id)` 随之释放） |
| 未完成事件（`pending`/`retry_wait`/`processing` 等） | 永久 | **不删除、不清 payload**，直到处理结束 |
| 非法/未知状态 | — | 按未完成处理，不删除 |

实现 SQL（`gateway_webhook_events`）：

```sql
-- 7d：只清已完成事件原文
UPDATE gateway_webhook_events SET payload_cipher='', payload_sha256=''
 WHERE status IN ('delivered','dead','unroutable')
   AND created_at < :now_minus_7d
   AND (LENGTH(payload_cipher)+COALESCE(LENGTH(payload_sha256),0)) > 0;

-- 30d：删除已完成事件整行（此时去重窗口结束）
DELETE FROM gateway_webhook_events
 WHERE status IN ('delivered','dead','unroutable')
   AND created_at < :now_minus_30d;
```

不自动 `VACUUM`（会长时间锁库）；删除后的空洞由 SQLite 复用，需彻底收缩时在
维护窗口手动 `VACUUM`（先备份、停写）。`status` 会展示 pending、DB 大小、增长率。

## 5. 部署 sidecar（推荐；不重启主网关）

```bash
cd /opt/erp
# 1) 准备目录（宿主机；UID 需与网关容器内用户一致，默认 UID 由 gateway 镜像 useradd 决定）
install -d -m 700 /opt/erp-gateway/backups /opt/erp-gateway/keys /opt/erp-gateway/offsite
chown -R <gateway-uid>:<gateway-gid> /opt/erp-gateway/backups /opt/erp-gateway/offsite
# 密钥由网关主机的管理员用户生成，权限 600，容器只读挂载

# 2) 生成密钥（若无；容器以非 root 运行，请把 key 文件 chown 给网关容器 UID，
#    或 640 给对应组；不要改为 world-readable）
python3 scripts/gateway_backup.py snapshot \
  --key-file /opt/erp-gateway/keys/backup.key --init-key

# 3) 备份 env
cp deploy/gateway/backup.env.example /opt/erp-gateway/backup.env
chmod 600 /opt/erp-gateway/backup.env

# 4) 先只校验 compose（不启动）
docker compose -f deploy/gateway/docker-compose.yml \
  -f deploy/gateway/docker-compose.backup.yml \
  --env-file /opt/erp-gateway/.env --env-file /opt/erp-gateway/backup.env \
  --profile backup config -q

# 5) 启动 sidecar（只新增容器；不会重建/重启 gateway）
docker compose -f deploy/gateway/docker-compose.yml \
  -f deploy/gateway/docker-compose.backup.yml \
  --env-file /opt/erp-gateway/.env --env-file /opt/erp-gateway/backup.env \
  --profile backup up -d --no-deps gateway-backup

# 6) 验证
docker compose ... --profile backup logs -f --tail=50 gateway-backup
python3 scripts/gateway_backup.py status --database /data/gateway.db --backup-root /opt/erp-gateway/backups
```

实际生产若卷名不是 `erp_gateway_data`（例如 `erp-gateway-v2-data`），在
`backup.env` 设置 `GATEWAY_DATA_VOLUME=erp-gateway-v2-data`，并先在只读副本上演练。
sidecar 镜像使用当前 `erp-gateway:<tag>`；无需重新构建 gateway。

## 6. 恢复演练与真实恢复

### 6.1 演练（任何环境；本任务执行方式）

`restore-drill` 流程：

1. 校验加密文件 SHA-256 与 manifest 一致（篡改立即失败）；
2. `decrypt` 到临时目录（HMAC 校验）；
3. `gzip` 解压到临时 SQLite；
4. `PRAGMA integrity_check`、schema 指纹、关键表行数与 manifest 比对；
5. 在临时库副本上模拟 dispatcher 领取 pending 事件（事务内 `UPDATE` 后 `ROLLBACK`）；
6. 输出 `drill-report-*.json`；不接触生产 volume，不重启容器。

本次本地演练证据（2026-11-14，临时目录/合成 1,200 条已完成事件；未接触生产）：

```text
源 SQLite            1,753,088 bytes
加密快照              909,748 bytes  sha256=3ff30726...f7ed81a79（压缩+加密）
快照 integrity_check  ok；schema/表行数/manifest 一致
dispatcher 领取      claimable_pending=1，claimed=1（事务内领取后回滚）
retention dry-run    would_scrub=364，would_delete=2，protected_incomplete=1（pending 永不删）
磁盘预算             319,291,009 bytes ≈ 0.30 GiB（recent 24h×4 + daily30，双副本）
自测                  consistent_online_snapshot / tamper_detection / restore_drill /
                      retention_policy / disk_budget_and_status 全部 ok
```

生产首次上线建议先在 `/tmp` 用最新备份跑 `restore-drill`，保留 drill-report 作为证据；
实际数字以 `python3 scripts/gateway_backup.py self-test` / `status` 输出为准。

### 6.2 真实恢复（维护窗口）

```bash
# 1) 在任何恢复前，先备份当前卷（如果还能读）；
# 2) 在隔离主机/临时目录先做 restore-drill，确认备份可用；
# 3) 维护窗口停止 gateway 写入（docker compose stop gateway；不删除数据卷）；
# 4) 用恢复出的 gateway.db 替换新数据卷中的文件（先保留旧卷/旧文件作为回滚资产）；
# 5) 启动 gateway，检查 /healthz、实例 active、OAuth state、webhook backlog；
# 6) 观察 15 分钟无异常后，再清理旧卷（需 Lead 审批）。
```

密钥必须与备份匹配：`GATEWAY_SECRET_KEY` 丢失 → 渠道 secret 无法解密；
`GATEWAY_BACKUP_KEY_FILE` 丢失 → 加密备份无法恢复。两者都应独立托管/异地保存。

## 7. 磁盘预算

公式（保守：加密后大小 ≈ 压缩后 DB；不假设压缩率）：

```text
保留份数 = recent_hours × 4 + daily_days          # 15 分钟一份
预算     ≈ 最新加密快照大小 × 保留份数 × 副本数
           × 1.10（manifest/临时/审计）+ 64MiB 余量
```

默认 `recent_hours=24`、`daily_days=30` → 126 份；本地 + 异机 `offsite=local/rsync`
时副本数=2。示例（按加密快照大小）：

| 加密快照大小 | 单副本保留 | 本地+异机 | 备注 |
| ---: | ---: | ---: | --- |
| 5 MiB | ~0.7 GiB | ~1.4 GiB | 小网关 |
| 20 MiB | ~2.8 GiB | ~5.5 GiB | 百店中等 |
| 50 MiB | ~6.9 GiB | ~13.9 GiB | 需独立备份盘 |
| 100 MiB | ~13.9 GiB | ~27.7 GiB | 需扩大备份盘/降低 recent_hours |

建议：备份放独立 30–50GB 数据盘；若磁盘紧张，把 `GATEWAY_BACKUP_RECENT_HOURS`
降到 6（24 份，仍保持 15 分钟 RPO）并按 `disk-budget` 实测重算。

## 8. 上线窗口与风险

| 步骤 | 是否影响生产 | 说明 |
| --- | --- | --- |
| 生成密钥、创建目录 | 否 | 只写备份/密钥目录 |
| sidecar `config -q` | 否 | 只校验 |
| 启动 sidecar（`up -d --no-deps`） | 否 | 新增容器；只读挂载数据卷 |
| 首次快照 | 轻微 I/O | SQLite online backup 分步读取；避开高峰 |
| `retention --apply` | 是（写库） | 需窗口：先快照 + dry-run + 确认 pending 不删 |
| 真实恢复 | 是（停写/停容器） | 需维护窗口与 Lead 审批，先保留旧卷 |

风险：

1. **磁盘满**：备份盘与 `/data` 分开；sidecar 失败要告警；定期看 `status` 的 used/quota。
2. **密钥丢失/放错**：加密备份与密钥分离保存；密钥泄露=渠道凭据泄露，按安全事件处理。
3. **sidecar 权限**：备份目录/密钥只有对应 UID 可读；脚本目录只读。
4. **SQLite 备份时间**：DB 过大时 snapshot 变慢；15 分钟间隔内未完成会跳过（锁文件防重入）。
5. **事件清理**：只删 `delivered/dead/unroutable` 且超 30 天；pending/retry 永不删除；
   `--apply` 前必须备份并先 `--dry-run` 核对计数。
6. **生产卷名不一致**：`erp-gateway-v2-data` vs 标准 `erp_gateway_data`，backup.env 必须覆盖；
   首次仅在只读副本验证后再挂生产。

## 9. 与 task-46 / 其他任务边界

* 本任务未修改 `backend/app/modules/gateway/**`；task-46 继续占用核心代码。
* `gateway_webhook_events` 的状态/列由 task-41/46 演进；脚本通过 `PRAGMA table_info`
  动态适配 `payload_cipher`/`lease_*` 列，不硬编码迁移版本。
* 未改 agent/frontend、未改生产配置/密钥/流量。
