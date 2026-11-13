# 批次 2 验收记录（2026-11-13，离线 02846cf → 生产试点 1da80c2）

- 离线验收提交：`02846cf`；生产部署/试点版本：`1da80c2`（`feat(batch2): 租户模式边界、任务实例绑定、SSH 指纹审计与前端状态`，整合 02846cf 全部提交与审计标签/前端产物）
- 执行角色：deploy-qa（task-27 离线验收、task-28 试点证据补记）；离线部分不连接生产；试点由试点流程执行，QA 只读取证
- 批次 2 提交范围：`09519b2`（升级前强制备份）、`0ba7b5d`（实例部署/迁移状态与 SSH 指纹展示）、
  `d97544e`（后台任务固定实例/租户绑定与迁移租户调度隔离）、`755a0bd`（租户模式边界与 center_0017）、
  `02846cf`（SSH 主机指纹、审计、会话复核加固）、`1da80c2`（批次 2 整合与前端构建产物）；
  期间另含 `65b48f4`（升级后 .env 保留共享组读权限）
- 批次 1 记录见 `docs/batch1-acceptance.md`；基线见 `docs/batch1-baseline.md`

## 1. 批次 2 交付内容

| # | 交付 | 说明 |
| --- | --- | --- |
| 1 | 部署形态与租户模式边界 | 运行模式 `deployment_mode=center/tenant/gateway`；tenant 模式只服务固定租户（`TENANT_ID`/`INSTANCE_ID`），拒绝 `/api/v1/platform/*`、`/api/v1/agent/*`（含平台 SSH WebSocket）并返回统一 404；HTTP 路径/查询/JSON body 中的 tenant_id 必须与固定租户一致，否则 403；Worker/Celery 任务 payload 同样校验租户 |
| 2 | `center_0017` 迁移 | `tenant_instances` 增加 `deployment_mode`（shared/independent）与 `migration_status`（registered/migrating/migrated/rollback）及索引；`server_nodes` 增加 `ssh_host_fingerprint`、`ssh_host_fingerprint_verified_at`、`ssh_fingerprint_algorithm`（当前固定 SHA256）；全部无外键、中文字段注释、旧记录默认值兼容 |
| 3 | 实例隔离与调度边界 | 迁移中/已迁出（migrating/migrated）实例不参与旧中心业务调度，也不允许用普通“启用租户”恢复写入；创建运维任务、开通租户等入口对迁移中实例拒绝；实例列表支持按部署模式/迁移状态筛选并展示 |
| 4 | 后台任务固定实例/租户绑定 | `tasks/instance.py` 统一解析并校验“当前实例 + 固定租户”绑定；中心 Worker 仅调度 active/shared 租户；tenant Worker 只服务自身租户；payload 中的 tenant_id/instance_id 不可覆盖上下文；调度器/Outbox/Worker 使用同一绑定规则 |
| 5 | Agent 升级前强制备份 | 升级执行前必须运行部署目录内 `backup.sh`；脚本缺失或备份失败时**不执行迁移/启动**；升级后 `.env` 保留共享组读权限（65b48f4） |
| 6 | SSH 主机指纹与安全加固 | SSH 目标只使用节点显式 ssh_host，不回退 Agent 上报 IP；首次连接记录 SHA256 主机指纹（TOFU），后续连接比对，不一致拒绝并告警；修改 SSH 目标会清空旧指纹；提供带显式确认的指纹重置接口；命令执行/断开/终端开关写入脱敏审计；会话空闲与定期复核，权限撤销/指纹不一致时关闭 WebSocket（4409） |
| 7 | 前端展示 | 实例列表/详情展示部署模式、迁移状态、SSH 指纹摘要等批次 2 字段（前端构建通过） |

## 2. 回归与构建结果（离线，HEAD 1da80c2 复核）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量测试 | `cd backend; python -m pytest -q` | **exit 0，531 passed**（1 条上游 Starlette 弃用提示；30 个 unit 测试文件 + 1 个 contract 测试文件） |
| 批次 2 特性测试 | `python -m pytest tests/unit/test_tenant_mode_boundary.py tests/unit/test_task_instance_binding.py tests/unit/test_ops_ssh_security.py -q` | **82 passed**（tenant 模式边界 24 用例、任务实例绑定 37 用例、SSH 安全 14 用例，含参数化展开） |
| Agent 测试 | `cd agent; python -m pytest -q` | **exit 0，22 passed** |
| 完整契约检查 | `cd backend; python scripts/verify_contracts.py` | **exit 0，38/38 通过，0 失败 0 警告** |
| 迁移链 | `python -m alembic -c alembic/center.ini heads` / `tenant.ini heads` | `center_0017 (head)`、`tenant_0014 (head)`；共 31 个迁移文件 |
| 离线迁移 SQL | 两套 `upgrade head --sql` | 中心库 30 条 CREATE（业务 29 张）、租户库 34 条（业务 33 张）；FOREIGN KEY 0、REFERENCES 0；表注释 29/33、字段注释 371/395 |
| 前端生产构建 | `cd frontend; npm run build -- --outDir dist-b2-check` | **exit 0**（vue-tsc + vite）；vite 6.97s；55 文件 / 1.96 MB；临时目录已删除，未改受版本管理的 `frontend/dist` |
| Compose 编排 | 默认 / `--env-file deploy/.env.example` / `--profile release` 三种 `config -q` | 均 exit 0 |

契约关键数字：中心库 29 张表、租户库 33 张表；迁移文档 31 条与 `ScriptDirectory` 一致；末端 `center_0017` / `tenant_0014`；
文档路由 94 条全部存在（应用 139 条路由对象）；权限码 21 个一致；零外键与中文注释全部通过。

## 3. 生产试点实测（2026-11-13，8.163.80.17）

### 3.1 中心部署与发布登记

| 项目 | 实测结果 |
| --- | --- |
| 中心代码版本 | `1da80c2` 已部署 |
| 中心库迁移 | `center_0017` |
| 发布版本自动登记 | `release_versions`：version/commit_sha=`1da80c2`，`build_status=verified`，`min_agent_protocol=2`，`center_db_version=center_0017`，`tenant_db_version=tenant_0014` |

### 3.2 租户升级（job 42）

| 项目 | 实测结果 |
| --- | --- |
| 租户实例 | `8.163.80.17` |
| 运维任务 | job **42**（upgrade）执行成功，`checks_status=passed` |
| 实例版本 | `actual_version=1da80c2`，`db_version=tenant_0014` |
| 升级前备份说明 | 备份步骤已加入 Agent 代码（`09519b2`：`backup.sh` 缺失或失败则不迁移）；本次执行的 executor 为升级前版本、尚未包含该步骤，因此“备份失败阻断迁移”的真实路径未在 job 42 验证，待 Agent 升级后生效并补验 |

### 3.3 tenant 模式边界实测

| 检查 | 实测结果 |
| --- | --- |
| 实例配置 | `TENANT_ID=1`、`INSTANCE_ID=1`（`deployment_mode=tenant`） |
| tenant 实例登录 | 200 |
| tenant runtime 接口 | 200 |
| tenant healthz | 200 |
| tenant 访问 `/platform/releases` | **404**（统一拒绝，不暴露平台接口） |
| 中心平台登录 | 200 |
| 中心 `/platform/releases` | 200（中心平台功能不受影响） |

结论：tenant 模式边界按设计生效——业务与健康检查可用，平台/Agent 接口统一 404；中心实例平台接口正常。

### 3.4 SSH 主机指纹实测

| 步骤 | 实测结果 |
| --- | --- |
| 首次 SSH exec | 成功；记录主机指纹 `SHA256:T2cB1vQn5bvCsJ9BPqmui5IIb+7vR/n1tuseQFY8LYE`（`ssh-ed25519`） |
| 再次 GET 节点 SSH 指纹 | `fingerprint_status=verified`（与首次记录一致） |
| 说明 | 首次连接为 TOFU 记录；当前实现指纹匹配为 verified，不一致会拒绝并告警；本次未做真实 mismatch 演练（由单测覆盖） |

### 3.5 试点结论

- 中心 `1da80c2` + `center_0017` 部署完成，发布版本自动登记字段正确（verified / 协议 2 / center_0017 / tenant_0014）。
- 租户 `8.163.80.17` 升级 job 42 成功：checks passed、实际版本 `1da80c2`、租户库 `tenant_0014`。
- tenant 模式 404 边界与 SSH 首指纹记录/verified 已在真实环境验证。
- 升级前备份步骤因旧 executor 尚未生效；备份恢复演练、维护状态、监控、网关、第二租户仍为剩余项（见第 5 节）。

## 4. 契约文档同步

- `docs/data-model.md`：新增 `center_0017` 迁移基线；`tenant_instances` 补充 `deployment_mode`/`migration_status` 枚举与索引说明；
  `server_nodes` 补充 3 个 SSH 指纹列；更新为 31 个迁移文件（center 17 + tenant 14）、末端 `center_0017`；新增批次 2 部署形态说明。
- `docs/acceptance.md`：顶部增加批次 2 当前状态与测试数字；批次 1 试点记录保留为历史。
- 契约脚本因上述文档补齐后恢复 **38/38**（补齐前唯一失败为“迁移文档遗漏 `center_0017`”）。

## 5. 批次 2 剩余风险与待完成项

1. **tenant 模式部署参数**：本次试点已用 `TENANT_ID=1`/`INSTANCE_ID=1` 实测通过；第二租户/第二台实例仍需按实例显式配置
   （含别名环境变量），未配置/不一致会拒绝平台和跨租户访问，部署清单需逐实例补充。
2. **SSH TOFU 首次指纹**：本次已真实记录 `SHA256:T2cB1vQn5bvCsJ9BPqmui5IIb+7vR/n1tuseQFY8LYE`（ssh-ed25519）并在再次查询时为
   `verified`；仍需把“首指纹人工核对”纳入运维流程，真实 mismatch/重置演练待补（单测已覆盖）。
3. **SSH 审计标签**：新增 `ssh.*` 平台审计动作（connect/command/fingerprint reset/mismatch 等）需要核对
   `PLATFORM_AUDIT_ACTIONS` 标签/前端展示是否齐全；当前以常量登记与单测为准，未做真实审计页面验收。
4. **未完成批次范围**：第二租户/第二台实例试点、备份恢复演练、显式维护状态、监控/告警、独立网关部署尚未完成；
   不得以本批次离线通过 + 单台试点等同批次 2 全量完成。
5. **升级前备份真实路径**：job 42 使用旧 executor（不含升级前备份步骤），因此“备份失败阻断迁移”尚未在真实环境验证；
   待 Agent 升级到包含 `09519b2` 的版本后，补一次带备份步骤的升级并确认备份失败场景会阻断迁移。
6. **测试覆盖边界**：Agent 升级前“备份失败阻断迁移”分支仍缺独立 Agent 单测（实现已合入），建议下一批补模拟测试。

## 6. 结论

- 批次 2 离线交付与契约文档同步完成：后端 531 passed、Agent 22 passed、契约 38/38、迁移链 `center_0017`/`tenant_0014`、
  前端构建与 Compose 校验通过。
- 生产试点（8.163.80.17）完成：中心 `1da80c2`+`center_0017`、发布版本自动登记、租户升级 job 42 成功、
  tenant 模式平台接口 404 边界、SSH 首指纹记录并 verified。
- 交付重点是“实例隔离/租户模式边界 + 后台任务实例绑定 + 升级前强制备份 + SSH 主机指纹/审计/会话复核”；
  tenant 模式真实边界与 SSH 首指纹已完成单台试点，升级前备份真实路径、备份恢复、维护状态、监控/网关、第二租户仍为剩余项（第 5 节）。
