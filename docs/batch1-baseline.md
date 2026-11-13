# 批次 1 QA 基线（HEAD 49794e5）

- 日期：2026-11-13
- 基线提交：`49794e514b0015079f4ad0d323194cb24f411951`（`main`，`fix(nginx): SSH WebSocket 长连接超时`）
- 角色：deploy-qa（批次 1 QA）；本文件只记录基线，不做代码修复，不部署生产
- 范围：只读检查 + 离线命令；**未启动任何容器/服务，未连接 MySQL/Redis，未访问生产 8.163.80.17**
- 下游：共享任务 task-19 依赖 T1（task-16 发布版本后端）、T2（task-17 Agent 2 协议）、T3（task-18 前端）；
  本基线在依赖完成前执行，用于固定“改动前”状态与验收口径

## 1. 环境与工作区

| 项目 | 值 |
| --- | --- |
| 操作系统 | Windows（研发机） |
| Python | 3.12.10 |
| Node / npm | v24.14.0 / 11.6.1 |
| 工作区 | `C:\Users\user\Desktop\ERP`；基线执行时 `git status` 为空（执行后 T1/T2/T3 并行改动出现，见 2.5） |
| 容器/数据库 | Docker daemon 未启动；未连接 MySQL/Redis（本轮命令均为纯离线） |

## 2. 基线命令与结果

### 2.1 后端测试

```powershell
cd backend
python -m pytest -q
```

结果：**exit 0，410 passed**，1 条第三方 `StarletteDeprecationWarning`，15.41s（含收集约 18s）。
测试文件 26 个（25 个 unit + 1 个 contract）；`agent/`、`gateway/`、`watchdog/` 当前**没有** Python 测试文件。

### 2.2 前端构建

```powershell
cd frontend
npm run build -- --outDir dist-batch1-baseline   # 临时输出目录，避免改动受版本管理的 frontend/dist
```

结果：**exit 0**；`vue-tsc --noEmit` + `vite build` 均通过；vite 8.47s、命令总耗时 16.3s；
临时产物 55 个文件 / 1.94 MB，与受版本管理的 `frontend/dist`（55 个文件）一致。
构建完成后已删除临时目录，`git status` 仍为空。

> 正式构建命令仍为 `npm run build`（输出到受版本管理的 `frontend/dist`）。基线为避免污染发布产物使用了
> `--outDir` 覆盖；批次验收如需更新 dist，由 Lead 明确后再执行。

### 2.3 数据库迁移链

```powershell
cd backend
python -m alembic -c alembic/center.ini heads
python -m alembic -c alembic/tenant.ini heads
```

结果（均 exit 0）：

| 迁移库 | 当前 head | 迁移文件数 |
| --- | --- | --- |
| 中心库 | `center_0015 (head)` | 15 |
| 租户库 | `tenant_0014 (head)` | 14 |

合计 29 个迁移文件；离线 SQL 可生成、源码零外键、revision 链连续（由 2.4 契约脚本的同组检查确认）。

### 2.4 仓库契约检查（固定“8 项失败”）

```powershell
cd backend
python scripts/verify_contracts.py
```

结果：**exit 1；30 项通过、8 项失败**（共 38 项检查）。失败明细如下：

| # | 检查项 | 实际结果 | 分类 / 根因 |
| --- | --- | --- | --- |
| 1 | 中心库元数据表清单与 `docs/data-model.md` 一致 | 模型 29 张，文档 18 张；多出 `alert_events`、`backup_records`、`maintenance_windows`、`node_checks`、`node_metric_aggregates`、`node_metric_samples`、`operation_job_steps`、`operation_jobs`、`release_versions`、`server_nodes`、`tenant_instances` | **文档漂移**：ops/SSH/发布等能力已合入模型与迁移，`docs/data-model.md` 未同步 |
| 2 | 中心库表与字段中文注释完整 | 7 处缺失：`alert_events.severity`、`alert_events.status`、`release_versions.status`、`operation_jobs.job_type`、`operation_jobs.status`、`operation_job_steps.status`、`backup_records.status` | **模型注释缺失**：状态/类型字段未写中文 `comment` |
| 3 | 中心库每张表包含 `id/created_at/updated_at` | `node_metric_samples`、`node_checks`（各缺 created_at/updated_at）；`operation_job_steps`、`backup_records`（各缺 created_at/updated_at）；`node_metric_aggregates`（缺 updated_at） | **规则与设计冲突**：指标样本/检查/步骤/备份等追加型表未按通用三列约定建表，需要“补列”或“调整契约规则”的决策 |
| 4 | 租户库元数据表清单与 `docs/data-model.md` 一致 | 模型 33 张，文档 32 张；多出 `tenant_local_users` | **文档漂移**：租户本地账号表未写入基线文档 |
| 5 | 迁移文档覆盖实际版本图 | 文档 22 个迁移，实际 29 个；遗漏 `center_0010`~`center_0015`（6 个）与 `tenant_0014`（1 个） | **文档漂移**：`docs/data-model.md`「迁移基线」未补齐 |
| 6 | 中心库迁移建表清单与文档一致 | 离线 SQL 29 张，文档 18 张（多出同 #1 的 11 张） | #1 的迁移 SQL 镜像 |
| 7 | 中心库迁移字段注释为中文 | `alert_events.severity/status`、`release_versions.status` 为非中文注释（值枚举） | #2 的迁移 SQL 镜像 |
| 8 | 租户库迁移建表清单与文档一致 | 离线 SQL 33 张，文档 32 张（多出 `tenant_local_users`） | #4 的迁移 SQL 镜像 |

补充事实：

* `docs/acceptance.md` 仍保留历史结论“38/38 通过、22 个迁移、中心 18 张/租户 32 张业务表”，与当前 HEAD 的实际
  （29 个迁移、中心 29 张/租户 33 张表）不一致；该文档需在批次 1 验收后统一刷新。
* 8 项失败中，**#1/#4/#5/#6/#8 属文档漂移（task-19 的 docs 写作用域可修）**；
  **#2/#7 属模型注释缺失（`backend/app/modules/ops/models_center.py`，属批次 1 后端写作用域）**；
  **#3 是契约规则与 ops 表设计冲突，不能只靠文档解决，需要 Lead/后端负责人决定补列或调整校验规则**。
* 本轮未修改任何代码、文档或数据；失败为 HEAD 既有状态复现，不是本轮引入。

### 2.5 基线时点与并发改动（重要）

基线命令执行于 **2026-11-13 14:21–14:22**，当时工作区干净（`git status` 为空），对应 HEAD `49794e5`。
执行后约 14:23 起，T1/T2/T3 并行开发开始写入工作区，当前存在未提交改动（**不属于本基线**）：

```text
M backend/app/modules/ops/models_center.py
M backend/app/modules/ops/schemas.py
M backend/app/modules/ops/service.py
M frontend/src/api/ops.ts
M frontend/src/types/ops.ts
M frontend/src/views/platform/PlatformInstancesView.vue
M frontend/src/views/platform/PlatformOpsCenterView.vue
?? agent/agentlib/
?? backend/alembic/center/versions/0016_center_release_upgrade.py
```

为避免工作区并发改动干扰，后端基线另用 `git archive 49794e5` 生成只读快照复核：
`pytest` 仍为 410 passed（20.08s）、契约 30/38、heads `center_0015` / `tenant_0014`，与主工作区结果一致。
前端构建在主工作区（14:22，早于 14:24 的前端改动）执行；在临时快照目录重跑构建会因 Windows 临时目录
esbuild 清理权限失败（`Access is denied`）——属环境限制，不是 HEAD 代码问题。

**因此批次 1 最终验收必须在 T1/T2/T3 合并后的干净提交上重跑第 4.1 节全部命令，不能用本基线数字代替交付验收。**

## 3. 基线缺口与风险

1. **Agent 无测试**：`agent/` 目前仅 `erp_agent.py`、`executor.py`、`install.sh`，无 pytest/模拟脚本；
   现有 Agent 仅“轮询任务 + 上报事件 + 遥测”，尚无心跳/租约续租/本地 SQLite 执行日志/协议版本能力（T2 范围）。
2. **ops 模块无专门测试**：`backend/tests/unit` 下没有 `test_ops*.py`；而 `ops/service.py` 已包含任务租约
   （10 分钟）、过期租约标记 `unknown` 且不自动重试、维护窗口等逻辑，批次 1 改动需要补测试覆盖。
3. **契约第 #3 项存在设计分歧**：指标样本/检查/步骤/备份表不满足“每表三列”规则；在 T1/T2 改动前需要先定口径。
4. **受版本管理的 `frontend/dist`**：任何 `npm run build` 都会改动 55 个受控文件；批量和发布构建需由 Lead 明确是否更新 dist。
5. **生产不可预演**：8.163.80.17 为生产环境，本轮未连接；试点检查只做只读清单，改动需 Lead 明确授权。
6. **T1/T2/T3 合并顺序耦合**：T1 新增 `center_0016` 后，docs 迁移基线、契约脚本、前端字段与验收数字都要一起更新，
   否则契约失败项会从 8 项漂移到新项。

## 4. 批次 1 测试计划（T1/T2/T3 完成后由 task-19 执行）

### 4.1 回归复跑（全部离线）

| # | 命令 | 通过标准 |
| --- | --- | --- |
| 1 | `cd backend; python -m pytest -q` | 0 失败；用例数 ≥ 410 + 批次新增；新增用例含 ops/发布校验/Agent 协议 |
| 2 | `cd backend; python scripts/verify_contracts.py` | **38/38 通过**（需先修 docs 账实：补 11 张 ops 表 + `tenant_local_users` + center_0010~0016/tenant_0014 迁移行，并按 T1 最终字段更新） |
| 3 | `cd backend; python -m alembic -c alembic/center.ini heads` / `tenant.ini heads` | 中心库 head 为 T1 交付的新修订（预期 `center_0016`）；租户库保持 `tenant_0014`；链连续、离线 SQL 零外键、中文注释完整 |
| 4 | `cd frontend; npm run build -- --outDir <临时目录>` | exit 0；新字段（commit/构建摘要/协议版本/DB 版本/回退兼容/升级状态）类型与模板编译通过；清理临时目录后 git 干净 |
| 5 | `docker compose -f deploy/docker-compose.yml config -q`（三种方式） | 部署编排未被批次 1 破坏（可选烟测） |

### 4.2 场景化模拟测试（task-19 重点，全部不依赖真实服务器）

| 场景 | 期望 | 测试落点 |
| --- | --- | --- |
| 重复事件幂等 | 同一 `webhook_events(tenant,channel,external_event_id)` 重放：不重复建单、不重复扣/恢复库存、outbox 不重复入队 | 复用 `tests/unit/test_sales.py`（订单/库存/唯一键）并新增 webhook 重放用例；必要时 fake session/monkeypatch |
| 任务租约续租 | Agent 每 30s 心跳续租、任务租约 120s；续租成功不产生 `unknown`；续租超时后任务标记 `unknown` 且占用不释放 | 后端 agent 心跳/租约接口（T1/T2 契约）service 层单测；Agent 侧模拟心跳/租约时序（T2 自测脚本） |
| `unknown` 不重试 | 过期租约 → `unknown`；`unknown` 继续占位，reconcile 前 claim/retry 不得重新投放执行 | `backend/tests/unit/test_ops_execution.py`（新增，fake center session）：claim 过滤 + retry 拒绝/409 |
| 升级检查失败保持维护 | 升级步骤中 DB 版本/readiness/业务只读检查失败：任务失败或 `unknown`，实例保持维护/告警，不自动回退或继续变更 | T1 升级校验 service 单测：构造失败 checks，断言 job 结果与实例状态/maintenance 保留 |
| 旧 Agent 不领取变更任务 | 协议版本/能力不足的旧 Agent：只能领取只读采集，不被派发 deploy/upgrade/restart 等变更任务；升级自身 helper 保留 UUID/config | 后端 claim 过滤单测 + Agent 端协议版本上报/拒绝逻辑模拟测试（T2） |

### 4.3 契约收尾步骤（task-19）

1. 以 T1 最终模型/迁移为准，更新 `docs/data-model.md`：补齐 11 张中心库 ops 表、`tenant_local_users`、
   center_0010~0016 与 tenant_0014 的迁移基线行和中文表/字段注释；同步 `docs/acceptance.md` 的数字与结论。
2. 与 T1/Lead 确认契约 #3（id/created_at/updated_at）口径：补列（含迁移回填）或调整 `verify_contracts.py` 规则；
   不允许“只为过检查”而绕过零外键/中文注释要求。
3. 重跑 4.1 全部命令，输出粘贴到 `docs/batch1-acceptance.md`；38/38 通过后才进入试点检查。

### 4.4 8.163.80.17 试点前只读检查清单（不做修改，除非 Lead 明确）

- [ ] 目标版本已登记：`release_versions` 存在该版本且 commit 为 40/64 位合法值、构建摘要/最低协议/DB 目标版本齐全
- [ ] 中心库 `alembic_version` = 预期头；全部租户库 `alembic_version` = 预期头
- [ ] 升级前备份存在（中心库 + 全部租户库 + 图片卷）且校验可用
- [ ] 维护窗口/暂停推送已确认；当前无进行中的变更任务（pending/leased/running/unknown）
- [ ] 健康检查：`/healthz`、`/readyz`、`/api/v1/gateway/health`、Agent 心跳在线
- [ ] Agent 版本矩阵：在线节点协议版本 ≥ 新版本最低协议；旧 Agent 清单与升级顺序
- [ ] 回滚方案与责任人明确；试点期间只观察，不触发第二次变更

## 5. 结论

* 当前 HEAD 的**测试与构建基线为绿**：pytest 410 passed；前端构建 exit 0；迁移链 heads 正常。
* **契约检查为 30/38**，稳定的 8 项失败已复现并分类：5 项文档漂移、2 项模型注释缺失、1 项规则/设计冲突。
* 本轮未改任何文件（仅新增本基线文档），未部署、未连接生产；等待 T1/T2/T3 完成后领取 task-19 执行 4.1~4.4。
