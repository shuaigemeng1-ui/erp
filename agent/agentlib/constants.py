"""Agent 2 常量与协议版本（仅标准库）。

版本号同时用于：
* 上报给管理中心的 ``agent_version`` / ``agent_protocol_version``；
* Agent 自身升级时定位 ``/opt/erp-agent/versions/<version>`` 目录；
* 变更任务能力协商（旧 Agent 协议版本低，不领取变更任务）。
"""

from __future__ import annotations

AGENT_VERSION = "2.0.0"
EXECUTOR_VERSION = AGENT_VERSION
# Agent 2 可靠执行协议版本；后端未实现新接口时客户端降级但版本号照实上报
PROTOCOL_VERSION = 2
# 领取变更任务所需的最低协议版本（旧 Agent 只会上报 1 或不上报，不会拿到变更任务）
MIN_PROTOCOL_FOR_MUTATION = 2
# 后端尚未实现新心跳接口时，降级使用后端现有 10 分钟租约语义
LEGACY_LEASE_SECONDS = 600

DEFAULT_CONFIG = "/etc/erp-agent/config.json"
DEFAULT_AGENT_ROOT = "/opt/erp-agent"
DEFAULT_STATE_DIR = "/var/lib/erp-agent"
DEFAULT_DEPLOY_DIR = "/opt/ventasync/deploy"
DEFAULT_REPO_DIR = "/opt/ventasync"

TELEMETRY_SECONDS = 30
HEARTBEAT_SECONDS = 30
JOB_POLL_SECONDS = 5
RESULT_REPORT_SECONDS = 10
# 任务租约 120 秒，续租间隔 30 秒（plan：任务租约 120s、每 30s 续租）
JOB_LEASE_SECONDS = 120
JOB_LEASE_RENEW_SECONDS = 30
# 单次结果上报最多尝试次数；超过后保持不变，等待网络恢复后由上报线程继续补发
RESULT_REPORT_MAX_ATTEMPTS = 50
# 租约续租连续失败（网络原因）达到该次数后本地判定租约丢失 → unknown
LEASE_RENEW_FAILURE_LIMIT = 3

TELEMETRY_CACHE_LIMIT = 2880  # 24h / 30s

# 任务类型：变更任务（同一实例同时只允许一个）与只读任务
MUTATION_JOB_TYPES = frozenset({"restart", "backup", "deploy", "upgrade"})
READONLY_JOB_TYPES = frozenset({"logs"})
ALLOWED_JOB_TYPES = MUTATION_JOB_TYPES | READONLY_JOB_TYPES

# 任务/执行阶段状态
STATUS_LEASED = "leased"
STATUS_RUNNING = "running"
STATUS_REPORTING = "reporting"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"          # 结果待核对：继续占用实例，reconcile 前禁止重试
STATUS_RECONCILED = "reconciled"    # 人工核对完成，解除占用
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_UNKNOWN})
ACTIVE_LEASE_STATUSES = frozenset({STATUS_LEASED, STATUS_RUNNING, STATUS_REPORTING, STATUS_UNKNOWN})

# Agent 能力：上报给中心，供后端决定是否下发变更任务
CAPABILITIES = (
    "telemetry",
    "heartbeat",
    "job_lease",
    "journal",
    "result_replay",
    "reconcile",
    "version_dir",
    "self_upgrade",
)
# 声明具备变更任务能力的最低协议版本
MUTATION_CAPABILITY = "mutation_jobs"

UNITS_DEFAULT = ("erp-agent.service", "erp-agent-executor.service")
