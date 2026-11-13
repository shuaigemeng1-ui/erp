# 批次 3 验收记录（2026-11-13，离线 3ae757e → 生产试点 3ae757e）

- 验收提交：`3ae757e`（`fix(frontend): 监控总览优先使用业务请求性能（task-32）`；含批次 3 监控/性能/备份/恢复演练相关提交）
- 执行角色：deploy-qa（task-33）；本记录为只读验收与证据整理
- **安全声明**：QA 未运行 `restore_drill.sh` 任何模式，未连接/修改生产；生产变更由试点流程执行，QA 只收取证
- 批次 2 记录见 `docs/batch2-acceptance.md`；备份实现说明见 `docs/backup-restore.md`

## 1. 批次 3 交付内容

| # | 交付 | 说明 |
| --- | --- | --- |
| 1 | 备份加密与异地存储 | `deploy/scripts/backup.sh` 备份中心库/租户库/配置/必要文件，本地目录 700、文件 600，产物移出 Git 工作目录（`/var/lib/ventasync-backups`）；sha256 校验、加密包、异地上传、过期清理、`backup_records` 状态登记；systemd 每日 03:30 调度 |
| 2 | `center_0018` 迁移 | `backup_records` 增加触发来源、本地生成/校验、异地上传/加密、恢复验证与阶段流水/清单等字段，支持四阶段追踪；无外键、中文字段注释、旧记录默认值兼容 |
| 3 | 监控与告警 | 请求性能指标（`core/request_metrics.py`）、监控汇总/公网探测/指标聚合、告警通知、独立 `watchdog` 进程与 systemd；监控相关后端接口与前端总览 |
| 4 | 备份/恢复管理界面 | 平台备份记录/阶段状态展示、恢复验证状态、维护窗口展示；前端监控总览、备份阶段与维护窗口页面（构建通过） |
| 5 | 恢复演练脚本（执行通过、记录待修） | `deploy/scripts/restore_drill.sh` 事故后重写独立卷/安全断言/就绪探针；新备份 093801Z 实测 **passed=true / failed=0 / 28 checks**；遗留 `backup_records` 关联缺陷（`--record-latest` 不可用）待 backend-sales 修复（见第 4 节） |

## 2. 离线回归与构建结果（HEAD 3ae757e）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量测试 | `cd backend; python -m pytest -q` | **exit 0，591 passed**（1 条上游 Starlette 弃用提示） |
| Agent 测试 | `cd agent; python -m pytest -q` | **exit 0，22 passed** |
| 完整契约检查 | `cd backend; python scripts/verify_contracts.py` | **exit 0，38/38 通过，0 失败 0 警告**（补齐 `center_0018` 迁移基线后） |
| 迁移链 | `python -m alembic -c alembic/center.ini heads` / `tenant.ini heads` | `center_0018 (head)`、`tenant_0014 (head)`；共 32 个迁移文件（center 18 + tenant 14） |
| 离线迁移 SQL | 两套 `upgrade head --sql` | 中心库 30 条 CREATE（业务 29 张）、租户库 34 条（业务 33 张）；FOREIGN KEY 0、REFERENCES 0；表注释 29/33、字段注释 387/395 |
| 前端生产构建 | `cd frontend; npm run build -- --outDir dist-b3-check` | **exit 0**（vue-tsc + vite）；vite 7.45s；55 文件 / 2.00 MB；临时目录已删除，未改受版本管理的 `frontend/dist` |
| Compose 编排 | 默认 / `--env-file deploy/.env.example` / `--profile release` 三种 `config -q` | 均 exit 0 |

契约关键数字：中心库 29 张表、租户库 33 张表；迁移文档 32 条与实际版本图一致；末端 `center_0018` / `tenant_0014`；
文档路由 94 条全部存在；零外键、中文注释（模型 + 迁移 SQL）全部通过。

> 说明：`verify_contracts.py` 在补齐 `center_0018` 文档前为 37/38（唯一失败为迁移文档遗漏）；本记录完成后 38/38。

## 3. 生产部署与试点（3ae757e）

| 项目 | 实测结果 |
| --- | --- |
| 生产版本 | `3ae757e`，中心与租户均已部署 |
| 租户升级 | **job 43** `succeeded`，`checks_status=passed`，target/actual=`3ae757e` |
| 租户服务 | 租户站点 HTTP 200 |
| 监控/性能/备份 UI | 批次 3 监控总览、请求性能、备份阶段与维护窗口页面已上线 |

## 4. 恢复演练事故、修复与处置（2026-11-13，8.163.80.17）

> 旧版脚本在 15:53 造成主库清空事故（已恢复）；修复版脚本在新备份上**演练执行通过**
> （passed=true / failed=0 / 28 checks）。但 `backup_records` 关联缺陷导致 `--record-latest` 失败、
> `restore_status` 未写 verified，**验收记录链尚未闭合**（详见 4.5/4.6）。

### 4.1 事故经过

- 时间：2026-11-13 约 **15:53 CST**；
- 地点/对象：`8.163.80.17` 租户服务器，执行旧版 `deploy/scripts/restore_drill.sh`；
- 影响：**主 MySQL 的 `ventasync_center` 被清空（0 表）**，主业务容器被停止；
- 背景：预演前已有当日备份，因此具备恢复条件；该次备份使用 `--no-record` 生成，**无 `backup_records` ID**。

### 4.2 用于恢复的备份（如实记录）

| 项目 | 证据 |
| --- | --- |
| 备份目录 | `/var/lib/ventasync-backups/releases/20261113T075322Z` |
| 中心库文件 | `ventasync_center.sql.gz`，**138866 字节** |
| 文件时间 | UTC **2026-11-13 07:53:22**（CST 15:53:22） |
| 记录状态 | 该次备份为 `--no-record`，**无 backup_records ID**，审计链以目录/文件时间与本节记录为准 |

### 4.3 恢复过程与结果

| 步骤 | 实际执行/结果 |
| --- | --- |
| 中心库恢复 | `gunzip -c .../ventasync_center.sql.gz \| docker compose ... exec -T mysql ... mysql -uroot` |
| 恢复后表数 | `information_schema.tables` 统计 `ventasync_center` = **30 表** |
| 服务恢复 | `docker compose -f docker-compose.yml -f docker-compose.server.yml --env-file .env up -d`；api **healthy**、nginx **started** |
| 租户站点 | `https://zuhu.alienyan.store/` → **HTTP 200** |
| 恢复后再次备份 | `/var/lib/ventasync-backups/releases/20261113T075459Z`，命令 **exit 0** |
| 恢复后升级 | job 43 成功（见第 3 节），target/actual=`3ae757e`，tenant HTTP 200 |
| 业务库恢复（后续） | 从同备份目录的 `vs_tenant_kangkang_shop.sql.gz` 恢复到 `vs_tenant_kangkang_shop`：**34 表**、schema `tenant_0014` |
| 数据库账号 | 创建/授权 `vs_t_3`（租户 3 业务账号） |
| 绑定修正 | `.env` 改为 `TENANT_ID=3`、`ERP_WORKER_TENANT_ID=3`，重启 `api`/`worker`/`scheduler` |
| 业务验证 | 登录 **200**、`products` **200**、租户站点 HTTP **200** |
| 恢复后新备份 | `/var/lib/ventasync-backups/releases/20261113T093925Z` |

### 4.4 新备份隔离演练（实际通过）

| 项目 | 证据 |
| --- | --- |
| 备份包 | `/var/lib/ventasync-backups/releases/20261113T093801Z/bundle.tar.gz.enc` |
| 命令 | `bash deploy/scripts/restore_drill.sh --bundle <上述包> --yes` |
| 报告 | `/var/lib/ventasync-backups/drill-reports/ventasync-drill-20261113093839-6570/drill-report.json` |
| 结果 | **passed=true、failed=0、28 项检查通过** |
| 主库/容器 | 主中心库 **30 表**、业务库 **34 表**；主容器**未重启** |
| 清理 | drill 容器/卷已清理，无残留 |

### 4.5 遗留问题：backup_records 关联缺陷

- `restore_drill.sh --record-latest` 报：`未找到唯一匹配本地 run_id/校验值的备份记录`；
- `backup_records` 当前只有 `id=1`，`local_path`/`checksum` 为空；
- 本次演练 `restore_status` **未写入 verified**；
- 需要 backend-sales 修复备份记录与本地 run_id/checksum 的关联/回写；
- 修复前：可用显式 `--bundle` + `--yes` 执行演练并保留报告，但**不得宣称 `restore_status=verified` 或记录链闭合**。

### 4.6 结论与处置

1. 旧版脚本未真正隔离独立卷/安全断言，导致 15:53 事故（根因与修复见 `docs/incident-2026-11-13-restore-drill.md`）；
2. 修复版脚本在 8.163.80.17 新备份上**演练执行通过**（28 项检查，主中心 30/业务 34 表不变、主容器未重启、drill 资源清理）；
3. **恢复演练验收状态 = 执行通过、记录回写待修复**：`backup_records` 关联缺陷未修复前，不写 `restore_status=verified`、不宣称验收闭环；
4. 后续演练必须：先 `--dry-run`，用显式 `--bundle <包>` + `--yes`，保存 `drill-report.json`；`--record-latest` 待修复后再使用；
5. 生产数据库恢复仍走“停写 → 最新备份 → 人工恢复中心/租户库 → 表数/权限/业务校验 → 恢复服务”的应急流程，需 Lead 审批。

## 5. 文档修正

- `docs/backup-restore.md` 顶部先加事故 STOP，后在修复版实测通过后更新为“运行限制/记录关联待修复”：保留显式
  `--bundle` + `--dry-run` 规则，撤回 `--record-latest` 回写；
- 删除“独立卷、不碰生产卷、无需停生产”的错误断言，改为事故记录 + 修复要求；
- `docs/data-model.md` 增加 `center_0018` 迁移基线及 `backup_records` 新字段/枚举（含恢复验证状态），迁移数更新为 32；
- `docs/acceptance.md` 顶部更新为批次 4 状态，批次 3 记录保留为历史并链接本记录。

## 6. 剩余风险与待完成

1. **恢复记录关联缺陷**：`backup_records` 仅 id=1 且 `local_path`/`checksum` 为空，`--record-latest` 无法匹配，
   本次演练 `restore_status` 未写 verified；需 backend-sales 修复后才能闭合验收记录链；
2. **演练执行边界**：修复版脚本已在 8.163.80.17 新备份上实际通过 28 项检查，但 `--record-latest` 修复前
   只能用显式 `--bundle`，且必须保留 `drill-report.json`、先 `--dry-run`；
3. **生产恢复审计链**：事故前 075322Z 备份为 `--no-record`（无 backup_records ID）；后续生产恢复前备份
   应强制 `--record`，恢复操作需 Lead 审批并记录；
4. **监控/告警真实环境**：批次 3 监控指标与告警在真实服务器/通知渠道的持续效果仍需观察；
5. **第二租户/网关/维护状态**：第二租户与独立网关尚未部署，维护状态等仍待后续批次；
6. **TENANT_ID 修正**：8.163.80.17 原 `TENANT_ID=1` 与中心 `tenant_id=3` 不一致，已在本轮恢复中修正为 3；
   相关历史记录（批次 2 试点）中的 1 仅代表当时配置。

## 7. 结论

- 批次 3 离线交付与契约同步完成：后端 591 passed、Agent 22 passed、契约 38/38、迁移链 `center_0018`/`tenant_0014`、
  前端构建与 Compose 校验通过；生产 `3ae757e` 部署、job 43 成功、tenant 200、监控/性能/备份 UI 上线。
- 8.163.80.17 事故恢复完成：中心库 **30 表**、业务库 **34 表**（schema `tenant_0014`）、`vs_t_3` 账号、
  `TENANT_ID=3`/`ERP_WORKER_TENANT_ID=3` 修正；登录 200、products 200、租户 HTTP 200；新备份 093925Z。
- 修复版 `restore_drill.sh` 在新备份（093801Z）上**演练执行通过**：passed=true / failed=0 / 28 项检查，
  主库/主容器未受影响，drill 资源已清理；但 `backup_records` 关联缺陷使 `restore_status` 未写 verified，
  **验收记录链待修复**——不写“记录已闭环/verified”。
