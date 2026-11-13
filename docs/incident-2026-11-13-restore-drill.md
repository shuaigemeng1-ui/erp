# 事故记录：隔离恢复演练误挂主 MySQL 卷导致主租户库被清空

- 日期：2026-11-13
- 环境：租户服务器 `8.163.80.17`，Compose 项目 `ventasync`
- 严重级别：高（主租户中心库表被清空、主业务容器停止）
- 状态：主租户运营已恢复；批次3「恢复演练」验收暂时不通过，待修复后在隔离临时卷上安全重跑
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
6. **清理保护**：只执行 `docker compose -p <drill> down -v`，且项目名必须匹配 `ventasync-drill-*`；
   `--keep` 保留演练环境；任何异常路径都不会调用主项目的 down/up。dry-run 不执行 docker 清理。

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

## 6. 安全重跑步骤（8.163.80.17，必须先 dry-run）

```bash
cd /opt/ventasync
# 0) 只读检查：确认 drill 解析卷名全部以 ventasync-drill-* 开头，mysql 无端口发布
sudo -u erp-agent-exec bash deploy/scripts/restore_drill.sh --latest --dry-run

# 1) 临时卷演练（独立项目 + 独立卷；默认需输入项目名确认，自动化可加 --yes）
sudo -u erp-agent-exec bash deploy/scripts/restore_drill.sh --latest --yes

# 2) 证据
cat /var/lib/ventasync-backups/drill-*/drill-report.json | tail -n 40
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
- [ ] 8.163.80.17 只读 `--dry-run` 通过（待服务器执行并留存输出）
- [ ] 8.163.80.17 临时卷演练通过且主项目卷/容器无变化（待服务器执行并留存证据）
- [ ] 中心库 `backup_records.restore_status=verified` 重新写入
