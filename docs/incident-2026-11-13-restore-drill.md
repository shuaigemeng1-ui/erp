# 事故记录：隔离恢复演练误挂主 MySQL 卷导致主租户库被清空

- 日期：2026-11-13
- 环境：租户服务器 `8.163.80.17`，Compose 项目 `ventasync`
- 严重级别：高（主租户中心库表被清空、主业务容器停止）
- 当前状态（2026-11-13 17:50 CST）：恢复脚本已修复；生产业务库已从 075322Z 备份恢复并修正 TENANT_ID=3；094919Z 演练通过并自动匹配 `backup_records id=2`、写入 `restore_status=verified`（`restore_verified_at=2026-11-13 09:49:54`），**记录链已闭环**。前提是 `BACKUP_INSTANCE_ID=1` 已配置（必配）。详见第 9 节。
- 关联：批次3 task-31（备份加密异地与恢复演练），脚本 `deploy/scripts/restore_drill.sh`

## 1. 事件经过

1. 在 `8.163.80.17` 执行演练命令：
   `bash deploy/scripts/restore_drill.sh --latest --record-latest`
2. 演练脚本用 `docker compose -p ventasync-drill-<ts> -f docker-compose.yml -f docker-compose.server.yml -f drill-override.yml`
   启动隔离 MySQL 并导入备份。
3. 随后发现**主租户 MySQL 的 `ventasync_center` 被清空（0 表）**、主业务容器全部停止。
4. 使用 15:53 的备份恢复 `ventasync_center`（30 表）并 `docker compose up -d`，租户业务恢复（订单 200 等）。

## 2. 根因

`deploy/docker-compose.yml` 顶部卷使用**显式卷名**：

```yaml
volumes:
  mysql_data:
    name: ${COMPOSE_PROJECT_NAME:-ventasync}_mysql_data
  redis_queue_data:
    name: ${COMPOSE_PROJECT_NAME:-ventasync}_redis_queue_data
  catalog_media_data:
    name: ${COMPOSE_PROJECT_NAME:-ventasync}_catalog_media_data
```

Compose 的显式 `name:` **优先于** `docker compose -p <project>` 的项目前缀。旧版演练脚本：

* 只加了 `-p ventasync-drill-<ts>`，未覆盖顶层卷名；
* drill 的 MySQL 因此仍挂载主卷 `ventasync_mysql_data`，与主 MySQL 同时使用同一数据目录；
* 备份导入执行 `CREATE DATABASE ventasync_center` / `DROP TABLE`，直接写到主卷 → 主中心库被清空；
* 演练结束时 `docker compose -p drill down -v` 按配置声明的卷名清理，**删除了主卷**，导致主容器无法正常启动。

辅因：

* 启动前没有解析最终 compose 配置做“卷必须属于 drill 项目”的断言；
* 启动后没有检查容器实际挂载卷；
* 清理没有保护（只要 `-p drill` 就执行 `down -v`）。

## 3. 已执行的恢复（事件当晚）

1. 停止/清理异常 drill 资源（不重复删除业务数据）；
2. 用 **15:53 备份**恢复主中心库 `ventasync_center`（恢复后 30 张表）；
3. `docker compose up -d` 拉起主业务容器；核对租户订单（200）等关键数据；
4. 主租户运营恢复，未再执行任何演练。

## 4. 修复内容（本次代码变更）

### 4.1 `deploy/scripts/restore_drill.sh` 重写（核心）

1. **双重项目名绑定**：所有 compose 调用统一为
   `COMPOSE_PROJECT_NAME=<drill 项目> docker compose -p <drill 项目> ...`，
   从根上让 base 中 `${COMPOSE_PROJECT_NAME}_*` 解析为 drill 项目名；
2. **overlay 显式卷名**：生成的 override 把 `mysql_data/redis_queue_data/catalog_media_data/registry_data`
   全部改为 `<drill 项目>_*`，并清空 mysql/redis 端口发布、禁用业务服务；
3. **启动前安全断言**：`docker compose config --format json` 解析最终配置，交给
   `deploy/scripts/check_drill_isolation.py` 断言：
   * 项目名必须匹配 `ventasync-drill-*` 且不等于主项目；
   * 每个解析后的卷名必须以 drill 项目名开头、不得等于/属于主项目（主项目名来自 `COMPOSE_PROJECT_NAME`）；
   * mysql/redis 挂载的命名卷必须全部属于 drill 项目；
   * mysql 不允许端口发布。
   任一不满足立即中止，**不启动任何容器**。
4. **人工确认/只读模式**：默认要求输入 drill 项目名确认；`--yes` 供自动化；`--dry-run`
   只做解密校验与 compose 安全断言，不执行任何 docker 启动/清理命令。
5. **启动后再校验**：`docker inspect` 校验容器名含 drill 项目、mysql 所有挂载卷名以 drill 项目开头；
   不满足立即中止（不会对主项目执行 down/rm）。
6. **清理保护**：只执行 `COMPOSE_PROJECT_NAME=<drill> docker compose -p <drill> down -v`，且：
   * 仅在启动前安全断言通过（`SAFETY_VERIFIED=1`）后才允许 docker 清理；
   * 项目名必须匹配 `ventasync-drill-*`；
   * 清理时再次固定 `COMPOSE_PROJECT_NAME=<drill>`，防止 base compose 的 `${COMPOSE_PROJECT_NAME}_*`
     在 down 时回退为主项目卷名；
   * `--keep` 保留演练环境；任何异常路径都不会调用主项目的 down/up。dry-run 不执行 docker 清理。

### 4.2 新增 `deploy/scripts/check_drill_isolation.py`

把上述断言做成独立脚本（stdin/`--config` 读取 compose config JSON），可单独在服务器执行只读校验：

```bash
PROJECT=ventasync-drill-manual-check
COMPOSE_PROJECT_NAME=$PROJECT docker compose -p $PROJECT \
  -f deploy/docker-compose.yml -f deploy/docker-compose.server.yml -f /tmp/drill-override.yml \
  config --format json > /tmp/drill-config.json
python3 deploy/scripts/check_drill_isolation.py --project $PROJECT --main-project ventasync --config /tmp/drill-config.json
```

### 4.3 文档与演练流程

* `docs/backup-restore.md` §8 增加「事故后硬性规则」与 `--dry-run`/`--yes` 流程；
* `deploy/README.md` 备份章节同步安全说明；
* 本文档作为事故记录与防复发依据。

## 5. 防复发规则（强制）

1. **禁止**在未通过 `--dry-run` 的生产主机上直接执行真实演练；
2. 演练必须在 **drill 专属项目 + drill 专属卷** 上运行；任何卷名不含 `drill`、卷名属于主项目的情况一律视为失败；
3. 修改 `deploy/docker-compose.yml` 顶层卷/网络命名时，必须同步检查 `check_drill_isolation.py` 与演练脚本；
4. 演练失败只允许清理 drill 项目资源；**禁止** `docker compose down -v` 不带 `-p`，禁止在仓库根目录直接执行 compose；
5. 每次演练前确认主项目最近备份可用（本次事故靠 15:53 备份恢复）；
6. 演练执行后必须保存：`--dry-run` 输出（卷名列表）、演练报告 `drill-report.json`、
   `docker inspect` 的容器/卷证据、中心库 `backup_records.restore_status`。

## 5.1 二次重跑（2026-11-13 下午）与 ERROR 2002 修复

Lead 在 8.163.80.17 用修复版脚本重跑（独立卷/安全断言已生效）：

- 安全断言通过：drill 项目 `ventasync-drill-*`、卷名全部为 drill 专属、mysql 无端口发布；
- 主卷 `ventasync_mysql_data` 与主 `ventasync_center`（30 表）**未受损**，主容器正常、租户 HTTP 200；
- 但 step5 导入阶段报 `ERROR 2002 (HY000): Can't connect to local MySQL server through socket`，
  `drill_exit=1`：MySQL 容器刚启动/初始化阶段，`mysqladmin ping` 已可成功，但 socket 尚不能稳定接受
  真实连接，导入命令抢跑失败。

修复（本脚本，commit 见仓库日志）：

1. **双探针就绪等待**：`mysqladmin ping` + `mysql -N -B -e 'SELECT 1'` 同时成功、连续 3 次（间隔 2s）
   才继续，最多 120s；超时打印 `docker inspect` 状态与 `docker logs --tail 200`，退出码 1。
2. **导入前硬校验**：
   - `SELECT @@datadir` 必须是 `/var/lib/mysql`；
   - `/var/lib/mysql` 挂载的命名卷必须属于 `<drill 项目>_*`，且不得属于主项目；
   - `SELECT @@read_only = 0`，并用临时表创建/删除做写入探针；
   - 目标库必须全新：`ventasync_center` 与各租户库 BASE TABLE 数必须为 0（drill 独立卷首次启动）。
3. **导入后表数核对**：逐库比较 `dump 中 CREATE TABLE 数` 与 `information_schema BASE TABLE 数`；
   不一致立即打印容器日志并中止，不生成“通过”报告。
4. **报告增强**：`drill-report.json` 增加 `target`（drill 项目、datadir、数据卷、主项目、就绪判据）
   与 `import_table_counts` 检查项；只有全部检查（含表数一致）通过才会 `passed=true`。

修复后验证（本机）：`bash -n`、`--self-test`、真实 compose `--dry-run` 均通过；服务器实际重跑结果见第 9 节（新备份 passed=true/28 checks）。

## 6. 安全重跑步骤（8.163.80.17，必须先 dry-run）

```bash
cd /opt/ventasync
# 0) 只读检查：确认 drill 解析卷名全部以 ventasync-drill-* 开头，mysql 无端口发布
sudo -u erp-agent-exec bash deploy/scripts/restore_drill.sh --latest --dry-run

# 1) 临时卷演练（独立项目 + 独立卷；默认需输入项目名确认，自动化可加 --yes）
sudo -u erp-agent-exec bash deploy/scripts/restore_drill.sh --latest --yes

# 2) 证据
cat /var/lib/ventasync-backups/drill-reports/*/drill-report.json | tail -n 40
docker volume ls | grep ventasync-drill      # 演练结束后应已被清理（--keep 时保留）
docker ps -a | grep ventasync-drill          # 应为空
```

预期：dry-run 输出中 `volumes` 全部为 `ventasync-drill-<ts>_*`、`mysql ports: []`，退出码 0；
真实演练只创建 `ventasync-drill-*` 容器/卷，主项目 `ventasync` 容器与卷在演练前后
`docker ps`/`docker volume inspect ventasync_mysql_data` 时间戳与内容不变。

## 7. 验收状态

- [x] 根因定位与修复代码
- [x] 本地自测：`--self-test`（含“复用主卷配置必须被拒绝”）通过
- [x] 本机 `--dry-run`（真实 deploy compose 文件）解析卷名全部为 drill 专属、`mysql ports: []`、退出码 0
- [x] Lead 在 8.163.80.17 完成第一次修复后重跑：独立卷/安全断言通过、主卷与 center 30 表未受损
- [x] 定位并修复第二轮失败（ERROR 2002：socket 未就绪）——双探针就绪等待 + 导入前后校验 + 报告 target
- [x] 8.163.80.17 再次 `--dry-run` 通过（只读，留存卷名输出）
- [x] 8.163.80.17 临时卷 actual 演练通过：新备份 093801Z 的 `drill-report.json` passed=true / failed=0 / 28 项检查，
      主卷 CreatedAt/Mountpoint 不变、主容器未重启、主 center 30 表/业务 34 表不变、无 drill 残留
- [x] Lead 批准后中心库 `backup_records.restore_status=verified` 写入：094919Z 演练自动匹配 `id=2`，
      `restore_verified_at=2026-11-13 09:49:54`（`BACKUP_INSTANCE_ID=1` 已配置）

## 8. 文件导入修复及实际演练结果（2026-11-13 17:30 CST）

第 5–7 节是此前修复过程记录；当前实现与证据以本节及第 9 节为准。

### 根因与修复

- 中心库 dump 第 22 行含 CREATE DATABASE ventasync_center，第 24 行含 USE ventasync_center，会覆盖前置 USE drilltest。
- actual20.log / actual21.log 第 36 行真实错误为 Unknown command '\`'，由拼接 USE 时多余的反斜杠导致。末尾 ERROR 2002 是旧脚本固定提示，不能作为这两次执行的真实错误。
- 保留 dump 原 schema；仅在独立 MySQL 中导入。docker cp SQL 后验证 SHA-256，由容器内 shell 文件重定向导入，不再通过 docker exec -i 管道。
- 就绪要求 PID 1 mysqld 和 TCP 127.0.0.1:3306 认证 SELECT 1 连续三次成功。查询失败直接失败，表数和表名都必须与备份匹配。

### 实际结果

| 备份/候选包 | 实际 schema | 导入前 → 后 | 整体结果 |
| --- | --- | --- | --- |
| releases/20261113T075459Z | ventasync_center | 0 → 30 | failed：缺少活跃租户业务库 |
| releases/20261113T075322Z | ventasync_center / vs_tenant_kangkang_shop | 0 → 30 / 0 → 34 | failed：归档 TENANT_ID=1，数据 tenant_id=3 |
| recovery-candidates/20261113T075322Z-tenant3 | 同上 | 0 → 30 / 0 → 34 | passed=true，28 项通过，退出码 0 |

候选包由 075322 原完整包派生：仅将归档 config/.env 的 TENANT_ID 从 1 改为 3，并重算清单与加密包校验值；manifest 记录 recovery_source_run_id、recovery_adjustments 和独立 run_id。原始包未修改，原始备份记录未标记 verified。

已验证：负责人访问权限 1 条、设置 1 条、SPU 1 条、SKU 1 条、标识/别名 1 条；角色/权限范围、SKU/SPU、标识/SKU 无孤儿关联；3 项加密凭据可用归档密钥解密。范围是数据关联、配置结构与解密，未启动业务服务、未做交互登录或对象存储连通测试。

服务器持久报告：

- 原指定包：`/var/lib/ventasync-backups/drill-reports/ventasync-drill-20261113092433-2202/drill-report.json`
- 原完整包最终复验：`/var/lib/ventasync-backups/drill-reports/ventasync-drill-20261113092746-32686/drill-report.json`
- 修正配置候选包：`/var/lib/ventasync-backups/drill-reports/ventasync-drill-20261113092953-30453/drill-report.json`

### 复现

在 8.163.80.17 执行下列命令，只创建新的隔离 MySQL，结束后清理该次容器、卷、网络，报告保留。可先把 --yes 替换为 --dry-run。

```bash
bash /opt/ventasync/deploy/scripts/restore_drill.sh \
  --bundle /var/lib/ventasync-backups/recovery-candidates/20261113T075322Z-tenant3/bundle.tar.gz.enc \
  --yes
```

预期：center before=0/after=30，tenant before=0/after=34，passed=true、failed=0、cleanup_status=removed，退出码 0。

若改用最初的 releases/20261113T075459Z 包，中心库恢复 0→30，但整体必须 passed=false、退出码 1，因为缺少 vs_tenant_kangkang_shop。

### 隔离与生产现状

演练仅使用独立 ventasync-drill-* 项目、随机密码、内部网络、无端口发布及单一新卷。启动和清理使用同一份冻结 Compose JSON，核对容器/卷标签、local driver、无 driver_opts、实际 Source 与生产不同。不会挂载或删除生产 ventasync_mysql_data。

演练前后生产 MySQL 容器 768236ec8d44、启动时间 2026-11-13T07:54:07.132670906Z、卷 ventasync_mysql_data、目录 /var/lib/docker/volumes/ventasync_mysql_data/_data、中心库 30 表、账号 1 条、成员 1 条一致。本轮未重启生产服务或导入生产数据。演练资源均已清理，租户首页及 healthz 返回 HTTP 200。

生产当前仅有 ventasync_center，但路由仍指向 mysql:3306/vs_tenant_kangkang_shop；API 实际 TENANT_ID=1，中心库租户 ID=3。这些在本轮开始时已存在，HTTP 200 不能证明商品业务正常。后续需单独恢复生产业务库并修正绑定，再完善备份覆盖检查。本轮只修正隔离候选配置，未触碰主 MySQL 卷。

> 上述生产业务库缺失与 TENANT_ID 不一致已由第 9.1 节恢复修正；新备份隔离演练结果见第 9.2 节。

### 验证与部署

- Linux 服务器 --self-test、原始包及候选包 --dry-run 通过。
- 21 项针对性回归通过：dump/schema、卷隔离、查询失败、孤儿关联、合法空商品、密钥算法、绑定冲突。
- npm run build 通过，构建输出到仓库外临时目录；未启动本地前后端。
- 仅部署恢复脚本及 helper；未发布其它 agent 的 gateway/onboarding 应用变更，未执行生产 Compose up/down。

## 9. 生产业务库恢复与新备份演练（2026-11-13 17:40 CST）

### 9.1 生产业务库缺失与 TENANT_ID 修正

演练事故后进一步核对发现：`8.163.80.17` 仅有 `ventasync_center`，**业务库 `vs_tenant_kangkang_shop` 缺失**；
API 配置 `TENANT_ID=1`，而中心库该租户实际 `tenant_id=3`，路由与绑定不一致，HTTP 200 不能证明商品业务可用。

已执行的生产恢复（由试点流程执行，QA 仅记录）：

| 步骤 | 实际结果 |
| --- | --- |
| 业务库恢复 | 从 `/var/lib/ventasync-backups/releases/20261113T075322Z/vs_tenant_kangkang_shop.sql.gz` 恢复到 `vs_tenant_kangkang_shop` |
| schema/表数 | **34 张表**，`alembic_version=tenant_0014` |
| 数据库账号 | 创建/授权 `vs_t_3`（租户 3 业务账号） |
| 配置修正 | `.env` 改为 `TENANT_ID=3`、`ERP_WORKER_TENANT_ID=3` |
| 服务重启 | 重启 `api` / `worker` / `scheduler` |
| 业务验证 | 登录 **200**、`products` **200**、租户站点 HTTP **200** |
| 恢复后备份 | `/var/lib/ventasync-backups/releases/20261113T093925Z` |

> 原批次 2 记录中的 `TENANT_ID=1` 为错误绑定，已用本轮生产恢复修正为 `3`；批次 2 历史记录中该值仅代表当时配置。

### 9.2 新备份隔离恢复演练（实际通过）

使用 9.1 恢复后的新备份做隔离演练（修复版脚本、独立项目/独立卷）：

- 备份包：`/var/lib/ventasync-backups/releases/20261113T093801Z/bundle.tar.gz.enc`；
- 命令：`bash deploy/scripts/restore_drill.sh --bundle <上述包> --yes`；
- 报告：`/var/lib/ventasync-backups/drill-reports/ventasync-drill-20261113093839-6570/drill-report.json`；
- 结果：**passed=true、failed=0、28 项检查通过**；
- 主库/容器核对：主 `ventasync_center` **30 表**、业务 `vs_tenant_kangkang_shop` **34 表**，主容器**未重启**；
- 清理：drill 容器/卷已清理，无 drill 残留。

这说明第 4.1/5.1 节的隔离修复与安全断言已在实际演练中生效；本轮演练只使用 drill 专属项目/卷，不触碰主卷。

### 9.3 记录关联修复与 094919Z 闭环

历史问题（已修复）：

- `--record-latest` 曾报 `未找到唯一匹配本地 run_id/校验值的备份记录`；
- `backup_records` 当时只有 `id=1`，`local_path`/`checksum` 为空，`restore_status` 未写 verified。

修复与验证：

- backend-sales 修复备份记录与本地 `local_path`/`run_id`/校验值的精确关联（commit `b485bee`）；
- 8.163.80.17 设置 **`BACKUP_INSTANCE_ID=1`**（与中心 `tenant_instances.id` 一致），生成 094919Z 备份；
- 执行 `bash deploy/scripts/restore_drill.sh --latest --record-latest --yes`；
- 结果：**passed=true、failed=0**；自动匹配 **`backup_records id=2`**，写入
  `restore_status=verified`、`restore_verified_at=2026-11-13 09:49:54`；
- 报告：`/var/lib/ventasync-backups/drill-reports/ventasync-drill-20261113094923-23419/drill-report.json`；
- 主库/容器：主 `ventasync_center` **30 表**、业务 `vs_tenant_kangkang_shop` **34 表**，租户 HTTP 200，主容器未重启。

结论：**恢复演练记录链已闭环**（报告、平台 `backup_records`、`restore_status=verified` 一致），不再需要人工补写。

### 9.4 `BACKUP_INSTANCE_ID` 必配

- `BACKUP_INSTANCE_ID` 必须等于中心库 `tenant_instances.id`（8.163.80.17 为 **1**）；
- 未配置时备份记录可能缺少 `instance_id`，`--record-latest` 无法唯一匹配，演练记录无法自动回写；
- 该变量已写入 `deploy/scripts/backup.sh` / `restore_drill.sh` 读取逻辑与 `deploy/.env` 模板说明；
- 部署/验收清单必须检查：备份、恢复演练前先确认 `BACKUP_INSTANCE_ID` 已设置并与实例 ID 一致。

### 9.5 验收口径更新

- 旧版脚本在 15:53 造成的事故记录仍有效（第 1–8 节），修复过程与防复发规则保留；
- 修复版脚本在 093801Z 演练执行通过（28 checks），并在 094919Z 演练中完成记录闭环（id=2/verified）；
- **批次 3 恢复演练验收状态 = 通过（演练执行 + 记录回写均闭环）**，前提是 `BACKUP_INSTANCE_ID` 已配置；
- 后续正式演练：建议先 `--dry-run`，再 `--latest --record-latest --yes`（或显式 `--bundle ... --record-id`），
  保存报告并核对 `backup_records.restore_status=verified`。
