# Agent 2 可靠执行协议（客户端实现说明）

服务端实现见 `backend/app/modules/ops/agent_protocol.py` 与 `agent_router.py`；本文件记录
**Agent 客户端**如何调用、如何降级，以及与后端已确认的字段契约（task-17）。

## 角色与进程

| 进程 | systemd unit | 职责 | 权限 |
| --- | --- | --- | --- |
| 采集 | `erp-agent.service` | 指标采集、30s 心跳、断网缓存补报、待发送结果补报 | 只读部署目录，`ProtectSystem=strict` |
| 执行 | `erp-agent-executor.service` | 轮询任务、租约/续租、受限执行、SQLite 执行日志、结果落库上报 | 可写 `/opt/ventasync`、`/var/lib/erp-agent` |

两个进程共享 `/var/lib/erp-agent/erp-agent.db`（WAL 模式，各自独立连接）。
本地 SQLite 保存：`job_leases`（task_id / attempt_id / lease_token / 阶段 / 租约到期）、
`job_phases`（阶段日志）、`subprocess_runs`（子进程命令/退出码/输出尾部）、
`job_results`（**先落库后上报**的结果与事件序号）、`event_counters`、`kv`。

## 接口契约

所有请求带 `Authorization: Bearer <node_token>`、`X-Agent-Version`、`X-Agent-Protocol`。

### 1. 注册 `POST /api/v1/agent/enroll`

在旧字段基础上追加 `agent_protocol_version=2`、`executor_version`、`capabilities`。

### 2. 心跳 `POST /api/v1/agent/heartbeat`（30s）

```json
{
  "hostname": "host", "app_version": "abc123", "agent_version": "2.0.0",
  "executor_version": "2.0.0", "protocol_version": 2, "capabilities": ["heartbeat", "job_lease"],
  "boot_id": "uuid", "seq": 7, "role": "collector", "db_version": "",
  "uptime_seconds": 120, "sent_at": "2026-01-01T00:00:00Z",
  "state": {"executor": {"busy": false}, "telemetry": {"queued": 0}, "journal": {"pending_results": 0}}
}
```

读取并使用：`heartbeat_seconds`（心跳间隔）、`job_lease_seconds`、`job_poll_seconds`、
`telemetry_seconds`、`mutation_allowed`、`upgrade_required`、`stale`、`seq_ack`。
接口未上线（404/405/501）时客户端降级：只记录一次日志，不重试，指标上报不受影响。

### 3. 领取任务 `POST /api/v1/agent/jobs/poll`

请求带 `protocol_version / agent_version / executor_version / capabilities / mutation_job_types`；
服务端按已上报协议决定是否派发变更任务（协议 <2 只派 `logs`）。

响应 `data.job` 字段（客户端全部使用）：
`id / job_type / payload / attempts / attempt_id / lease_token / lease_expires_at /
lease_seconds / status / min_agent_protocol`。
客户端把 `attempt_id`、`lease_token` 写入本地租约；`lease_seconds` 以服务端为准（固定 120）。
同一租约内重复 poll 会返回同一 `attempt_id + lease_token`（客户端会复用，不重复执行）。
`job=null` 且带 `blocked_job/message` 表示中心暂不派发（例如存在 unknown 占用），客户端空转等待。

客户端兜底：若 `job.min_agent_protocol > 当前协议`，不执行并上报 `failed`（`agent_protocol_too_low`），
保证旧 Agent 不会执行变更任务。

### 4. 任务续租 `POST /api/v1/agent/jobs/{id}/heartbeat`（30s，租约 120s）

```json
{"attempt_id": "...", "lease_token": "...", "phase": "running", "lease_seconds": 120,
 "agent_version": "2.0.0", "executor_version": "2.0.0", "protocol_version": 2, "sent_at": "..."}
```

读取 `lease_expires_at / lease_seconds / lease_lost / cancel_requested / status`。
**409（旧 attempt / 终态 / 租约过期）**：立即停止续租、本地转 `unknown`、结果上报 unknown，
不再对终态任务续租。连续网络失败 3 次同样本地判定租约丢失。
接口未上线时降级为不续租（沿用旧 10 分钟租约），只记录一次日志。

### 5. 事件与结果 `POST /api/v1/agent/jobs/{id}/events`

阶段事件（`status=running`）与终态事件（`succeeded/failed/unknown`）都带：

```json
{"status": "succeeded", "result": {}, "error": "",
 "attempt_id": "...", "lease_token": "...", "event_seq": 12, "checks": {"services_ready": "succeeded"}}
```

`event_seq` 由本地 SQLite `event_counters` 按 `(task_id, attempt_id)` 单调分配（从 1 开始）；
**结果重放沿用原 `event_seq`**，服务端 `event_seq <= 已收水位` 即幂等返回 duplicate。
阶段事件允许跳号，但同一 attempt 内严格递增。

结果上报策略：

* 先写 `job_results`（`(task_id, attempt_id)` 唯一、先写先得），再尝试上报；
* 网络错误 / 5xx / 429：保留待发送，按 5s→10s→…→300s 退避补发；
* **409 等永久错误：落死信、停止重试、不改写本地状态**（服务端不会覆盖已终态任务）；
* 采集进程每 10s 补报一次；执行器执行结束与启动时也会补报，网络恢复后自动重发。

### 6. 核对 `POST /api/v1/agent/jobs/{id}/reconcile`

执行器重启 / unknown 后调用：

```json
{"attempt_id": "...", "lease_token": "...", "local_status": "unknown", "phase": "unknown",
 "error": "执行器重启，结果未知", "result": {}, "checks": {},
 "phases": [{"step_no": 1, "step_name": "准备版本目录", "status": "succeeded", "message": ""}]}
```

读取 `known / action / server_status / retry_allowed`：

| action | 客户端行为 |
| --- | --- |
| `marked_succeeded` / `marked_failed` | 以 `server_status` 覆盖本地结果、补报、解除占用 |
| `already_terminal` | 同上（服务端已有终态，忽略重复上报） |
| `kept_unknown` / `ignored_unknown_task` | 保持本地 unknown 占用，等人工 |
| `not_dispatched` | 保持本地 unknown 占用 |
| `known=false` | 中心无该任务、不改库；保持本地占用等人工 |

`reconcile` 命令行：`erp_agent.py reconcile --job-id <id> [--status succeeded|failed] [--note "..."]`；
本地/服务端都无终态时可用 `--status` 明确人工结论，再补报。

### 7. 指标 `POST /api/v1/agent/telemetry`

单条旧格式继续兼容（`collect` 原样 payload）。客户端优先批量：

```json
{"boot_id": "uuid", "protocol_version": 2, "capabilities": ["telemetry"],
 "samples": [{"sampled_at": "...", "cpu_percent": 1.2, "boot_id": "uuid", "sample_seq": 1}]}
```

* 每批最多 20 条，不足一批也允许；`sample_seq` 同一 boot 内从 1 单调递增；
* 按 `results[index].status` 处理：`accepted`/`duplicate` 删除、`invalid` 记日志后丢弃、网络失败保留缓存；
* 批量接口未上线或旧服务端拒绝多余字段（400/404/405/422/501）→ 自动回落单条旧格式。

## 状态机

```text
poll → leased → running ─┬─ succeeded / failed ──→ 上报终态 → 释放占用
                         ├─ 续租失败(3次/网络) ──→ unknown（继续占用，心跳保持）
                         ├─ 续租 409           ──→ unknown（停止续租）
                         └─ 执行器重启/被杀     ──→ unknown（启动时修正并上报）
unknown ── reconcile（人工/服务端终态）──→ 释放占用；不会自动重试旧任务
```

同一实例同时只允许一条活动租约（含 unknown），由 SQLite 事务 + 唯一约束保证。

## 升级（应用发布）

`upgrade` 任务必须携带**完整 40/64 位 commit**（短哈希直接拒绝，不执行任何 git/docker 命令）：

1. `git rev-parse --verify <commit>^{commit}` 与 `git cat-file -e` 复核；
2. `git worktree add --detach <repo>/versions/<commit12> <commit>` 建独立版本目录（共享对象库，
   **不再原地 `git reset --hard`**，不修改服务器 HEAD）；
3. 在版本目录执行 `docker compose build api nginx`，compose 项目名固定，env-file 指向服务器
   原 `deploy/.env`（升级不覆盖配置）；
4. `up -d --remove-orphans` → 等待 api/nginx running/healthy；
5. 成功后写 `versions/current.json` + `current` 软链，并把镜像标签写回 `.env`；
   失败保留原版本指针（回滚=切回上一版本目录）。

## Agent 自身升级

`erp_agent.py self-upgrade --source <源码目录|tar.gz> --version <版本> [--commit <完整commit>]`

* 独立目录：`/opt/erp-agent/versions/<版本>`，`current` 软链原子切换；
* `/etc/erp-agent/config.json` 在版本目录之外，升级前后校验 SHA-256 不变（节点 UUID/令牌保留）；
* `systemctl restart erp-agent.service erp-agent-executor.service` 后轮询 `is-active`；
* 验证失败自动切回上一版本并再次重启（回滚），报告失败原因。

## 兼容与降级矩阵

| 服务端能力 | 缺失时客户端行为 |
| --- | --- |
| `/agent/heartbeat` | 跳过心跳，仅指标上报（日志提示一次） |
| `/agent/jobs/{id}/heartbeat` | 不续租，沿用旧 10 分钟租约 |
| `/agent/jobs/{id}/reconcile` | 仅本地记录 unknown，继续占用；不重试旧任务 |
| events 新字段（attempt_id/event_seq） | 旧服务端忽略多余字段；客户端始终携带 |
| 批量 telemetry | 回落单条旧格式（400/404/405/422/501） |
| `mutation_allowed/upgrade_required` | 仅记录；最终是否派发由服务端决定，客户端保留 `min_agent_protocol` 兜底 |
