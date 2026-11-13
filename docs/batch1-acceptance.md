# 批次 1 验收记录（2026-11-13，离线 6828c2d → 试点 2e34e6b）

- 离线集成验收：`6828c2d`（task-19，见 `docs/batch1-baseline.md`；回归/契约/构建全绿）
- 第一台试点实测：`2e34e6b`（`fix(batch1): Agent2 迁移校验、共享库权限、就绪输出与发布元数据`），部署于 `8.163.80.17`
- 执行角色：deploy-qa（task-22）。本文档汇总离线复核 + **试点实测证据**；试点部署与变更由试点流程执行，QA 只读取证，未直接改生产
- 试点环境：中心实例 + 单租户实例（8.163.80.17）；离线环境未启动容器、未连接本机 MySQL/Redis

## 1. 离线回归与构建（基于 2e34e6b 复核）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量测试 | `cd backend; python -m pytest -q` | **exit 0，449 passed**（1 条上游 Starlette 弃用提示） |
| Agent 测试 | `cd agent; python -m pytest -q` | **exit 0，22 passed** |
| 完整契约检查 | `cd backend; python scripts/verify_contracts.py` | **exit 0，38/38 通过，0 失败 0 警告** |
| 迁移链 | `python -m alembic -c alembic/center.ini heads` / `tenant.ini heads` | `center_0016 (head)`、`tenant_0014 (head)`，exit 0；共 30 个迁移文件 |
| 离线迁移 SQL | 两套 `upgrade head --sql` | 中心库 30 条 CREATE（业务 29 张）、租户库 34 条 CREATE（业务 33 张）；FOREIGN KEY 0、REFERENCES 0；表注释 29/33、字段注释 366/395 |
| 前端生产构建 | `cd frontend; npm run build -- --outDir dist-b1-pilot-check` | **exit 0**（vue-tsc + vite）；vite 5.79s；55 文件 / 1.95 MB；临时目录已删除，未改受版本管理的 `frontend/dist` |
| Compose 编排 | 默认 / `--env-file deploy/.env.example` / `--profile release` 三种 `config -q` | 均 exit 0 |

契约关键数字：中心库 29 张表、租户库 33 张表；文档路由 94 条全部存在（应用路由对象 138 条）；权限码 21 个一致；
零外键与中文注释（模型 + 迁移 SQL）全部通过；追加型表例外按 Lead 决策由检查器与 `docs/data-model.md` 同步。

## 2. 第一台试点实测（8.163.80.17）

### 2.1 中心部署与发布登记

| 项目 | 实测结果 |
| --- | --- |
| 中心代码版本 | `2e34e6b` 已部署 |
| 中心库迁移 | 升至 `center_0016` |
| 发布版本自动登记 | `release_versions`：version/commit_sha=`2e34e6b`（完整 40 位 commit），`build_status=verified`，`min_agent_protocol=2`，`center_db_version=center_0016`，`tenant_db_version=tenant_0014` |
| 升级目标约束 | 后端只允许 `verified` 且 commit 合法的发布版本作为 deploy/upgrade 目标；“自动登记 + 校验”链路在试点可用 |

### 2.2 Agent2 安装与运行

| 项目 | 实测结果 |
| --- | --- |
| 安装主机 | `8.163.80.17` |
| systemd 单元 | `erp-agent.service`、`erp-agent-executor.service` 均为 **active**（采集/心跳与任务执行分离） |
| 协议版本 | `protocol_version=2`（满足发布版本 `min_agent_protocol=2`） |
| 组件版本 | Agent 2.0.0、Executor 2.0.0 |
| 本地执行日志 | SQLite 位于 `/var/lib/erp-agent`，任务/租约/结果先本地持久化再上报 |
| 试点修复项 | 共享库权限、docker `HOME`/`DOCKER_CONFIG`、迁移校验、就绪输出、发布元数据（提交 `2e34e6b`） |
| 节点版本心跳 | 节点 `app_version` 已随心跳更新为 `2e34e6b` |

### 2.3 试点升级（job 41）

| 项目 | 实测结果 |
| --- | --- |
| 运维任务 | job **41**，`job_type=upgrade`，target=`2e34e6b` |
| 校验结论 | `checks_status=passed`（三项标准校验齐全且通过） |
| 执行步骤 | 含 `build` / `migrate` / `up` / `readiness` / `business_readonly` |
| 实例状态 | `actual_version=2e34e6b`、`db_version=tenant_0014`、`upgrade_status=succeeded` |
| 节点状态 | `app_version=2e34e6b`（心跳回传），协议版本 2 |
| 租户库迁移 | 升级中执行，终态 `tenant_0014` |

### 2.4 失败演练与修复（job 35–41）

| job | 场景 | 结果与处理 |
| --- | --- | --- |
| 35 | SQLite 只读 | 演练暴露 Agent 本地执行日志目录不可写；修复共享库/目录权限后继续（对应提交 `2e34e6b` 的“共享库权限”） |
| 36 | invalid_commit | 非法/短 commit 被正确拒绝，未进入构建执行（commit 校验生效） |
| 37 | build 失败 | 构建阶段失败；修复 Agent 构建/环境问题（docker `HOME`/`DOCKER_CONFIG` 等）后重试 |
| 38 | readiness 失败 | 服务就绪校验失败，任务未判定成功；修复就绪输出/环境后重试 |
| 39 | partial | 校验结论为 partial（标准校验项不完整），未按通过处理 |
| 40 | 重试 | 修复后进入正常执行，结果 passed |
| 41 | 最终升级 | target=`2e34e6b`，`checks_status=passed`，任务与实例终态成功（见 2.3） |

### 2.5 试点结论

- 第一台试点（单租户实例 8.163.80.17）升级闭环打通：发布登记 → Agent2 心跳/执行 → 构建/迁移/启动/就绪/业务只读校验 → 实例与节点版本回写。
- 失败演练覆盖 SQLite 权限、非法 commit、build/readiness 失败、partial 校验；修复后重试成功，说明任务可恢复、旧任务/非法目标不会误执行。
- 第二台/第二租户以及备份恢复、维护状态等见第 5 节剩余项。

## 3. 场景模拟测试（离线，补充回归）

| 场景 | 覆盖用例（节选） | 结果 |
| --- | --- | --- |
| 重复事件幂等 | `test_sales.py` 唯一键/重复拉单/重复回调；`test_ops_release_upgrade.py::test_event_seq_is_idempotent`；`test_agent_protocol.py::test_event_seq_idempotent_and_terminal_conflict` | 后端筛选 15 passed；Agent 5 passed |
| 任务租约续租 | `test_agent_protocol.py::test_job_heartbeat_renews_lease_and_rejects_bad_token`、`::test_job_heartbeat_expired_lease_marks_unknown_and_conflicts`；Agent `test_protocol_client.py::test_lease_renewal_success_and_409_lease_lost` | 后端 2 passed；Agent lease 9 passed |
| `unknown` 不重试 | `test_agent_protocol.py::test_unknown_job_blocks_new_dispatch_until_reconcile`、`::test_expired_lease_becomes_unknown_without_rerun`；Agent `test_unknown_occupies_and_blocks_new_tasks` | 后端 `-k unknown` 19 passed；Agent 5 passed |
| 升级检查失败停止变更 | `test_ops_release_upgrade.py::test_checks_failed_marks_job_and_instance_failed`、`::test_legacy_checks_partial_and_missing` | 2 passed（job failed / checks_status failed / 实例 upgrade_status failed，不自动继续） |
| 旧 Agent 不领取变更任务 | `test_agent_protocol.py::test_old_agent_cannot_claim_mutation_jobs`、`::test_old_agent_can_still_claim_readonly_logs`、`::test_mutation_requires_target_release_min_protocol` | 后端 3 passed；Agent 协议/升级 14 passed |

## 4. 试点检查清单执行结果

| # | 检查项 | 状态 | 证据/说明 |
| --- | --- | --- | --- |
| 1 | 已部署版本与目标版本 | 完成 | 发布版本 `2e34e6b` 已登记，commit 合法 |
| 2 | 发布元数据完整 | 完成 | verified / min_agent_protocol=2 / center_0016 / tenant_0014 |
| 3 | 数据库版本 | 完成 | 中心库 `center_0016`、租户库升级后 `tenant_0014` |
| 4 | 备份可用 | **未完成（第二批）** | 备份前置与恢复演练列入第二批，不在本次试点执行 |
| 5 | 变更任务静默 | 完成 | job 41 终态 succeeded；演练任务 35–40 为已验证的失败/修复记录 |
| 6 | Agent 版本矩阵 | 完成 | 在线节点 protocol=2 ≥ min=2；Agent/Executor 2.0.0 |
| 7 | 健康检查 | 部分完成 | 升级 checks 中 readiness/business_readonly 通过；独立网关与持续监控列入第三批 |
| 8 | 回滚预案 | **未完成（第二批）** | 回滚包/备份时间点与演练列入第二批 |
| 9 | 业务只读检查 | 完成 | 升级 checks 的 business_readonly 通过 |

## 5. 剩余项（第二批 / 第三批）

**第二批（建议优先）：**

1. 备份前置与恢复演练：中心库 + 全部租户库 + 图片卷的备份/校验/恢复，恢复后先对账再开启推送。
2. 维护状态：升级/变更期间实例的显式维护态（当前检查失败落为 `upgrade_status=failed`，非字面 maintenance），需后端/前端确认口径。
3. 第二租户试点：第二台租户实例/第二个租户的 Agent2 + 升级闭环，验证多租户并行与数据隔离。

**第三批：**

4. SSH 指纹校验与操作审计（服务器详情 SSH 命令的指纹确认/审计落库）。
5. 监控与告警：指标采集、聚合趋势、告警确认闭环、外部巡检。
6. 独立网关：网关独立部署/回调与健康检查（当前 gateway 随中心运行）。

## 6. 风险与结论

1. **试点通过范围**：第一台单租户实例升级闭环通过；第二租户、备份恢复、维护状态、监控/网关/SSH 审计仍未验收，不得据此宣称批次 1 全量完成。
2. **“检查失败保持维护”口径**：当前实现为任务 failed、实例 `upgrade_status=failed` 并停止后续变更；若要求显式 `maintenance` 状态，需在第二批实现并回归。
3. **真实平台闭环**：本次试点验证的是 ERP 自身发布/升级链路，真实 TikTok/Shopee 订单库存闭环仍需独立店铺与授权环境验收。
4. **测试隔离小问题**：`tests/unit/test_sales.py` 单独运行时依赖 `app.db.registry` 已被导入；全量 `pytest tests/unit tests/contract` 正常。
5. **dist 更新**：离线验收使用临时 `--outDir`，未改受版本管理的 `frontend/dist`；生产 dist 由 Lead 决定。
6. 本文件证据来自试点只读采集；QA 未直接修改生产数据或服务。
