# 批次 1 集成验收记录（2026-11-13，HEAD 6828c2d）

- 验收提交：`6828c2da2e657efa69efd5aef47bcbd4e5a59260`（`chore(batch1): 集成自动发布元数据、追加型契约规则与前端构建产物`）
- 执行角色：deploy-qa（task-19）；执行时工作区干净（`git status` 为空）
- 范围：离线回归 + 契约检查 + 场景模拟 + 试点前只读清单；**未启动容器、未连接 MySQL/Redis、未访问生产 8.163.80.17、未改生产**
- 前置：T1（task-16 发布版本后端）、T2（task-17 Agent 2 协议）、T3（task-18 前端）与 T20/T21 已合入；基线见 `docs/batch1-baseline.md`

## 1. 回归与构建结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量测试 | `cd backend; python -m pytest -q` | **exit 0，448 passed**（1 条上游 Starlette 弃用提示；含 27 个 unit + 1 个 contract 共 28 个测试文件） |
| Agent 测试 | `cd agent; python -m pytest -q` | **exit 0，21 passed**（`test_journal_and_lease.py`、`test_protocol_client.py`） |
| 完整契约检查 | `cd backend; python scripts/verify_contracts.py` | **exit 0，38/38 通过，0 失败 0 警告** |
| 迁移链 | `python -m alembic -c alembic/center.ini heads` / `tenant.ini heads` | `center_0016 (head)`、`tenant_0014 (head)`，exit 0；共 30 个迁移文件 |
| 离线迁移 SQL | 两套 `upgrade head --sql` | 中心库 30 条 CREATE（业务 29 张）、租户库 34 条 CREATE（业务 33 张）；**FOREIGN KEY 0、REFERENCES 0**；表注释 29/33、字段注释 366/395，均含中文注释 |
| 前端生产构建 | `cd frontend; npm run build -- --outDir dist-b1-final` | **exit 0**（vue-tsc + vite）；vite 6.67s、命令总耗时 13.67s；产物 55 文件 / 1.95 MB；临时目录已删除，未改受版本管理的 `frontend/dist` |
| Compose 编排 | `docker compose -f deploy/docker-compose.yml config -q`；另加 `--env-file deploy/.env.example`、`--profile release` | 三种方式均 exit 0（不需要 daemon 运行） |

契约关键数字（`verify_contracts.py` 输出）：

- 文档基线：中心库 **29** 张表、租户库 **33** 张表；迁移文档 30 条与 `ScriptDirectory` 实际版本图一致；末端 `center_0016` / `tenant_0014`
- 文档路由 **94** 条全部存在；FastAPI 应用 138 条路由对象；权限码 21 个三处一致
- 零外键：模型元数据 + 两套离线 SQL 均为 0；追加型表例外（Lead 决策）已在检查器与 `docs/data-model.md` 同步
- 中文注释：模型 7 个 ops 字段（task-16 修复）与迁移 SQL 注释检查均通过

## 2. 场景模拟测试（无需真实服务器）

| 场景 | 覆盖用例（节选） | 执行结果 |
| --- | --- | --- |
| 重复事件幂等 | `test_sales.py`：唯一键/重复拉单/重复回调不重复扣减；`test_ops_release_upgrade.py::test_event_seq_is_idempotent`；`test_agent_protocol.py::test_event_seq_idempotent_and_terminal_conflict` | 后端筛选 **15 passed**；Agent 幂等/事件序号筛选 **5 passed** |
| 任务租约续租 | `test_agent_protocol.py::test_job_heartbeat_renews_lease_and_rejects_bad_token`、`::test_job_heartbeat_expired_lease_marks_unknown_and_conflicts`；Agent `test_protocol_client.py::test_lease_renewal_success_and_409_lease_lost` | 后端 **2 passed**；Agent lease 相关 **9 passed** |
| `unknown` 不重试 | `test_agent_protocol.py::test_unknown_job_blocks_new_dispatch_until_reconcile`、`::test_expired_lease_becomes_unknown_without_rerun`、`::test_reconcile_unknown_marks_succeeded_without_rerun`；Agent `test_unknown_occupies_and_blocks_new_tasks`、`test_unknown_blocks_polling_and_reconcile_payload` | 后端 `-k unknown` **19 passed**；Agent unknown/recover **5 passed** |
| 升级检查失败保持维护 | `test_ops_release_upgrade.py::test_checks_failed_marks_job_and_instance_failed`（db_version 通过、readiness 失败 → job failed、checks_status=failed、实例 upgrade_status=failed）、`::test_legacy_checks_partial_and_missing` | **2 passed**（见风险 1 的口径说明） |
| 旧 Agent 不领取变更任务 | `test_agent_protocol.py::test_old_agent_cannot_claim_mutation_jobs`、`::test_old_agent_can_still_claim_readonly_logs`、`::test_mutation_requires_target_release_min_protocol`；Agent `test_executor_rejects_short_commit_without_git_calls`、`test_executor_upgrade_uses_version_dir_not_reset` | 后端 **3 passed**；Agent 协议/升级 **14 passed** |

复跑命令示例：

```powershell
cd backend
python -m pytest -q
python -m pytest tests/unit tests/contract -q -k "idempot or unique_key or repeat_ingest or dedup or event_seq"
python -m pytest tests/unit tests/contract -q -k "heartbeat and (renew or expired)"
python -m pytest tests/unit tests/contract -q -k "unknown"
python -m pytest tests/unit tests/contract -q -k "checks"
python -m pytest tests/unit tests/contract -q -k "old_agent or min_protocol"
cd ..\agent
python -m pytest -q
```

说明：`tests/unit/test_sales.py` 单独运行时，其 SQLite fixture 依赖 `app.db.registry` 已被导入以填充 `TenantBase.metadata`；
完整 `pytest tests/unit tests/contract` 收集顺序下正常。按模块单跑时请先保证 registry 导入，或直接使用上面的全量命令。

## 3. 8.163.80.17 试点前只读检查清单（不做修改，除 Lead 明确授权）

> 以下全部为只读命令；不在生产执行迁移、发布、重启、备份恢复或数据写入。

| # | 检查项 | 只读命令示例 | 通过标准 |
| --- | --- | --- | --- |
| 1 | 已部署版本与目标版本 | 只读查看实例/发布版本记录 | 目标版本已登记；`commit_sha` 为 40/64 位合法值且与应用版本一致 |
| 2 | 发布元数据完整 | `SELECT version, commit_sha, build_config_summary, min_agent_protocol, center_db_version, tenant_db_version, rollback_compatible, build_status, status FROM release_versions WHERE version='<目标>';` | 最低协议、center/tenant DB 目标版本、回退兼容声明非空且与本次发布一致 |
| 3 | 数据库版本 | 中心库与每个租户库 `SELECT version_num FROM alembic_version;` | 中心库 `center_0016`、租户库 `tenant_0014`（或该发布登记的目标 revision） |
| 4 | 备份可用 | `ls -l /opt/backups/releases/<版本>/` + `sha256sum -c`（只读） | 中心库、全部租户库、图片卷备份完成且校验通过 |
| 5 | 变更任务静默 | `SELECT id, job_type, status, target_version FROM operation_jobs WHERE status IN ('pending','leased','running','unknown');` | 无进行中/unknown 变更任务；维护窗口已确认 |
| 6 | Agent 版本矩阵 | `SELECT node_uuid, hostname, agent_version, protocol_version, capabilities_json, status, last_seen_at FROM server_nodes;` | 在线节点 `protocol_version` ≥ 目标版本 `min_agent_protocol`；旧 Agent 不领取变更任务 |
| 7 | 健康检查 | `curl -fsS https://<domain>/healthz`、`/readyz`、`/api/v1/gateway/health`；`docker compose ps`（只读） | 全部 200/healthy；Agent 心跳在线 |
| 8 | 回滚预案 | 只读核对上一版本镜像/包与备份时间点 | 回滚镜像、DB 备份、责任人、窗口明确；回滚前不删旧版本包 |
| 9 | 业务只读检查 | 不触发写操作，仅查看订单/库存/任务页面与接口 | 关键列表可读、无异常告警；推送总开关按计划保持关闭 |

## 4. 阻塞与风险

1. **“检查失败保持维护”的口径**：当前实现为升级任务 `failed`、实例 `upgrade_status=failed`、写入失败项并停止后续变更（不自动重试/不自动回退），未使用字面 `maintenance` 状态；若试点要求实例保持显式维护态，需要 Lead/后端确认是否调整。
2. **真实环境未验证**：MySQL 行锁/并发、Redis/Celery Worker 中断恢复、真实 TikTok/Shopee 授权与推送、真实服务器 Agent 心跳/升级、备份恢复演练均未在本环境执行；本文件只证明离线与模拟层面通过，不替代试点验收。
3. **Agent 测试为本地模拟**：Agent 协议契约以 `agent/PROTOCOL.md` 与后端 `test_agent_protocol.py` 对齐，真实 Linux/host 环境待试点验证。
4. **受版本管理的 `frontend/dist`**：本次验收用临时 `--outDir`，未改 dist；生产 dist 是否更新由 Lead 决定。
5. **生产未触碰**：未连接 8.163.80.17，未执行任何生产写操作；试点需 Lead 明确授权与维护窗口。

## 5. 结论

- 批次 1 离线集成验收**全绿**：后端 448 passed、Agent 21 passed、契约 38/38、迁移链 `center_0016`/`tenant_0014`、前端构建 exit 0、Compose 校验 exit 0。
- 5 个关键场景均有自动化模拟用例并通过（幂等/租约续租/unknown 不重试/检查失败停止变更/旧 Agent 不领取变更任务）。
- 具备试点前置条件；试点前按第 3 节只读清单逐项确认，真实环境结论单独记录，不并入本次离线通过。
