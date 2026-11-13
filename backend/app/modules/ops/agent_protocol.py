"""Agent 2 可靠执行协议服务端实现（节点 Bearer 认证，不走用户会话）。

端点契约（与 ``agent/agentlib`` 客户端、``docs/batch1-baseline.md`` 对齐）：

* ``POST /agent/heartbeat``：节点协议/版本/能力/启动批次心跳；
* ``POST /agent/jobs/poll``：领取任务，返回 ``attempt_id`` / ``lease_token`` / 120s 租约；
  同一节点在租约内重复 poll 返回同一执行批次；旧协议 Agent 不领取变更任务；
* ``POST /agent/jobs/{id}/heartbeat``：任务执行心跳，续租 120s，错误/过期租约 409；
* ``POST /agent/jobs/{id}/events``：事件按 ``event_seq`` 幂等，终态只接受当前 attempt，
  重复终态幂等，旧 attempt / 冲突终态拒绝覆盖；
* ``POST /agent/jobs/{id}/reconcile``：节点上报本地批次/阶段/结果，服务端核对后标记
  succeeded/failed/unknown，**绝不把任务重新置为 pending 或重跑变更**；
* ``POST /agent/telemetry``：单条旧格式兼容 + 有限批量（<=20 条），按
  ``boot_id+sample_seq`` 逐条确认/去重。

字段依赖：``center_0016`` 为 ``operation_jobs`` 增加 ``attempt_id`` / ``lease_token`` /
``event_seq`` / ``last_heartbeat_at``，为 ``server_nodes`` 增加 ``protocol_version`` /
``capabilities_json`` / ``executor_version`` / ``db_version`` / ``last_heartbeat_at``。
本模块优先使用这些列；若迁移尚未落地（并行开发期），attempt/lease/事件水位回退到
``operation_jobs.payload_json`` 的保留键 ``_agent_attempt``，节点协议/能力回退到
``server_nodes.metadata_json`` 的 ``agent_protocol`` 子键，不修改模型。
只在 payload 中回退时，返回给 Agent 的 ``payload`` 会剔除保留键。
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ConflictError, ErrorCode
from app.core.security import constant_time_equals
from app.db.base import utcnow
from app.modules.ops import service
from app.modules.ops.models_center import OperationJob, ServerNode, TenantInstance
from app.modules.ops.schemas import TelemetryPayload

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- 协议常量
PROTOCOL_VERSION = 2
# 领取变更任务所需的最低协议：旧 Agent（未上报或 <2）只能领取只读任务
MIN_MUTATION_PROTOCOL = 2
JOB_LEASE_SECONDS = 120
HEARTBEAT_INTERVAL_SECONDS = 30
TELEMETRY_INTERVAL_SECONDS = 30
JOB_POLL_INTERVAL_SECONDS = 5
MAX_TELEMETRY_BATCH = 20

READONLY_JOB_TYPES = frozenset({"logs"})
MUTATION_JOB_TYPES = frozenset({"deploy", "upgrade", "restart", "backup", "config"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})
UNKNOWN_STATUS = "unknown"
STATUS_ACTIVE = frozenset({"leased", "running"})
STATUS_VALUES = frozenset(
    {"running", "leased", "succeeded", "failed", "unknown", "reporting"}
)

_PROTOCOL_STATE_KEY = "agent_protocol"
_ATTEMPT_PAYLOAD_KEY = "_agent_attempt"

# --------------------------------------------------------------------------- 请求模型
class NodeHeartbeatIn(BaseModel):
    """节点心跳：兼容 Agent 2 字段，也兼容旧客户端多传/少传。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    hostname: str = Field(default="", max_length=255)
    app_version: str = Field(default="", max_length=64)
    agent_version: str = Field(default="", max_length=32)
    executor_version: str = Field(default="", max_length=32)
    protocol_version: int = Field(default=0, ge=0, le=10000)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    boot_id: str = Field(default="", max_length=64)
    seq: int = Field(default=0, ge=0, le=2_000_000_000)
    role: str = Field(default="", max_length=32)
    db_version: str = Field(default="", max_length=64)
    uptime_seconds: int | None = Field(default=None, ge=0)
    sent_at: datetime | None = None
    state: dict[str, Any] | None = None


class JobPollIn(BaseModel):
    """任务轮询请求体（旧 Agent 发 ``{}`` 也合法）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    protocol_version: int = Field(default=0, ge=0, le=10000)
    agent_version: str = Field(default="", max_length=32)
    executor_version: str = Field(default="", max_length=32)
    app_version: str = Field(default="", max_length=64)
    boot_id: str = Field(default="", max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=64)
    mutation_job_types: list[str] = Field(default_factory=list, max_length=32)


class JobHeartbeatIn(BaseModel):
    """任务执行心跳（续租）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    attempt_id: str = Field(default="", max_length=36)
    lease_token: str = Field(default="", max_length=128)
    phase: str = Field(default="running", max_length=32)
    lease_seconds: int = Field(default=JOB_LEASE_SECONDS, ge=1, le=3600)
    agent_version: str = Field(default="", max_length=32)
    protocol_version: int = Field(default=0, ge=0, le=10000)
    event_seq: int = Field(default=0, ge=0, le=1_000_000)
    sent_at: datetime | None = None


class JobEventIn(BaseModel):
    """任务事件：不沿用 ``schemas.AgentJobEvent``（其 ``extra=forbid`` 会 422 新字段）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    status: str = Field(default="running", max_length=16)
    step_no: int | None = Field(default=None, ge=1, le=100)
    step_name: str = Field(default="", max_length=64)
    step_status: str = Field(default="", max_length=16)
    message: str = Field(default="", max_length=1000)
    result: dict[str, Any] | None = None
    error: str = Field(default="", max_length=500)
    checks: dict[str, Any] | None = None
    attempt_id: str = Field(default="", max_length=36)
    lease_token: str = Field(default="", max_length=128)
    event_seq: int = Field(default=0, ge=0, le=1_000_000)
    agent_protocol_version: int = Field(default=0, ge=0, le=10000)


class JobReconcileIn(BaseModel):
    """节点本地执行批次 / 阶段 / 结果上报（unknown 核对，不重跑变更）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="allow")

    attempt_id: str = Field(default="", max_length=36)
    lease_token: str = Field(default="", max_length=128)
    status: str = Field(default="", max_length=32)
    local_status: str = Field(default="", max_length=32)
    phase: str = Field(default="", max_length=64)
    batch: int | None = Field(default=None, ge=0)
    batches: Any | None = None
    step_no: int | None = Field(default=None, ge=0, le=100)
    step_name: str = Field(default="", max_length=64)
    result: dict[str, Any] | None = None
    error: str = Field(default="", max_length=500)
    checks: dict[str, Any] | None = None
    event_seq: int = Field(default=0, ge=0, le=1_000_000)
    phases: list[dict[str, Any]] = Field(default_factory=list, max_length=200)


class TelemetrySampleIn(TelemetryPayload):
    """单条指标样本：在旧 ``TelemetryPayload`` 上增加去重字段并容忍未知字段。"""

    model_config = ConfigDict(extra="allow")

    boot_id: str = Field(default="", max_length=64)
    sample_seq: int | None = Field(default=None, ge=0, le=2_000_000_000)


# --------------------------------------------------------------------------- 通用工具
@dataclass(frozen=True, slots=True)
class AttemptState:
    attempt_id: str
    lease_token: str
    event_seq: int
    protocol_version: int


def _now() -> datetime:
    return utcnow()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat() + "Z"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- 认证
def authenticate_node(center: Session, token: str) -> ServerNode:
    """节点 Bearer 令牌认证；失败统一 401，不泄露节点是否存在。"""
    normalized = (token or "").strip()
    if not normalized:
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED, "节点令牌无效")
    node = center.execute(
        select(ServerNode).where(ServerNode.token_hash == service.hash_token(normalized))
    ).scalar_one_or_none()
    if node is None:
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED, "节点令牌无效")
    return node


# --------------------------------------------------------------------------- 节点协议状态（metadata_json）
def _metadata(node: ServerNode) -> dict[str, Any]:
    return dict(node.metadata_json) if isinstance(node.metadata_json, dict) else {}


def protocol_state(node: ServerNode) -> dict[str, Any]:
    state = _metadata(node).get(_PROTOCOL_STATE_KEY)
    return dict(state) if isinstance(state, dict) else {}


def _save_protocol_state(node: ServerNode, state: dict[str, Any]) -> None:
    metadata = _metadata(node)
    metadata[_PROTOCOL_STATE_KEY] = state
    node.metadata_json = metadata


def _node_protocol(node: ServerNode) -> int:
    value = getattr(node, "protocol_version", 0) or 0
    if value:
        return max(0, _safe_int(value))
    return max(0, _safe_int(protocol_state(node).get("protocol_version")))


def _set_node_protocol(node: ServerNode, value: int, state: dict[str, Any]) -> None:
    protocol = max(0, _safe_int(value))
    if hasattr(node, "protocol_version"):
        node.protocol_version = protocol
    else:  # 并行开发期回退：不修改模型
        state["protocol_version"] = protocol


def _node_capabilities(node: ServerNode) -> list[str]:
    value = getattr(node, "capabilities_json", None)
    if isinstance(value, list):
        return [str(item) for item in value]
    state = protocol_state(node).get("capabilities")
    return [str(item) for item in state] if isinstance(state, list) else []


def _set_node_capabilities(node: ServerNode, capabilities: list[str]) -> None:
    cleaned = [str(item)[:64] for item in capabilities][:64]
    if hasattr(node, "capabilities_json"):
        node.capabilities_json = cleaned
    else:
        state = protocol_state(node)
        state["capabilities"] = cleaned
        _save_protocol_state(node, state)


def _touch_node(node: ServerNode, now: datetime) -> None:
    node.status = "online"
    node.last_seen_at = now
    if hasattr(node, "last_heartbeat_at"):
        node.last_heartbeat_at = now


# --------------------------------------------------------------------------- 任务批次状态
def _job_payload(job: OperationJob) -> dict[str, Any]:
    return dict(job.payload_json) if isinstance(job.payload_json, dict) else {}


def _attempt_fallback(job: OperationJob) -> dict[str, Any]:
    value = _job_payload(job).get(_ATTEMPT_PAYLOAD_KEY)
    return dict(value) if isinstance(value, dict) else {}


def _mem_get(job: OperationJob, column: str, key: str) -> Any:
    value = getattr(job, column, None)
    if value not in (None, ""):
        return value
    return _attempt_fallback(job).get(key)


def _mem_set(job: OperationJob, column: str, key: str, value: Any) -> None:
    if hasattr(job, column):
        setattr(job, column, value)
        return
    payload = _job_payload(job)
    state = dict(payload.get(_ATTEMPT_PAYLOAD_KEY) or {})
    state[key] = value
    payload[_ATTEMPT_PAYLOAD_KEY] = state
    job.payload_json = payload


def attempt_state(job: OperationJob) -> AttemptState:
    return AttemptState(
        attempt_id=str(_mem_get(job, "attempt_id", "attempt_id") or ""),
        lease_token=str(_mem_get(job, "lease_token", "lease_token") or ""),
        event_seq=_safe_int(_mem_get(job, "event_seq", "event_seq")),
        protocol_version=_safe_int(
            getattr(job, "agent_protocol_version", 0)
            or _attempt_fallback(job).get("protocol_version")
        ),
    )


def _set_attempt(job: OperationJob, *, attempt_id: str, lease_token: str, protocol: int) -> None:
    _mem_set(job, "attempt_id", "attempt_id", attempt_id)
    _mem_set(job, "lease_token", "lease_token", lease_token)
    _mem_set(job, "event_seq", "event_seq", 0)
    if hasattr(job, "agent_protocol_version"):
        job.agent_protocol_version = max(0, _safe_int(protocol))
    else:
        _mem_set(job, "agent_protocol_version", "protocol_version", max(0, _safe_int(protocol)))


def _set_event_seq(job: OperationJob, seq: int) -> None:
    _mem_set(job, "event_seq", "event_seq", max(0, _safe_int(seq)))


def _job_protocol(job: OperationJob) -> int:
    value = getattr(job, "agent_protocol_version", 0)
    if value:
        return _safe_int(value)
    return _safe_int(_attempt_fallback(job).get("protocol_version"))


def _legacy_attempt(job: OperationJob) -> bool:
    """旧协议租约（或迁移前领取的租约）：允许不带 attempt 的旧 Agent 事件。"""
    return _job_protocol(job) < MIN_MUTATION_PROTOCOL


def _agent_payload(job: OperationJob) -> dict[str, Any]:
    payload = _job_payload(job)
    payload.pop(_ATTEMPT_PAYLOAD_KEY, None)
    return payload


def _mark_unknown(job: OperationJob, now: datetime, error: str) -> None:
    job.status = UNKNOWN_STATUS
    job.error = (error or "执行结果待核对")[:500]
    job.finished_at = now
    job.updated_at = now
    if hasattr(job, "upgrade_status") and job.job_type in {"deploy", "upgrade"}:
        try:
            job.upgrade_status = "unknown"
        except Exception:  # pragma: no cover - 字段不存在时忽略
            pass


def _sync_instance_safe(center: Session, job: OperationJob, now: datetime) -> None:
    """升级任务状态回写实例展示字段；依赖 task-16 service 时做防御调用。"""
    sync = getattr(service, "_sync_instance_after_job", None)
    if callable(sync):
        try:
            sync(center, job, now)
            return
        except Exception:  # pragma: no cover - 并行开发期字段未齐时忽略
            logger.debug("同步实例升级状态失败", exc_info=True)
    if job.job_type not in {"deploy", "upgrade"}:
        return
    instance = center.get(TenantInstance, int(job.instance_id))
    if instance is None:
        return
    for field in ("upgrade_status", "actual_version", "db_version", "agent_version"):
        if hasattr(job, field) and hasattr(instance, field):
            value = getattr(job, field)
            if value:
                setattr(instance, field, value)
    instance.updated_at = now


# --------------------------------------------------------------------------- 协议要求
def min_protocol_for_mutation(center: Session, target_version: str = "") -> int:
    """变更任务最低协议：协议常量与目标发布版本 ``min_agent_protocol`` 取较大值。"""
    required = MIN_MUTATION_PROTOCOL
    try:
        release: Any = None
        if target_version and hasattr(service, "get_release_by_version"):
            release = service.get_release_by_version(center, target_version)
        elif hasattr(service, "list_releases"):
            releases = service.list_releases(center) or []
            release = releases[0] if releases else None
        if isinstance(release, dict):
            required = max(required, _safe_int(release.get("min_agent_protocol")))
        elif release is not None:
            required = max(required, _safe_int(getattr(release, "min_agent_protocol", 0)))
    except Exception:  # noqa: BLE001 - 迁移未落地/表不存在时按协议常量
        logger.debug("读取发布版本最低协议失败，使用默认 %s", required)
    return required


def _job_min_protocol(center: Session, job: OperationJob) -> int:
    if job.job_type in READONLY_JOB_TYPES:
        return 0
    return min_protocol_for_mutation(center, str(job.target_version or ""))


def _mutation_allowed(center: Session, node: ServerNode, target_version: str = "") -> bool:
    return _node_protocol(node) >= min_protocol_for_mutation(center, target_version)


# --------------------------------------------------------------------------- 节点心跳
def node_heartbeat(center: Session, node: ServerNode, payload: NodeHeartbeatIn) -> dict[str, Any]:
    """更新节点心跳/版本/能力，返回服务端协议要求与升级提示。"""
    now = _now()
    required = min_protocol_for_mutation(center)
    state = protocol_state(node)
    previous_protocol = _node_protocol(node)
    boot_id = payload.boot_id or str(state.get("boot_id") or "")
    seq = _safe_int(payload.seq)
    stale = bool(
        boot_id
        and boot_id == str(state.get("boot_id") or "")
        and seq
        and seq <= _safe_int(state.get("heartbeat_seq"))
    )
    if stale:
        # 乱序/重放心跳：只刷新在线状态，不允许旧值覆盖新版本信息
        _touch_node(node, now)
        node.updated_at = now
        center.commit()
    else:
        if payload.hostname:
            node.hostname = payload.hostname[:255]
        if payload.agent_version:
            node.agent_version = payload.agent_version[:32]
        if payload.app_version:
            node.app_version = payload.app_version[:64]
        for field, value in (
            ("executor_version", payload.executor_version),
            ("db_version", payload.db_version),
        ):
            if value and hasattr(node, field):
                setattr(node, field, value[:64])
        _set_node_protocol(node, payload.protocol_version, state)
        if "capabilities" in payload.model_fields_set:
            _set_node_capabilities(node, payload.capabilities)
        if boot_id:
            state["boot_id"] = boot_id[:64]
        if seq:
            state["heartbeat_seq"] = seq
        if payload.role:
            state["role"] = payload.role[:32]
        state["updated_at"] = _iso(now)
        _save_protocol_state(node, state)
        _touch_node(node, now)
        node.updated_at = now
        instance = center.get(TenantInstance, int(node.instance_id))
        if instance is not None:
            if payload.app_version and hasattr(instance, "actual_version"):
                instance.actual_version = payload.app_version[:64]
            if payload.app_version:
                instance.app_version = payload.app_version[:64]
            if payload.db_version and hasattr(instance, "db_version"):
                instance.db_version = payload.db_version[:64]
            if payload.agent_version and hasattr(instance, "agent_version"):
                instance.agent_version = payload.agent_version[:32]
            if payload.executor_version and hasattr(instance, "executor_version"):
                instance.executor_version = payload.executor_version[:32]
            instance.updated_at = now
            center.add(instance)
        center.commit()

    protocol = _node_protocol(node)
    upgrade_required = protocol < required
    return {
        "ok": True,
        "node_id": str(node.id),
        "server_time": _iso(now),
        "protocol_version": PROTOCOL_VERSION,
        "node_protocol_version": protocol,
        "previous_protocol_version": previous_protocol,
        "min_protocol_version": required,
        "min_agent_protocol": required,
        "mutation_allowed": not upgrade_required,
        "upgrade_required": upgrade_required,
        "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
        "job_lease_seconds": JOB_LEASE_SECONDS,
        "job_poll_seconds": JOB_POLL_INTERVAL_SECONDS,
        "telemetry_seconds": TELEMETRY_INTERVAL_SECONDS,
        "boot_id": str(state.get("boot_id") or ""),
        "seq_ack": _safe_int(state.get("heartbeat_seq")),
        "stale": stale,
        "capabilities": _node_capabilities(node),
        "agent_version": node.agent_version,
        "executor_version": getattr(node, "executor_version", ""),
        "server_role": "center",
    }


# --------------------------------------------------------------------------- 任务领取
def _load_job(center: Session, node: ServerNode, job_id: int, *, lock: bool = False) -> OperationJob:
    stmt = select(OperationJob).where(
        OperationJob.id == int(job_id), OperationJob.node_id == node.id
    )
    if lock:
        stmt = stmt.with_for_update()
    job = center.execute(stmt).scalar_one_or_none()
    if job is None:
        raise AppError(ErrorCode.NOT_FOUND, "任务不存在")
    return job


def _job_view(center: Session, job: OperationJob) -> dict[str, Any]:
    attempt = attempt_state(job)
    return {
        "id": str(job.id),
        "job_type": job.job_type,
        "target_version": job.target_version or "",
        "payload": _agent_payload(job),
        "attempts": _safe_int(job.attempts),
        "attempt_id": attempt.attempt_id,
        "lease_token": attempt.lease_token,
        "lease_expires_at": _iso(job.lease_expires_at),
        "lease_seconds": JOB_LEASE_SECONDS,
        "status": job.status,
        "agent_protocol_version": attempt.protocol_version,
        "upgrade_status": getattr(job, "upgrade_status", "") or "",
        "min_agent_protocol": _job_min_protocol(center, job),
    }


def _blocked_job_view(center: Session, job: OperationJob, reason: str) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "job_type": job.job_type,
        "target_version": job.target_version or "",
        "min_agent_protocol": _job_min_protocol(center, job),
        "reason": reason,
    }


def _empty_poll(
    center: Session,
    node: ServerNode,
    *,
    blocked_job: OperationJob | None = None,
    message: str = "",
) -> dict[str, Any]:
    required = min_protocol_for_mutation(center)
    protocol = _node_protocol(node)
    data: dict[str, Any] = {
        "job": None,
        "server_time": _iso(_now()),
        "protocol_version": PROTOCOL_VERSION,
        "min_protocol_version": required,
        "min_agent_protocol": required,
        "upgrade_required": protocol < required,
        "mutation_allowed": protocol >= required,
        "lease_seconds": JOB_LEASE_SECONDS,
        "blocked_job": None,
        "message": message,
    }
    if blocked_job is not None:
        data["blocked_job"] = _blocked_job_view(center, blocked_job, "agent_protocol_too_low")
    return data


def poll_job(
    center: Session,
    node: ServerNode,
    payload: JobPollIn | None = None,
    *,
    claimed_protocol: int = 0,
) -> dict[str, Any]:
    """任务领取：租约内重复 poll 返回同一 attempt；旧 Agent 不领取变更任务。"""
    now = _now()
    required = min_protocol_for_mutation(center)
    claimed = max(_safe_int(claimed_protocol), _safe_int(payload.protocol_version) if payload else 0)
    if claimed > _node_protocol(node):
        state = protocol_state(node)
        _set_node_protocol(node, claimed, state)
        _save_protocol_state(node, state)
    if payload is not None:
        if payload.agent_version:
            node.agent_version = payload.agent_version[:32]
        if payload.capabilities:
            _set_node_capabilities(node, payload.capabilities)
    protocol = _node_protocol(node)
    _touch_node(node, now)
    node.updated_at = now

    # 过期租约 → unknown（不自动重跑、不释放占用）
    stale_jobs = center.execute(
        select(OperationJob).where(
            OperationJob.node_id == node.id,
            OperationJob.status.in_(tuple(STATUS_ACTIVE)),
            OperationJob.lease_expires_at.is_not(None),
            OperationJob.lease_expires_at < now,
        )
    ).scalars().all()
    for item in stale_jobs:
        _mark_unknown(item, now, "执行中失联，结果待核对")
        _sync_instance_safe(center, item, now)

    # unknown 占用：reconcile 前不派发新任务
    unknown = center.execute(
        select(OperationJob)
        .where(OperationJob.node_id == node.id, OperationJob.status == UNKNOWN_STATUS)
        .order_by(OperationJob.id.asc())
        .limit(1)
    ).scalars().first()
    if unknown is not None:
        center.commit()
        return _empty_poll(
            center,
            node,
            blocked_job=unknown,
            message="存在待核对（unknown）任务，reconcile 前不派发新任务",
        )

    # 同一租约内重复 poll：返回同一执行批次
    active = center.execute(
        select(OperationJob)
        .where(
            OperationJob.node_id == node.id,
            OperationJob.status.in_(tuple(STATUS_ACTIVE)),
            OperationJob.lease_expires_at.is_not(None),
            OperationJob.lease_expires_at >= now,
        )
        .order_by(OperationJob.id.desc())
        .limit(1)
    ).scalars().first()
    if active is not None:
        if not attempt_state(active).attempt_id:
            _lease_job(active, node, now)
        center.commit()
        data = _empty_poll(center, node)
        data["job"] = _job_view(center, active)
        data["reused"] = True
        return data

    # 新任务：按顺序找第一个当前协议允许执行的任务
    candidates = center.execute(
        select(OperationJob)
        .where(OperationJob.node_id == node.id, OperationJob.status == "pending")
        .order_by(OperationJob.id.asc())
        .with_for_update()
    ).scalars().all()
    blocked: OperationJob | None = None
    for row in candidates:
        job_required = _job_min_protocol(center, row)
        if protocol < job_required:
            blocked = blocked or row
            continue
        _lease_job(row, node, now)
        center.commit()
        data = _empty_poll(center, node)
        data["job"] = _job_view(center, row)
        data["reused"] = False
        return data

    center.commit()
    message = ""
    if blocked is not None:
        message = (
            f"存在变更任务（{blocked.job_type}），当前 Agent 协议 {protocol} "
            f"低于最低要求 {_job_min_protocol(center, blocked)}，请先升级 Agent"
        )
    return _empty_poll(center, node, blocked_job=blocked, message=message)


def _lease_job(job: OperationJob, node: ServerNode, now: datetime) -> None:
    protocol = _node_protocol(node)
    job.status = "leased"
    job.attempts = _safe_int(job.attempts) + 1
    _set_attempt(
        job,
        attempt_id=str(uuid.uuid4()),
        lease_token=secrets.token_urlsafe(32)[:64],
        protocol=protocol,
    )
    job.lease_expires_at = now + timedelta(seconds=JOB_LEASE_SECONDS)
    if hasattr(job, "last_heartbeat_at"):
        job.last_heartbeat_at = now
    job.started_at = job.started_at or now
    job.updated_at = now
    if hasattr(job, "agent_version") and node.agent_version:
        job.agent_version = node.agent_version[:32]
    if hasattr(job, "upgrade_status") and job.job_type in {"deploy", "upgrade"}:
        if not job.upgrade_status:
            job.upgrade_status = "pending"


# --------------------------------------------------------------------------- 任务心跳（续租）
def job_heartbeat(
    center: Session, node: ServerNode, job_id: int, payload: JobHeartbeatIn
) -> dict[str, Any]:
    """校验节点/attempt/lease_token 后续租 120s；过期或错误 token 返回 409。"""
    now = _now()
    job = _load_job(center, node, job_id, lock=True)
    attempt = attempt_state(job)
    if not attempt.attempt_id:
        raise ConflictError("任务没有有效执行批次，请重新领取")
    if not payload.attempt_id or payload.attempt_id != attempt.attempt_id:
        raise ConflictError("执行批次不匹配（旧 attempt），拒绝续租")
    if attempt.lease_token and not constant_time_equals(
        payload.lease_token or "", attempt.lease_token
    ):
        raise ConflictError("租约令牌无效，拒绝续租")

    job.last_heartbeat_at = now if hasattr(job, "last_heartbeat_at") else None
    if hasattr(node, "last_heartbeat_at"):
        node.last_heartbeat_at = now
    _touch_node(node, now)
    node.updated_at = now

    if job.status in TERMINAL_STATUSES:
        job.updated_at = now
        center.commit()
        return {
            "ok": True,
            "job_id": str(job.id),
            "attempt_id": attempt.attempt_id,
            "status": job.status,
            "upgrade_status": getattr(job, "upgrade_status", "") or "",
            "finished": True,
            "lease_lost": False,
            "cancel_requested": False,
            "server_time": _iso(now),
        }

    expired = job.lease_expires_at is not None and job.lease_expires_at < now
    if expired and job.status != UNKNOWN_STATUS:
        _mark_unknown(job, now, "Agent 心跳超时，结果待核对")
        _sync_instance_safe(center, job, now)
        center.commit()
        raise ConflictError("租约已过期，任务已标记为待核对（unknown），拒绝续租")

    job.lease_expires_at = now + timedelta(seconds=JOB_LEASE_SECONDS)
    if hasattr(job, "last_heartbeat_at"):
        job.last_heartbeat_at = now
    if payload.event_seq:
        _set_event_seq(job, max(attempt.event_seq, _safe_int(payload.event_seq)))
    marked_unknown = False
    if payload.phase == UNKNOWN_STATUS and job.status != UNKNOWN_STATUS:
        _mark_unknown(job, now, "Agent 上报执行结果待核对")
        _sync_instance_safe(center, job, now)
        marked_unknown = True
    elif job.status == "leased":
        job.status = "running"
    if payload.agent_version and hasattr(job, "agent_version"):
        job.agent_version = payload.agent_version[:32]
    if payload.protocol_version and hasattr(job, "agent_protocol_version"):
        job.agent_protocol_version = max(0, _safe_int(payload.protocol_version))
    job.updated_at = now
    center.commit()
    return {
        "ok": True,
        "job_id": str(job.id),
        "attempt_id": attempt.attempt_id,
        "status": job.status,
        "upgrade_status": getattr(job, "upgrade_status", "") or "",
        "lease_expires_at": _iso(job.lease_expires_at),
        "lease_seconds": JOB_LEASE_SECONDS,
        "lease_lost": False,
        "cancel_requested": False,
        "occupied": job.status == UNKNOWN_STATUS,
        "marked_unknown": marked_unknown,
        "server_time": _iso(now),
    }


# --------------------------------------------------------------------------- 任务事件
def _validate_event_attempt(job: OperationJob, payload: JobEventIn) -> AttemptState:
    attempt = attempt_state(job)
    provided_attempt = (payload.attempt_id or "").strip()
    if provided_attempt:
        if attempt.attempt_id and provided_attempt != attempt.attempt_id:
            raise ConflictError("旧执行批次的事件被拒绝")
        if not attempt.attempt_id:
            _mem_set(job, "attempt_id", "attempt_id", provided_attempt)
            attempt = attempt_state(job)
    elif attempt.attempt_id and not _legacy_attempt(job):
        # 过渡兼容：新 Agent 尚未回传 attempt_id 时，仅首次领取可接受
        if _safe_int(job.attempts) > 1:
            raise ConflictError("缺少执行批次 attempt_id，且任务已重新领取，拒绝旧事件")
    if payload.lease_token and attempt.lease_token and not constant_time_equals(
        payload.lease_token, attempt.lease_token
    ):
        raise ConflictError("租约令牌无效，拒绝写入事件")
    return attempt


def _event_response(
    job: OperationJob,
    attempt: AttemptState,
    *,
    duplicate: bool = False,
    ignored: bool = False,
    message: str = "",
) -> dict[str, Any]:
    return {
        "ok": True,
        "duplicate": duplicate,
        "ignored": ignored,
        "event_seq": attempt.event_seq,
        "status": job.status,
        "attempt_id": attempt.attempt_id,
        "upgrade_status": getattr(job, "upgrade_status", "") or "",
        "checks_status": getattr(job, "checks_status", "") or "",
        "message": message,
        "server_time": _iso(_now()),
    }


def job_event(
    center: Session, node: ServerNode, job_id: int, payload: JobEventIn
) -> dict[str, Any]:
    """事件幂等 + 终态 attempt 校验；内容应用复用 ``service.agent_job_event``。"""
    now = _now()
    job = _load_job(center, node, job_id, lock=True)
    attempt = _validate_event_attempt(job, payload)
    seq = _safe_int(payload.event_seq)
    status = (payload.status or "running").strip()
    if status not in STATUS_VALUES:
        status = "running"

    if seq and seq <= attempt.event_seq:
        return _event_response(job, attempt, duplicate=True)

    if job.status in TERMINAL_STATUSES:
        if status == job.status:
            if seq:
                _set_event_seq(job, seq)
                job.updated_at = now
                center.commit()
                attempt = attempt_state(job)
            return _event_response(job, attempt, duplicate=True)
        if status in {"succeeded", "failed", "unknown"}:
            raise ConflictError("任务已处于终态，拒绝冲突结果覆盖")
        return _event_response(job, attempt, duplicate=True, message="任务已终态，忽略执行中事件")

    if job.status == UNKNOWN_STATUS and status not in {"succeeded", "failed", "unknown"}:
        if seq:
            _set_event_seq(job, seq)
            center.commit()
            attempt = attempt_state(job)
        return _event_response(job, attempt, ignored=True, message="结果待核对，忽略执行中事件")

    if job.status == "pending" and status in {"succeeded", "failed", "unknown"}:
        raise ConflictError("任务尚未派发，拒绝执行结果")

    _touch_node(node, now)
    node.updated_at = now
    try:
        result = service.agent_job_event(center, node, int(job.id), payload)
    except AppError:
        center.rollback()
        raise
    job = center.get(OperationJob, int(job.id))
    attempt = attempt_state(job)
    if seq:
        _set_event_seq(job, max(attempt.event_seq, seq))
    if job.status not in TERMINAL_STATUSES and job.status != UNKNOWN_STATUS:
        job.lease_expires_at = now + timedelta(seconds=JOB_LEASE_SECONDS)
        if hasattr(job, "last_heartbeat_at"):
            job.last_heartbeat_at = now
    job.updated_at = now
    center.commit()
    attempt = attempt_state(job)
    data = _event_response(job, attempt)
    if isinstance(result, dict):
        data["duplicate"] = bool(result.get("duplicate"))
        data["upgrade_status"] = result.get("upgrade_status", data["upgrade_status"])
        data["checks_status"] = result.get("checks_status", data["checks_status"])
    return data


# --------------------------------------------------------------------------- 任务对账（reconcile）
def _normalize_local_status(payload: JobReconcileIn) -> str:
    for value in (payload.local_status, payload.status, payload.phase):
        normalized = (value or "").strip().lower()
        if normalized in {"succeeded", "success", "ok", "done", "reconciled"}:
            return "succeeded"
        if normalized in {"failed", "fail", "error"}:
            return "failed"
        if normalized in {"unknown", "lost", "interrupted"}:
            return UNKNOWN_STATUS
        if normalized in {"running", "leased", "reporting"}:
            return "running"
    return ""


def _reconcile_response(
    job: OperationJob | None,
    *,
    known: bool,
    action: str,
    local_status: str = "",
    message: str = "",
    retry_allowed: bool = False,
) -> dict[str, Any]:
    return {
        "ok": True,
        "known": known,
        "job_id": str(job.id) if job is not None else "",
        "action": action,
        "local_status": local_status,
        "server_status": job.status if job is not None else "",
        "attempt_id": attempt_state(job).attempt_id if job is not None else "",
        "attempts": _safe_int(job.attempts) if job is not None else 0,
        "upgrade_status": (getattr(job, "upgrade_status", "") or "") if job is not None else "",
        "checks_status": (getattr(job, "checks_status", "") or "") if job is not None else "",
        "lease_expires_at": _iso(job.lease_expires_at) if job is not None else None,
        "finished_at": _iso(job.finished_at) if job is not None else None,
        "retry_allowed": retry_allowed,
        "message": message,
        "server_time": _iso(_now()),
    }


def _merge_reconcile_meta(
    center: Session,
    job: OperationJob,
    payload: JobReconcileIn,
    local_status: str,
    now: datetime,
) -> None:
    result = dict(job.result_json) if isinstance(job.result_json, dict) else {}
    result["reconcile"] = {
        "local_status": local_status,
        "phase": payload.phase or "",
        "batch": payload.batch,
        "attempt_id": attempt_state(job).attempt_id,
        "reported_at": _iso(now),
        "phases": payload.phases[:100],
    }
    job.result_json = result
    job.updated_at = now


def reconcile_job(
    center: Session, node: ServerNode, job_id: int, payload: JobReconcileIn
) -> dict[str, Any]:
    """节点上报本地执行批次/阶段/结果；只核对标记，不重跑变更。"""
    now = _now()
    try:
        job = _load_job(center, node, job_id, lock=True)
    except AppError:
        return _reconcile_response(
            None,
            known=False,
            action="ignored_unknown_task",
            message="中心无此任务记录，请人工核对本地日志后清理占用",
        )

    # 批次/令牌校验：错误 attempt 一律拒绝覆盖
    attempt = attempt_state(job)
    if payload.attempt_id:
        if attempt.attempt_id and payload.attempt_id != attempt.attempt_id:
            center.rollback()
            raise ConflictError("执行批次不匹配，拒绝核对覆盖")
        if not attempt.attempt_id:
            _mem_set(job, "attempt_id", "attempt_id", payload.attempt_id)
            attempt = attempt_state(job)
    elif attempt.attempt_id and not _legacy_attempt(job):
        center.rollback()
        raise ConflictError("缺少执行批次 attempt_id，拒绝核对")
    if payload.lease_token and attempt.lease_token and not constant_time_equals(
        payload.lease_token, attempt.lease_token
    ):
        center.rollback()
        raise ConflictError("租约令牌无效，拒绝核对")

    local = _normalize_local_status(payload)

    if job.status == "pending":
        center.commit()
        return _reconcile_response(
            job,
            known=True,
            action="not_dispatched",
            local_status=local,
            message="任务尚未派发，不做任何变更或重跑",
        )

    if job.status in TERMINAL_STATUSES:
        center.commit()
        confirmed = local == job.status
        return _reconcile_response(
            job,
            known=True,
            action="already_terminal",
            local_status=local,
            message="任务已是终态且不覆盖" if not confirmed else "本地结果与中心一致",
        )

    if job.status == UNKNOWN_STATUS:
        if local in {"succeeded", "failed"}:
            event = JobEventIn(
                status=local,
                result=payload.result,
                error=payload.error,
                checks=payload.checks,
                attempt_id=payload.attempt_id or attempt.attempt_id,
                lease_token=payload.lease_token,
                event_seq=0,
            )
            service.agent_job_event(center, node, int(job.id), event)
            job = center.get(OperationJob, int(job.id))
            _merge_reconcile_meta(center, job, payload, local, now)
            center.commit()
            return _reconcile_response(
                job,
                known=True,
                action="marked_succeeded" if local == "succeeded" else "marked_failed",
                local_status=local,
                message="按本地执行日志核对并标记终态（未重跑变更）",
            )
        _merge_reconcile_meta(center, job, payload, local, now)
        center.commit()
        return _reconcile_response(
            job,
            known=True,
            action="kept_unknown",
            local_status=local,
            message="本地状态无法确认，继续保持 unknown 占用，等待人工核对",
        )

    # 执行中（leased/running）：本地终态可恢复；否则保持/超时转 unknown
    expired = job.lease_expires_at is not None and job.lease_expires_at < now
    if local in {"succeeded", "failed"}:
        event = JobEventIn(
            status=local,
            result=payload.result,
            error=payload.error,
            checks=payload.checks,
            attempt_id=payload.attempt_id or attempt.attempt_id,
            lease_token=payload.lease_token,
            event_seq=0,
        )
        service.agent_job_event(center, node, int(job.id), event)
        job = center.get(OperationJob, int(job.id))
        _merge_reconcile_meta(center, job, payload, local, now)
        center.commit()
        return _reconcile_response(
            job,
            known=True,
            action="marked_succeeded" if local == "succeeded" else "marked_failed",
            local_status=local,
            message="按本地执行日志恢复终态（未重跑变更）",
        )
    if local == UNKNOWN_STATUS or expired:
        _mark_unknown(job, now, payload.error or "对账未确认本地结果，结果待核对")
        _sync_instance_safe(center, job, now)
        _merge_reconcile_meta(center, job, payload, local or UNKNOWN_STATUS, now)
        center.commit()
        return _reconcile_response(
            job,
            known=True,
            action="marked_unknown",
            local_status=local or UNKNOWN_STATUS,
            message="租约已过期或本地结果未知，标记 unknown，不自动重跑",
        )
    _merge_reconcile_meta(center, job, payload, local, now)
    center.commit()
    return _reconcile_response(
        job,
        known=True,
        action="continue_execution",
        local_status=local,
        message="本地仍在执行，继续按心跳续租；中心不会重派任务",
    )


# --------------------------------------------------------------------------- 指标批量
def _extract_samples(payload: dict[str, Any]) -> list[Any] | None:
    for key in ("samples", "items", "batch"):
        if key in payload:
            value = payload.get(key)
            if not isinstance(value, list):
                raise AppError(ErrorCode.VALIDATION_ERROR, f"telemetry.{key} 必须是数组")
            return value
    return None


def _telemetry_state(node: ServerNode) -> tuple[dict[str, Any], dict[str, Any]]:
    state = protocol_state(node)
    telemetry = state.get("telemetry")
    if not isinstance(telemetry, dict):
        telemetry = {}
    boots = telemetry.get("boots")
    if not isinstance(boots, dict):
        boots = {}
    telemetry["boots"] = boots
    state["telemetry"] = telemetry
    return state, telemetry


def _is_duplicate_sample(telemetry: dict[str, Any], boot_id: str, sample_seq: int | None) -> bool:
    if not boot_id or sample_seq is None:
        return False
    boots = telemetry.setdefault("boots", {})
    last = _safe_int(boots.get(boot_id), -1)
    return int(sample_seq) <= last


def _record_sample_seq(telemetry: dict[str, Any], boot_id: str, sample_seq: int) -> None:
    boots = telemetry.setdefault("boots", {})
    boots[boot_id] = max(_safe_int(boots.get(boot_id), -1), int(sample_seq))
    # 只保留最近 8 个启动批次，避免 metadata 无限增长
    if len(boots) > 8:
        for key in list(boots)[: len(boots) - 8]:
            boots.pop(key, None)
    telemetry["boot_id"] = boot_id
    telemetry["last_sample_seq"] = int(sample_seq)
    telemetry["updated_at"] = _iso(_now())


def _absorb_versions_from_sample(node: ServerNode, sample: TelemetrySampleIn, state: dict[str, Any]) -> None:
    protocol = _safe_int(getattr(sample, "protocol_version", 0))
    if protocol:
        _set_node_protocol(node, protocol, state)
    capabilities = getattr(sample, "capabilities", None)
    if isinstance(capabilities, list) and capabilities:
        _set_node_capabilities(node, capabilities)
    for field in ("executor_version", "db_version", "agent_version", "app_version"):
        value = getattr(sample, field, "")
        if value and hasattr(node, field):
            setattr(node, field, str(value)[:64])


def _invalid_result(index: int, exc: ValidationError) -> dict[str, Any]:
    errors = exc.errors()
    message = str(errors[0].get("msg"))[:200] if errors else "参数不合法"
    loc = ".".join(str(item) for item in (errors[0].get("loc") if errors else ()))
    return {"index": index, "sample_seq": None, "status": "invalid", "error": (loc + ": " + message).strip(": ")[:240]}


def ingest_telemetry(
    center: Session, node: ServerNode, payload: dict[str, Any]
) -> dict[str, Any]:
    """指标上报：单条旧格式兼容；批量按 boot_id+sample_seq 逐条去重。"""
    if not isinstance(payload, dict):
        raise AppError(ErrorCode.VALIDATION_ERROR, "指标数据必须是 JSON 对象")
    samples = _extract_samples(payload)
    if samples is None:
        return _ingest_single(center, node, payload)
    if len(samples) > MAX_TELEMETRY_BATCH:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"单次最多上报 {MAX_TELEMETRY_BATCH} 条指标，当前 {len(samples)} 条",
        )
    now = _now()
    state, telemetry = _telemetry_state(node)
    top_boot = str(payload.get("boot_id") or "")[:64]
    envelope_protocol = _safe_int(payload.get("protocol_version"))
    if envelope_protocol:
        _set_node_protocol(node, envelope_protocol, state)
    envelope_capabilities = payload.get("capabilities")
    if isinstance(envelope_capabilities, list) and envelope_capabilities:
        _set_node_capabilities(node, envelope_capabilities)
    for field in ("agent_version", "executor_version", "app_version", "db_version"):
        value = payload.get(field)
        if value and hasattr(node, field):
            setattr(node, field, str(value)[:64])

    results: list[dict[str, Any]] = []
    accepted = duplicates = invalid = 0
    has_live_sample = False
    for index, raw in enumerate(samples):
        if not isinstance(raw, dict):
            invalid += 1
            results.append(
                {"index": index, "sample_seq": None, "status": "invalid", "error": "样本必须是 JSON 对象"}
            )
            continue
        try:
            sample = TelemetrySampleIn.model_validate(raw)
        except ValidationError as exc:
            invalid += 1
            results.append(_invalid_result(index, exc))
            continue
        boot_id = str(getattr(sample, "boot_id", "") or top_boot)[:64]
        sample_seq = getattr(sample, "sample_seq", None)
        if _is_duplicate_sample(telemetry, boot_id, sample_seq):
            duplicates += 1
            results.append(
                {"index": index, "sample_seq": sample_seq, "boot_id": boot_id, "status": "duplicate"}
            )
            continue
        if boot_id and sample_seq is not None:
            _record_sample_seq(telemetry, boot_id, int(sample_seq))
        from app.modules.ops.monitoring import is_historical_sample

        live_sample = not is_historical_sample(getattr(sample, "sampled_at", None), now=now)
        if live_sample:
            # 心跳与历史样本分离：历史补报不改版本/在线状态，新样本才更新心跳。
            has_live_sample = True
            _absorb_versions_from_sample(node, sample, state)
        _save_protocol_state(node, state)
        try:
            service.ingest_telemetry(center, node, sample)
        except AppError:
            center.rollback()
            raise
        accepted += 1
        results.append(
            {"index": index, "sample_seq": sample_seq, "boot_id": boot_id, "status": "accepted"}
        )
    _save_protocol_state(node, state)
    if has_live_sample:
        _touch_node(node, now)
        node.last_metrics_at = now
    node.updated_at = now
    center.commit()
    return {
        "ok": True,
        "node_id": str(node.id),
        "accepted": accepted,
        "duplicates": duplicates,
        "invalid": invalid,
        "total": len(samples),
        "batch": True,
        "results": results,
        "next_report_seconds": TELEMETRY_INTERVAL_SECONDS,
        "server_time": _iso(now),
    }


def _ingest_single(center: Session, node: ServerNode, payload: dict[str, Any]) -> dict[str, Any]:
    """单条旧格式：保持原有响应字段，附加 boot_id/sample_seq 去重（若提供）。"""
    now = _now()
    try:
        sample = TelemetrySampleIn.model_validate(payload)
    except ValidationError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "指标数据格式不正确",
            details=[{"loc": ".".join(str(i) for i in err.get("loc", ())), "msg": str(err.get("msg", ""))} for err in exc.errors()[:5]],
        ) from exc
    state, telemetry = _telemetry_state(node)
    boot_id = str(getattr(sample, "boot_id", "") or "")[:64]
    sample_seq = getattr(sample, "sample_seq", None)
    from app.modules.ops.monitoring import is_historical_sample

    live_sample = not is_historical_sample(getattr(sample, "sampled_at", None), now=now)
    duplicate = _is_duplicate_sample(telemetry, boot_id, sample_seq)
    if duplicate:
        if live_sample:
            _touch_node(node, now)
            node.last_metrics_at = now
        center.commit()
        return {
            "ok": True,
            "node_id": str(node.id),
            "accepted": 0,
            "duplicates": 1,
            "duplicate": True,
            "next_report_seconds": TELEMETRY_INTERVAL_SECONDS,
            "server_time": _iso(now),
        }
    if boot_id and sample_seq is not None:
        _record_sample_seq(telemetry, boot_id, int(sample_seq))
    if live_sample:
        _absorb_versions_from_sample(node, sample, state)
    _save_protocol_state(node, state)
    result = service.ingest_telemetry(center, node, sample)
    data = dict(result) if isinstance(result, dict) else {}
    data.update(
        {
            "ok": True,
            "node_id": str(node.id),
            "accepted": 1,
            "duplicates": 0,
            "duplicate": False,
            "next_report_seconds": data.get("next_report_seconds", TELEMETRY_INTERVAL_SECONDS),
        }
    )
    return data


__all__ = [
    "PROTOCOL_VERSION",
    "MIN_MUTATION_PROTOCOL",
    "JOB_LEASE_SECONDS",
    "MAX_TELEMETRY_BATCH",
    "READONLY_JOB_TYPES",
    "MUTATION_JOB_TYPES",
    "NodeHeartbeatIn",
    "JobPollIn",
    "JobHeartbeatIn",
    "JobEventIn",
    "JobReconcileIn",
    "TelemetrySampleIn",
    "authenticate_node",
    "min_protocol_for_mutation",
    "node_heartbeat",
    "poll_job",
    "job_heartbeat",
    "job_event",
    "reconcile_job",
    "ingest_telemetry",
]
