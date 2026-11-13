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
| 5 | 恢复演练脚本（⛔ 安全冻结） | `deploy/scripts/restore_drill.sh` 设计与实现不满足生产隔离要求，试点中发生事故（见第 4 节）；当前**禁止生产运行**，待 backend-sales 修复独立卷/安全断言后重新验收 |

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

## 4. 恢复演练事故与处置（2026-11-13，8.163.80.17）

> 本节为事故记录，**恢复演练状态 = 阻断/未通过**，不得写作“恢复演练通过”。

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

### 4.4 结论与处置

1. **当前 `restore_drill.sh` 不安全**：未真正实现独立卷/安全断言，旧文档“独立项目、独立卷、不碰生产卷、无需停生产”的断言不成立；
2. **恢复演练状态 = 阻断/未通过**，不得在 `backup_records` 写 `restore_status=verified`，不得宣称恢复演练通过；
3. **禁止再在任何生产机运行当前 `restore_drill.sh` 的任何模式**（`--latest`/`--bundle`/`--record-latest`/`--self-test` 用于生产）；
4. 待 backend-sales 修复“独立卷 + 启动前安全断言 + dry-run/确认 + 危险操作防护”后，先在隔离主机验证，再由 Lead 审批恢复演练流程；
5. 生产数据库恢复过渡期只能走“停写 → 最新备份 → 人工恢复中心/租户库 → 表数/权限/业务校验 → 恢复服务”的应急流程，且需 Lead 审批。

## 5. 文档修正

- `docs/backup-restore.md` 顶部新增 **STOP 安全冻结**警告；删除/撤回旧版“真实机执行前提/执行命令/隔离规则/通过标准”与生产执行命令；
- 删除“独立卷、不碰生产卷、无需停生产”的错误断言，改为事故记录 + 修复要求；
- `docs/data-model.md` 增加 `center_0018` 迁移基线及 `backup_records` 新字段/枚举（含恢复验证状态），迁移数更新为 32；
- `docs/acceptance.md` 顶部更新为批次 3 状态并链接本记录。

## 6. 剩余风险与待完成

1. **恢复演练修复与重新验收**：独立卷/安全断言/隔离主机验证未完成；当前恢复演练阻断；
2. **恢复状态数据**：事故前备份无 `backup_records` ID，后续演练/恢复需确保记录可追溯（建议所有生产恢复前备份必须 `--record`）；
3. **生产应急恢复流程**：需 Lead 审批与维护窗口；本次事故恢复未经过标准 backup_records 审计链；
4. **监控/告警真实环境**：批次 3 监控指标与告警在真实服务器/通知渠道的持续效果仍需观察；
5. **第二租户/网关/维护状态**：沿用前批次剩余项，未在本次完成。

## 7. 结论

- 批次 3 离线交付与契约同步完成：后端 591 passed、Agent 22 passed、契约 38/38、迁移链 `center_0018`/`tenant_0014`、
  前端构建与 Compose 校验通过；生产 `3ae757e` 部署、job 43 成功、tenant 200、监控/性能/备份 UI 上线。
- **恢复演练因 8.163.80.17 事故判定为阻断/未通过**：当前脚本不安全，已禁止生产运行；恢复本身已用备份完成
  （中心库 30 表、租户 HTTP 200、再次备份 exit 0），但不得将事故恢复当作“恢复演练通过”。修复并重新验收后方可恢复演练。
