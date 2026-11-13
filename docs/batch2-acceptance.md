# 批次 2 验收记录（2026-11-13，HEAD 02846cf）

- 验收提交：`02846cf`（`feat(ops): SSH 主机指纹、审计与会话复核加固`，本地 HEAD，未 push）
- 执行角色：deploy-qa（task-27）；本批次为离线验收，**未连接生产、未启动容器、未改后端/前端代码**
- 批次 2 提交范围：`09519b2`（升级前强制备份）、`0ba7b5d`（实例部署/迁移状态与 SSH 指纹展示）、
  `d97544e`（后台任务固定实例/租户绑定与迁移租户调度隔离）、`755a0bd`（租户模式边界与 center_0017）、
  `02846cf`（SSH 主机指纹、审计、会话复核加固）；期间另含 `65b48f4`（升级后 .env 保留共享组读权限）
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

## 2. 回归与构建结果（HEAD 02846cf）

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

## 3. 契约文档同步

- `docs/data-model.md`：新增 `center_0017` 迁移基线；`tenant_instances` 补充 `deployment_mode`/`migration_status` 枚举与索引说明；
  `server_nodes` 补充 3 个 SSH 指纹列；更新为 31 个迁移文件（center 17 + tenant 14）、末端 `center_0017`；新增批次 2 部署形态说明。
- `docs/acceptance.md`：顶部增加批次 2 当前状态与测试数字；批次 1 试点记录保留为历史。
- 契约脚本因上述文档补齐后恢复 **38/38**（补齐前唯一失败为“迁移文档遗漏 `center_0017`”）。

## 4. 批次 2 剩余风险与待完成项

1. **tenant 模式部署参数**：tenant 模式必须显式配置 `TENANT_ID` 与 `INSTANCE_ID`（含别名环境变量）；
   未配置/不一致会拒绝平台和跨租户访问。部署清单与 compose 环境变量需要按实例补充，尚未做真实 tenant 实例验收。
2. **SSH TOFU 首次指纹**：首次连接采用 TOFU 记录 SHA256 指纹，需运维人工核对首次指纹（或通过重置接口重建）后才算可信；
   指纹不一致会拒绝连接并告警，但正式发布前仍需按服务器逐台核对。
3. **SSH 审计标签**：新增 `ssh.*` 平台审计动作（connect/command/fingerprint reset/mismatch 等）需要在正式发布前核对
   `PLATFORM_AUDIT_ACTIONS` 标签/前端展示是否齐全；当前以常量登记与单测为准，未做真实审计页面验收。
4. **未完成批次范围**：第二租户/第二台实例试点、备份前置与恢复演练、显式维护状态、监控/告警、独立网关部署
   尚未完成；按批次 1/2 记录继续排期，不得以本批次离线通过与试点完成等同。
5. **真实环境边界**：本批次未在真实服务器验证 SSH 指纹/会话复核、升级前备份阻断、tenant 模式隔离；
   这些需要下一轮试点环境（含 SSH 可达主机、备份卷、独立 tenant 实例）执行。
6. **测试覆盖边界**：Agent 升级前“备份失败阻断迁移”分支尚未有独立 Agent 单测覆盖（实现已合入并随升级链路执行），
   建议下一批补模拟测试。

## 5. 结论

- 批次 2 离线交付与契约文档同步完成：后端 531 passed、Agent 22 passed、契约 38/38、迁移链 `center_0017`/`tenant_0014`、
  前端构建与 Compose 校验通过。
- 交付重点是“实例隔离/租户模式边界 + 后台任务实例绑定 + 升级前强制备份 + SSH 主机指纹/审计/会话复核”；均有无需真实环境的自动化用例覆盖。
- tenant 模式真实实例、SSH TOFU 首指纹人工核对、审计标签展示及第二租户/监控/网关等仍为剩余项（第 4 节），
  本文件不宣称这些已完成。
