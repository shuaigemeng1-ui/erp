"""统一运维中心：实例、节点、指标、探测与告警服务。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
from sqlalchemy import func, or_, select

from app.core.errors import AppError, ConflictError, ErrorCode
from app.db.base import utcnow
from app.modules.ops.models_center import (
    AlertEvent,
    NodeCheck,
    NodeMetricSample,
    ServerNode,
    TenantInstance,
)
from app.modules.ops.schemas import (
    COMMIT_SHA_RE,
    UPGRADE_STATUSES,
    AgentHeartbeatIn,
    InstanceCreate,
    InstanceUpdate,
    NodeEnrollIn,
    TelemetryPayload,
)

ENROLL_TOKEN_TTL_MINUTES = 30
OFFLINE_AFTER_SECONDS = 90
METRIC_RETENTION_DAYS = 7
ALERT_REMIND_SECONDS = 30 * 60
# Agent 任务租约：领取/心跳后 10 分钟内有效，过期失联标记 unknown，不自动重跑
AGENT_LEASE_MINUTES = 10
# 升级类任务：创建时校验目标版本已登记且 commit 合法，执行后按校验结果判定成功
UPGRADE_JOB_TYPES = ("deploy", "upgrade")
# 升级校验项：数据库版本 / 服务就绪 / 业务只读
UPGRADE_CHECK_NAMES = ("db_version", "readiness", "business_readonly")

# 实例部署模式与迁移状态（批次2：实例隔离与租户模式边界）
INSTANCE_DEPLOYMENT_MODES = ("shared", "independent")
INSTANCE_MIGRATION_STATUSES = ("registered", "migrating", "migrated", "rollback")
# 已独立部署 / 迁移中 / 已迁出 / 回滚中：不再参与旧中心调度，也不能普通启用恢复写入
BLOCKED_DEPLOYMENT_MODES = frozenset({"independent"})
BLOCKED_MIGRATION_STATUSES = frozenset({"migrating", "migrated", "rollback"})
MIGRATION_WRITE_BLOCKED_MESSAGE = (
    "该租户实例已独立部署或处于迁移状态，请通过迁移流程操作，不能用普通启用恢复写入"
)

ALERT_CPU = "cpu_high"
ALERT_MEMORY = "memory_high"
ALERT_DISK = "disk_high"
ALERT_AGENT = "agent_offline"
ALERT_PUBLIC = "public_unreachable"
ALERT_CERT = "cert_expiring"
ALERT_BACKUP = "backup_overdue"

TARGETS = (("root", "/"), ("healthz", "/healthz"))


def _now() -> datetime:
    return utcnow()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat() + "Z"


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").strip().encode("utf-8")).hexdigest()


def is_full_commit(value: str | None) -> bool:
    """是否为完整 Git 提交号（40 或 64 位十六进制）。"""
    return bool(COMMIT_SHA_RE.fullmatch((value or "").strip()))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _upgrade_status(value: str | None, default: str = "unknown") -> str:
    normalized = (value or "").strip()
    return normalized if normalized in UPGRADE_STATUSES else default


def _check_ok(value: Any) -> bool:
    """校验项布尔值归一化：兼容 true/false、"succeeded"/"failed" 等写法。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"failed", "fail", "false", "error", "err", "0", "no", "down"}:
            return False
        if normalized in {"succeeded", "success", "ok", "passed", "pass", "true", "1", "yes", "up"}:
            return True
        return bool(normalized)
    return bool(value)


def normalize_upgrade_checks(raw: Any) -> list[dict[str, Any]]:
    """把 Agent 上报的校验结果规范化为 [{name, ok, value, message}]。

    支持两种形态：
    ``{"db_version": {"ok": true, "value": "tenant_0011"}, "readiness": {"ok": true}}``
    或 ``[{"name": "db_version", "ok": true, "value": "..."}]``；
    也兼容旧 Agent 的 ``{"commit_verified": true, "build": "succeeded"}``。
    """
    items: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            if isinstance(value, dict):
                ok = _check_ok(value.get("ok", value.get("passed", False)))
                item_value = str(value.get("value", "") or "")[:64]
                message = str(value.get("message", "") or "")[:200]
            else:
                ok = _check_ok(value)
                item_value = str(value)[:64] if not isinstance(value, bool) else ""
                message = ""
            items.append({"name": str(name)[:32], "ok": ok, "value": item_value, "message": message})
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("key") or "").strip()
            if not name:
                continue
            items.append(
                {
                    "name": name[:32],
                    "ok": _check_ok(entry.get("ok", False)),
                    "value": str(entry.get("value", "") or "")[:64],
                    "message": str(entry.get("message", "") or "")[:200],
                }
            )
    return items


def evaluate_upgrade_checks(checks: list[dict[str, Any]] | None) -> str:
    """校验结论：passed / failed / partial / missing。

    * 三项标准校验（db_version / readiness / business_readonly）齐全且全部通过 → passed；
    * 标准校验缺项 → partial；任一项失败 → failed；
    * 只有旧式校验项（如 commit_verified/build/services_ready）全部通过 → partial；
    * 完全没有校验结果 → missing（旧 Agent 兼容，任务仍可成功但标记未完整校验）。
    """
    items = checks or []
    if not items:
        return "missing"
    required = [item for item in items if item["name"] in UPGRADE_CHECK_NAMES]
    if not required:
        return "failed" if any(not item["ok"] for item in items) else "partial"
    if any(not item["ok"] for item in required):
        return "failed"
    missing = [name for name in UPGRADE_CHECK_NAMES if not any(item["name"] == name for item in required)]
    return "partial" if missing else "passed"


def failed_check_names(checks: list[dict[str, Any]] | None) -> list[str]:
    return [item["name"] for item in (checks or []) if not item["ok"]]


def upgrade_phase_from_step(step_name: str) -> str | None:
    """把 Agent 步骤名映射为升级阶段（新协议英文阶段名 + 旧 Agent 中文步骤名）。"""
    name = (step_name or "").strip()
    if name in UPGRADE_STATUSES:
        return name if name not in TERMINAL_JOB_STATUSES else None
    if any(keyword in name for keyword in ("构建", "准备版本")):
        return "building"
    if "启动服务" in name:
        return "maintenance"
    if any(keyword in name for keyword in ("就绪", "切换当前版本", "数据库迁移")):
        return "verifying"
    if "迁移" in name:
        return "migrating"
    return None


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def effective_node_status(node: ServerNode, now: datetime | None = None) -> str:
    if node.status == "pending" and node.enrolled_at is None:
        return "pending"
    if node.last_seen_at is None:
        return "unknown"
    current = now or _now()
    if current - node.last_seen_at > timedelta(seconds=OFFLINE_AFTER_SECONDS):
        return "offline"
    return "online"


def _latest_metric(center, node_id: int) -> NodeMetricSample | None:
    return center.execute(
        select(NodeMetricSample)
        .where(NodeMetricSample.node_id == node_id)
        .order_by(NodeMetricSample.sampled_at.desc(), NodeMetricSample.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _metric_payload(row: NodeMetricSample | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "sampled_at": _iso(row.sampled_at),
        "received_at": _iso(row.received_at),
        "cpu_percent": float(row.cpu_percent or 0),
        "load1": float(row.load1 or 0),
        "load5": float(row.load5 or 0),
        "load15": float(row.load15 or 0),
        "memory_total_mb": int(row.memory_total_mb or 0),
        "memory_used_mb": int(row.memory_used_mb or 0),
        "memory_available_mb": int(row.memory_available_mb or 0),
        "memory_percent": float(row.memory_percent or 0),
        "swap_total_mb": int(row.swap_total_mb or 0),
        "swap_used_mb": int(row.swap_used_mb or 0),
        "disks": row.disks or [],
        "network": row.network or {},
        "services": row.services or {},
        "public_ip": row.public_ip or "",
    }


def node_payload(node: ServerNode, *, metric: NodeMetricSample | None = None) -> dict[str, Any]:
    latest = metric if metric is not None else None
    return {
        "id": str(node.id),
        "instance_id": str(node.instance_id),
        "tenant_id": str(node.tenant_id),
        "node_uuid": node.node_uuid,
        "hostname": node.hostname,
        "os_name": node.os_name,
        "arch": node.arch,
        "cpu_cores": int(node.cpu_cores or 0),
        "memory_total_mb": int(node.memory_total_mb or 0),
        "disk_total_gb": int(node.disk_total_gb or 0),
        "internal_ips": node.internal_ips or [],
        "configured_public_ip": node.configured_public_ip,
        "observed_public_ip": node.observed_public_ip,
        "agent_version": node.agent_version or "",
        "executor_version": node.executor_version or "",
        "protocol_version": _safe_int(node.protocol_version),
        "capabilities": node.capabilities_json or [],
        "app_version": node.app_version or "",
        "actual_version": node.app_version or "",
        "db_version": node.db_version or "",
        "status": effective_node_status(node),
        "enrolled_at": _iso(node.enrolled_at),
        "last_seen_at": _iso(node.last_seen_at),
        "last_heartbeat_at": _iso(node.last_heartbeat_at),
        "last_metrics_at": _iso(node.last_metrics_at),
        "ssh_host": node.ssh_host or "",
        "ssh_host_fingerprint": node.ssh_host_fingerprint or "",
        "ssh_fingerprint_algorithm": node.ssh_fingerprint_algorithm or "",
        "ssh_host_fingerprint_verified_at": _iso(node.ssh_host_fingerprint_verified_at),
        "latest_metric": _metric_payload(latest),
    }


def instance_payload(center, instance: TenantInstance, tenant_name: str = "") -> dict[str, Any]:
    nodes = center.execute(
        select(ServerNode).where(ServerNode.instance_id == instance.id).order_by(ServerNode.id.asc())
    ).scalars().all()
    node_payloads = [node_payload(node, metric=_latest_metric(center, node.id)) for node in nodes]
    online = sum(1 for node in node_payloads if node["status"] == "online")
    open_alerts = int(
        center.execute(
            select(func.count())
            .select_from(AlertEvent)
            .where(AlertEvent.instance_id == instance.id, AlertEvent.status == "open")
        ).scalar_one()
    )
    primary_node = next((node for node in nodes if node.app_version), nodes[0] if nodes else None)
    app_version = instance.app_version or (primary_node.app_version if primary_node else "")
    actual_version = instance.actual_version or app_version
    agent_version = instance.agent_version or (primary_node.agent_version if primary_node else "")
    executor_version = instance.executor_version or (
        primary_node.executor_version if primary_node else ""
    )
    db_version = instance.db_version or (primary_node.db_version if primary_node else "")
    return {
        "id": str(instance.id),
        "tenant_id": str(instance.tenant_id),
        "tenant_name": tenant_name,
        "name": instance.name,
        "domain": instance.domain,
        "environment": instance.environment,
        "status": instance.status,
        "deployment_mode": normalize_deployment_mode(instance.deployment_mode),
        "migration_status": normalize_migration_status(instance.migration_status),
        "write_blocked": instance_write_blocked(instance),
        "app_version": app_version,
        "actual_version": actual_version,
        "target_version": instance.target_version or "",
        "agent_version": agent_version,
        "executor_version": executor_version,
        "db_version": db_version,
        "upgrade_status": _upgrade_status(instance.upgrade_status),
        "notes": instance.notes,
        "created_at": _iso(instance.created_at),
        "node_count": len(nodes),
        "online_node_count": online,
        "open_alert_count": open_alerts,
        "nodes": node_payloads,
    }


def normalize_deployment_mode(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in INSTANCE_DEPLOYMENT_MODES else "shared"


def normalize_migration_status(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in INSTANCE_MIGRATION_STATUSES else "registered"


def instance_write_blocked(instance: TenantInstance | None) -> bool:
    """实例是否禁止普通写入：独立部署 / 迁移中 / 已迁出 / 回滚中。"""
    if instance is None:
        return False
    mode = normalize_deployment_mode(instance.deployment_mode)
    migration = normalize_migration_status(instance.migration_status)
    return mode in BLOCKED_DEPLOYMENT_MODES or migration in BLOCKED_MIGRATION_STATUSES


def get_instance_by_tenant(center, tenant_id: int) -> TenantInstance | None:
    return center.execute(
        select(TenantInstance).where(TenantInstance.tenant_id == int(tenant_id))
    ).scalar_one_or_none()


def tenant_scheduling_blocked(center, tenant_id: int) -> bool:
    """租户是否应停止旧中心调度（迁移中 / 已迁出 / 独立部署）。"""
    return instance_write_blocked(get_instance_by_tenant(center, tenant_id))


def ensure_tenant_writable(center, tenant_id: int) -> None:
    """租户处于迁移流程时拒绝普通启用 / 重新开通，只允许迁移流程操作。"""
    if tenant_scheduling_blocked(center, tenant_id):
        raise ConflictError(MIGRATION_WRITE_BLOCKED_MESSAGE)


def list_instances(
    center,
    *,
    page: int,
    page_size: int,
    q: str = "",
    status: str = "",
    deployment_mode: str = "",
    migration_status: str = "",
) -> dict[str, Any]:
    from app.modules.identity.models_center import Tenant

    stmt = select(TenantInstance, Tenant.name).join(
        Tenant, Tenant.id == TenantInstance.tenant_id, isouter=True
    )
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(TenantInstance.name.like(pattern), TenantInstance.domain.like(pattern)))
    if status:
        stmt = stmt.where(TenantInstance.status == status)
    if deployment_mode:
        stmt = stmt.where(
            TenantInstance.deployment_mode == normalize_deployment_mode(deployment_mode)
        )
    if migration_status:
        stmt = stmt.where(
            TenantInstance.migration_status == normalize_migration_status(migration_status)
        )
    total = int(center.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = center.execute(
        stmt.order_by(TenantInstance.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [instance_payload(center, instance, tenant_name or "") for instance, tenant_name in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_instance(center, instance_id: int) -> TenantInstance:
    row = center.get(TenantInstance, int(instance_id))
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "实例不存在")
    return row


def create_instance(center, payload: InstanceCreate, *, actor_user_id: int) -> dict[str, Any]:
    from app.modules.identity.models_center import Tenant
    from app.modules.platform.service import record_platform_audit

    tenant = center.get(Tenant, int(payload.tenant_id))
    if tenant is None:
        raise AppError(ErrorCode.NOT_FOUND, "租户不存在")
    if center.execute(
        select(TenantInstance).where(TenantInstance.tenant_id == int(payload.tenant_id))
    ).scalar_one_or_none():
        raise ConflictError("该租户已登记实例，一期每个租户只允许一个生产实例")
    if center.execute(
        select(TenantInstance).where(TenantInstance.domain == payload.domain)
    ).scalar_one_or_none():
        raise ConflictError("该域名已绑定其他实例")
    now = _now()
    row = TenantInstance(
        tenant_id=int(payload.tenant_id),
        name=payload.name,
        domain=payload.domain,
        environment=payload.environment,
        status="pending",
        notes=payload.notes,
        created_at=now,
        updated_at=now,
    )
    center.add(row)
    center.flush()
    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action="instance.create",
        resource_type="tenant_instance",
        resource_id=row.id,
        tenant_id=row.tenant_id,
        summary=f"登记租户实例：{row.name}（{row.domain}）",
        payload={"domain": row.domain, "tenant_id": row.tenant_id},
    )
    center.commit()
    return instance_payload(center, row, tenant.name if tenant else "")


def update_instance(
    center, instance_id: int, payload: InstanceUpdate, *, actor_user_id: int
) -> dict[str, Any]:
    from app.modules.platform.service import record_platform_audit

    row = get_instance(center, instance_id)
    changes: list[str] = []
    if payload.domain and payload.domain != row.domain:
        if center.execute(
            select(TenantInstance).where(
                TenantInstance.domain == payload.domain, TenantInstance.id != row.id
            )
        ).scalar_one_or_none():
            raise ConflictError("该域名已绑定其他实例")
        row.domain = payload.domain
        changes.append("domain")
    if payload.name is not None and payload.name != row.name:
        row.name = payload.name
        changes.append("name")
    if payload.deployment_mode is not None and payload.deployment_mode != normalize_deployment_mode(
        row.deployment_mode
    ):
        row.deployment_mode = payload.deployment_mode
        changes.append("deployment_mode")
    if payload.migration_status is not None and payload.migration_status != normalize_migration_status(
        row.migration_status
    ):
        row.migration_status = payload.migration_status
        changes.append("migration_status")
    if payload.status is not None and payload.status != row.status:
        if payload.status == "active" and instance_write_blocked(row):
            raise ConflictError(MIGRATION_WRITE_BLOCKED_MESSAGE)
        row.status = payload.status
        changes.append("status")
    if payload.app_version is not None and payload.app_version != row.app_version:
        row.app_version = payload.app_version
        changes.append("app_version")
    if payload.notes is not None and payload.notes != row.notes:
        row.notes = payload.notes
        changes.append("notes")
    if changes:
        row.updated_at = _now()
        record_platform_audit(
            center,
            actor_user_id=actor_user_id,
            action="instance.update",
            resource_type="tenant_instance",
            resource_id=row.id,
            tenant_id=row.tenant_id,
            summary=f"更新租户实例：{row.name}",
            payload={
                "fields": changes,
                "deployment_mode": normalize_deployment_mode(row.deployment_mode),
                "migration_status": normalize_migration_status(row.migration_status),
            },
        )
    center.commit()
    return instance_payload(center, row)


def create_node_enrollment(center, instance_id: int, *, actor_user_id: int) -> dict[str, Any]:
    from app.modules.platform.service import record_platform_audit

    instance = get_instance(center, instance_id)
    now = _now()
    node_uuid = str(uuid.uuid4())
    enrollment_token = secrets.token_urlsafe(32)
    node = ServerNode(
        instance_id=instance.id,
        tenant_id=instance.tenant_id,
        node_uuid=node_uuid,
        status="pending",
        token_hash=hash_token(enrollment_token),
        enrollment_expires_at=now + timedelta(minutes=ENROLL_TOKEN_TTL_MINUTES),
        created_at=now,
        updated_at=now,
    )
    center.add(node)
    center.flush()
    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action="instance.node.enroll_token",
        resource_type="server_node",
        resource_id=node.id,
        tenant_id=instance.tenant_id,
        summary=f"生成服务器接入令牌：{instance.name}",
    )
    center.commit()
    return {
        "node": node_payload(node),
        "node_uuid": node_uuid,
        "enrollment_token": enrollment_token,
        "expires_at": _iso(node.enrollment_expires_at),
        "expires_in": ENROLL_TOKEN_TTL_MINUTES * 60,
        "install_command": (
            "sudo python3 /opt/erp-agent/erp_agent.py enroll "
            f"--center https://erp.alienyan.store --token {enrollment_token} "
            f"--instance {instance.id}"
        ),
    }


def list_nodes(center, instance_id: int) -> list[dict[str, Any]]:
    get_instance(center, instance_id)
    rows = center.execute(
        select(ServerNode).where(ServerNode.instance_id == instance_id).order_by(ServerNode.id.asc())
    ).scalars().all()
    return [node_payload(node, metric=_latest_metric(center, node.id)) for node in rows]


def _node_from_token(center, token: str) -> ServerNode:
    row = center.execute(
        select(ServerNode).where(ServerNode.token_hash == hash_token(token))
    ).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.AUTH_SESSION_REQUIRED, "节点令牌无效")
    return row


def enroll_node(center, payload: NodeEnrollIn) -> dict[str, Any]:
    node = center.execute(
        select(ServerNode)
        .where(ServerNode.token_hash == hash_token(payload.token))
        .with_for_update()
    ).scalar_one_or_none()
    if node is None:
        raise AppError(ErrorCode.NOT_FOUND, "接入令牌无效")
    now = _now()
    if node.enrollment_expires_at is None or node.enrollment_expires_at < now:
        raise AppError(ErrorCode.VALIDATION_ERROR, "接入令牌已过期，请在后台重新生成")
    node_token = secrets.token_urlsafe(32)
    node.hostname = payload.hostname
    node.os_name = payload.os_name
    node.arch = payload.arch
    node.cpu_cores = payload.cpu_cores
    node.memory_total_mb = payload.memory_total_mb
    node.disk_total_gb = payload.disk_total_gb
    node.internal_ips = payload.internal_ips
    node.observed_public_ip = payload.public_ip
    node.agent_version = payload.agent_version
    node.executor_version = payload.executor_version
    node.protocol_version = _safe_int(payload.protocol_version)
    node.capabilities_json = payload.capabilities or None
    node.app_version = payload.app_version
    node.metadata_json = payload.metadata
    node.status = "online"
    node.enrolled_at = now
    node.last_seen_at = now
    node.token_hash = hash_token(node_token)
    node.token_version += 1
    node.enrollment_expires_at = None
    node.updated_at = now
    center.commit()
    return {
        "node_id": str(node.id),
        "node_uuid": node.node_uuid,
        "node_token": node_token,
        "instance_id": str(node.instance_id),
        "tenant_id": str(node.tenant_id),
    }


def _upsert_alert(
    center,
    node: ServerNode,
    alert_type: str,
    severity: str,
    message: str,
    value: str,
    threshold: str,
    now: datetime,
) -> None:
    row = center.execute(
        select(AlertEvent).where(
            AlertEvent.node_id == node.id,
            AlertEvent.alert_type == alert_type,
            AlertEvent.status.in_(("open", "acknowledged")),
        )
    ).scalar_one_or_none()
    if row is None:
        center.add(
            AlertEvent(
                node_id=node.id,
                instance_id=node.instance_id,
                alert_type=alert_type,
                severity=severity,
                status="open",
                message=message[:500],
                metric_value=value[:64],
                threshold=threshold[:64],
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        row.last_seen_at = now
        row.message = message[:500]
        row.metric_value = value[:64]
        row.threshold = threshold[:64]
        row.severity = severity
        row.updated_at = now


def _resolve_alerts(center, node: ServerNode, alert_types: Iterable[str], now: datetime) -> None:
    rows = center.execute(
        select(AlertEvent).where(
            AlertEvent.node_id == node.id,
            AlertEvent.alert_type.in_(tuple(alert_types)),
            AlertEvent.status.in_(("open", "acknowledged")),
        )
    ).scalars().all()
    for row in rows:
        row.status = "resolved"
        row.resolved_at = now
        row.updated_at = now


def _sustained(center, node_id: int, column, threshold: float, seconds: int, minimum: int) -> bool:
    since = _now() - timedelta(seconds=seconds)
    values = center.execute(
        select(column).where(NodeMetricSample.node_id == node_id, NodeMetricSample.sampled_at >= since)
    ).scalars().all()
    hits = sum(1 for value in values if float(value or 0) >= threshold)
    return len(values) >= minimum and hits >= minimum


def ingest_telemetry(center, node: ServerNode, payload: TelemetryPayload) -> dict[str, Any]:
    now = _now()
    sampled_at = _aware_utc(payload.sampled_at) or now
    node.hostname = payload.hostname or node.hostname
    node.os_name = payload.os_name or node.os_name
    node.arch = payload.arch or node.arch
    node.cpu_cores = payload.cpu_cores or node.cpu_cores
    node.memory_total_mb = payload.memory_total_mb or node.memory_total_mb
    node.disk_total_gb = payload.disk_total_gb or node.disk_total_gb
    if payload.internal_ips:
        node.internal_ips = payload.internal_ips
    if payload.public_ip:
        node.observed_public_ip = payload.public_ip
    node.agent_version = payload.agent_version or node.agent_version
    node.executor_version = payload.executor_version or node.executor_version
    if payload.protocol_version:
        node.protocol_version = _safe_int(payload.protocol_version)
    if payload.capabilities:
        node.capabilities_json = payload.capabilities
    node.db_version = payload.db_version or node.db_version
    node.app_version = payload.app_version or node.app_version
    if payload.app_version or payload.db_version or payload.protocol_version:
        instance = center.get(TenantInstance, node.instance_id)
        if instance is not None:
            if payload.app_version and instance.actual_version != payload.app_version:
                instance.actual_version = payload.app_version
            if payload.app_version and not instance.app_version:
                instance.app_version = payload.app_version
            if payload.db_version:
                instance.db_version = payload.db_version
            if payload.agent_version:
                instance.agent_version = payload.agent_version
            if payload.executor_version:
                instance.executor_version = payload.executor_version
            instance.updated_at = now
    node.status = "online"
    node.last_seen_at = now
    node.last_metrics_at = now
    node.updated_at = now
    instance = center.get(TenantInstance, node.instance_id)
    if instance is not None and instance.status == "pending":
        instance.status = "active"
        instance.updated_at = now
    sample = NodeMetricSample(
        node_id=node.id,
        sampled_at=sampled_at,
        received_at=now,
        cpu_percent=payload.cpu_percent,
        load1=payload.load1,
        load5=payload.load5,
        load15=payload.load15,
        memory_total_mb=payload.memory_total_mb,
        memory_used_mb=payload.memory_used_mb,
        memory_available_mb=payload.memory_available_mb,
        memory_percent=payload.memory_percent,
        swap_total_mb=payload.swap_total_mb,
        swap_used_mb=payload.swap_used_mb,
        disks=payload.disks,
        network=payload.network,
        services=payload.services,
        public_ip=payload.public_ip,
    )
    center.add(sample)

    _resolve_alerts(center, node, (ALERT_AGENT,), now)
    # CPU 85% 持续 10 分钟；内存 90% 持续 5 分钟；样本不足时不告警。
    if _sustained(center, node.id, NodeMetricSample.cpu_percent, 85, 10 * 60, 20):
        _upsert_alert(
            center,
            node,
            ALERT_CPU,
            "warning" if payload.cpu_percent < 95 else "critical",
            f"CPU 使用率持续过高：{payload.cpu_percent:.1f}%",
            f"{payload.cpu_percent:.1f}%",
            "85% / 10分钟",
            now,
        )
    elif payload.cpu_percent < 85:
        _resolve_alerts(center, node, (ALERT_CPU,), now)
    if _sustained(center, node.id, NodeMetricSample.memory_percent, 90, 5 * 60, 10):
        _upsert_alert(
            center,
            node,
            ALERT_MEMORY,
            "critical",
            f"内存使用率持续过高：{payload.memory_percent:.1f}%",
            f"{payload.memory_percent:.1f}%",
            "90% / 5分钟",
            now,
        )
    elif payload.memory_percent < 90:
        _resolve_alerts(center, node, (ALERT_MEMORY,), now)

    max_disk = 0.0
    for disk in payload.disks:
        try:
            max_disk = max(max_disk, float(disk.get("percent") or 0))
        except (TypeError, ValueError):
            continue
    if max_disk >= 95:
        _upsert_alert(
            center, node, ALERT_DISK, "critical",
            f"磁盘使用率过高：{max_disk:.1f}%", f"{max_disk:.1f}%", "95%", now,
        )
    elif max_disk >= 85:
        _upsert_alert(
            center, node, ALERT_DISK, "warning",
            f"磁盘使用率预警：{max_disk:.1f}%", f"{max_disk:.1f}%", "85%", now,
        )
    else:
        _resolve_alerts(center, node, (ALERT_DISK,), now)

    center.commit()
    return {
        "ok": True,
        "node_id": str(node.id),
        "next_report_seconds": 30,
        "server_time": _iso(now),
    }


def mark_stale_nodes_offline(center) -> int:
    now = _now()
    rows = center.execute(
        select(ServerNode).where(
            ServerNode.status == "online",
            or_(ServerNode.last_seen_at.is_(None), ServerNode.last_seen_at < now - timedelta(seconds=OFFLINE_AFTER_SECONDS)),
        )
    ).scalars().all()
    for node in rows:
        node.status = "offline"
        node.updated_at = now
        _upsert_alert(
            center, node, ALERT_AGENT, "critical",
            "Agent 超过 90 秒未上报", "offline", "90s", now,
        )
    center.commit()
    return len(rows)


def _cert_days_left(domain: str) -> int | None:
    import ssl
    import socket

    try:
        context = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=6) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        not_after = cert.get("notAfter")
        if not not_after:
            return None
        import time as _time

        expires = ssl.cert_time_to_seconds(not_after)
        return int((expires - _time.time()) // 86400)
    except (OSError, ssl.SSLError, ValueError):
        return None


def probe_node(center, node_id: int) -> dict[str, Any]:
    node = center.get(ServerNode, int(node_id))
    if node is None:
        raise AppError(ErrorCode.NOT_FOUND, "节点不存在")
    instance = get_instance(center, node.instance_id)
    cert_days = _cert_days_left(instance.domain)
    checks: list[NodeCheck] = []
    for target, path in TARGETS:
        url = f"https://{instance.domain}{path}"
        started = _now()
        ok = False
        status_code: int | None = None
        error = ""
        try:
            response = httpx.get(url, timeout=8.0, follow_redirects=False)
            status_code = response.status_code
            ok = 200 <= response.status_code < 400
            if not ok:
                error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            error = str(exc)[:255]
        latency = int((_now() - started).total_seconds() * 1000)
        row = NodeCheck(
            node_id=node.id,
            checked_at=started,
            target=target,
            url=url,
            ok=ok,
            status_code=status_code,
            latency_ms=latency,
            cert_days_left=cert_days,
            error=error,
        )
        center.add(row)
        checks.append(row)
    if cert_days is not None and cert_days < 14:
        _upsert_alert(center, node, ALERT_CERT, "critical" if cert_days < 7 else "warning",
                      f"HTTPS 证书剩余 {cert_days} 天", str(cert_days), "14天", _now())
    elif cert_days is not None:
        _resolve_alerts(center, node, (ALERT_CERT,), _now())
    if all(item.ok for item in checks):
        _resolve_alerts(center, node, (ALERT_PUBLIC,), _now())
    else:
        recent = center.execute(
            select(NodeCheck.ok).where(NodeCheck.node_id == node.id).order_by(NodeCheck.id.desc()).limit(24)
        ).scalars().all()
        failures = 0
        for ok in recent:
            if ok:
                break
            failures += 1
        if failures >= 6:
            _upsert_alert(
                center, node, ALERT_PUBLIC, "critical",
                "公网访问连续 3 次探测失败", "failed", "连续3次", _now(),
            )
    center.commit()
    return {
        "node_id": str(node.id),
        "instance_id": str(instance.id),
        "domain": instance.domain,
        "checks": [
            {
                "target": item.target,
                "url": item.url,
                "ok": item.ok,
                "status_code": item.status_code,
                "latency_ms": item.latency_ms,
                "error": item.error,
                "checked_at": _iso(item.checked_at),
            }
            for item in checks
        ],
    }


def list_metrics(center, node_id: int, *, minutes: int = 60, limit: int = 500) -> dict[str, Any]:
    node = center.get(ServerNode, int(node_id))
    if node is None:
        raise AppError(ErrorCode.NOT_FOUND, "节点不存在")
    since = _now() - timedelta(minutes=max(1, min(minutes, 60 * 24 * 30)))
    rows = center.execute(
        select(NodeMetricSample)
        .where(NodeMetricSample.node_id == node.id, NodeMetricSample.sampled_at >= since)
        .order_by(NodeMetricSample.sampled_at.asc())
        .limit(max(1, min(limit, 2000)))
    ).scalars().all()
    return {
        "node_id": str(node.id),
        "minutes": minutes,
        "items": [_metric_payload(row) for row in rows],
        "latest": _metric_payload(_latest_metric(center, node.id)),
    }


def list_alerts(
    center, *, status: str = "", node_id: int | None = None, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    stmt = select(AlertEvent, ServerNode.hostname).join(
        ServerNode, ServerNode.id == AlertEvent.node_id, isouter=True
    )
    if status:
        stmt = stmt.where(AlertEvent.status == status)
    if node_id is not None:
        stmt = stmt.where(AlertEvent.node_id == int(node_id))
    total = int(center.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = center.execute(
        stmt.order_by(AlertEvent.last_seen_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": str(row.id),
                "node_id": str(row.node_id),
                "hostname": hostname or "",
                "instance_id": str(row.instance_id),
                "alert_type": row.alert_type,
                "severity": row.severity,
                "status": row.status,
                "message": row.message,
                "metric_value": row.metric_value,
                "threshold": row.threshold,
                "first_seen_at": _iso(row.first_seen_at),
                "last_seen_at": _iso(row.last_seen_at),
                "acknowledged_at": _iso(row.acknowledged_at),
                "resolved_at": _iso(row.resolved_at),
            }
            for row, hostname in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def acknowledge_alert(center, alert_id: int, *, note: str, actor_user_id: int) -> dict[str, Any]:
    from app.modules.platform.service import record_platform_audit

    row = center.get(AlertEvent, int(alert_id))
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "告警不存在")
    if row.status == "resolved":
        raise AppError(ErrorCode.VALIDATION_ERROR, "告警已恢复，无需确认")
    row.status = "acknowledged"
    row.acknowledged_at = _now()
    row.updated_at = _now()
    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action="alert.acknowledge",
        resource_type="alert_event",
        resource_id=row.id,
        tenant_id=None,
        summary=f"确认告警：{row.message}",
        payload={"note": note[:500]},
    )
    center.commit()
    return {"id": str(row.id), "status": row.status, "message": row.message}


def resolve_node_statuses(center) -> None:
    """把长期未上报的节点标为 offline，供调度器周期调用。"""
    mark_stale_nodes_offline(center)


_last_probe_at: datetime | None = None


def run_center_maintenance(center, *, probe_interval_seconds: int = 60) -> dict[str, int]:
    """调度进程周期调用：标记 Agent 失联并定时执行公网探测。"""
    global _last_probe_at
    now = _now()
    offline = mark_stale_nodes_offline(center)
    probed = 0
    failed = 0
    if _last_probe_at is None or (now - _last_probe_at).total_seconds() >= probe_interval_seconds:
        nodes = center.execute(
            select(ServerNode)
            .where(ServerNode.enrolled_at.is_not(None))
            .order_by(ServerNode.id.asc())
            .limit(100)
        ).scalars().all()
        for node in nodes:
            try:
                probe_node(center, node.id)
                probed += 1
            except Exception:  # noqa: BLE001 - 单节点探测失败不影响其他节点
                failed += 1
        _last_probe_at = now
    aggregate_metrics(center)
    mark_stale_operations(center)
    notifications = check_backup_alerts(center)
    notified = notify_open_alerts(center)
    cleaned = cleanup_old_monitoring_data(center)
    return {
        "offline": offline,
        "probed": probed,
        "probe_failed": failed,
        "notified": notified,
        "backup_alerts": notifications,
        "metrics_cleaned": cleaned["metrics"],
        "checks_cleaned": cleaned["checks"],
    }


_last_alert_email_at: datetime | None = None
_last_cleanup_at: datetime | None = None


def _platform_admin_emails(center) -> list[str]:
    from app.modules.identity.models_center import PlatformRole, PlatformUserRole, User

    rows = center.execute(
        select(User.email)
        .join(PlatformUserRole, PlatformUserRole.user_id == User.id)
        .join(PlatformRole, PlatformRole.id == PlatformUserRole.role_id)
        .where(PlatformRole.code == "platform_admin", User.status == "active")
        .distinct()
    ).all()
    return [str(row[0]) for row in rows if row[0]]


def _in_maintenance(center, instance_id: int, now: datetime | None = None) -> bool:
    from app.modules.ops.models_center import MaintenanceWindow

    moment = now or _now()
    return center.execute(
        select(MaintenanceWindow.id).where(
            MaintenanceWindow.instance_id == instance_id,
            MaintenanceWindow.starts_at <= moment,
            MaintenanceWindow.ends_at >= moment,
        ).limit(1)
    ).first() is not None


def notify_open_alerts(center) -> int:
    """对未通知或超过 30 分钟的未恢复告警发送邮件（尽力而为）。"""
    global _last_alert_email_at
    now = _now()
    if _last_alert_email_at is not None and (now - _last_alert_email_at).total_seconds() < 60:
        return 0
    _last_alert_email_at = now
    recipients = _platform_admin_emails(center)
    if not recipients:
        return 0
    rows = center.execute(
        select(AlertEvent).where(
            AlertEvent.status.in_(("open", "acknowledged")),
            or_(
                AlertEvent.last_notified_at.is_(None),
                AlertEvent.last_notified_at < now - timedelta(seconds=ALERT_REMIND_SECONDS),
            ),
        ).limit(20)
    ).scalars().all()
    if not rows:
        return 0
    rows = [row for row in rows if not _in_maintenance(center, row.instance_id, now)]
    if not rows:
        return 0
    from app.modules.email import service as email_service

    sent = 0
    for row in rows:
        try:
            email_service.send_default(
                center,
                to=recipients,
                subject=f"[ERP 运维告警][{row.severity}] {row.message[:80]}",
                text=(
                    f"告警类型：{row.alert_type}\n"
                    f"节点 ID：{row.node_id}\n"
                    f"当前值：{row.metric_value}\n"
                    f"阈值：{row.threshold}\n"
                    f"首次触发：{_iso(row.first_seen_at)}\n"
                    f"最近触发：{_iso(row.last_seen_at)}\n\n"
                    "请登录统一 ERP 管理中心 → 服务器与实例 → 告警中心处理。"
                ),
            )
        except Exception:  # noqa: BLE001 - 邮件服务未配置或临时失败不阻塞监控
            continue
        row.notify_count = int(row.notify_count or 0) + 1
        row.last_notified_at = now
        row.updated_at = now
        sent += 1
    if sent:
        center.commit()
    return sent


def cleanup_old_monitoring_data(center) -> dict[str, int]:
    """原始指标保留 7 天、探测保留 7 天；每天最多执行一次。"""
    global _last_cleanup_at
    now = _now()
    if _last_cleanup_at is not None and (now - _last_cleanup_at).total_seconds() < 86400:
        return {"metrics": 0, "checks": 0}
    cutoff = now - timedelta(days=METRIC_RETENTION_DAYS)
    metrics = center.query(NodeMetricSample).filter(NodeMetricSample.received_at < cutoff).delete(
        synchronize_session=False
    )
    checks = center.query(NodeCheck).filter(NodeCheck.checked_at < cutoff).delete(
        synchronize_session=False
    )
    center.commit()
    _last_cleanup_at = now
    return {"metrics": int(metrics or 0), "checks": int(checks or 0)}


def list_checks(center, node_id: int, *, limit: int = 20) -> dict[str, Any]:
    node = center.get(ServerNode, int(node_id))
    if node is None:
        raise AppError(ErrorCode.NOT_FOUND, "节点不存在")
    rows = center.execute(
        select(NodeCheck)
        .where(NodeCheck.node_id == node.id)
        .order_by(NodeCheck.id.desc())
        .limit(max(1, min(limit, 100)))
    ).scalars().all()
    return {
        "node_id": str(node.id),
        "items": [
            {
                "target": row.target,
                "url": row.url,
                "ok": row.ok,
                "status_code": row.status_code,
                "latency_ms": row.latency_ms,
                "error": row.error,
                "checked_at": _iso(row.checked_at),
            }
            for row in rows
        ],
    }


# --------------------------------------------------------------------- 阶段四：发布与运维任务
def _release_payload(row) -> dict[str, Any]:
    """发布版本响应结构（旧记录新字段返回空值/默认值，不会报错）。"""
    commit_sha = (row.commit_sha or "").strip().lower()
    return {
        "id": str(row.id),
        "version": row.version,
        "image_tag": row.image_tag or "",
        "image_digest": row.image_digest or "",
        "status": row.status,
        "commit_sha": commit_sha,
        "build_config_summary": row.build_config_summary or "",
        "min_agent_protocol": _safe_int(row.min_agent_protocol),
        "center_db_version": row.center_db_version or "",
        "tenant_db_version": row.tenant_db_version or "",
        "rollback_compatible": bool(row.rollback_compatible),
        "build_status": row.build_status or "built",
        "notes": row.notes or "",
        "created_at": _iso(row.created_at),
    }


def list_releases(center) -> list[dict[str, Any]]:
    from app.modules.ops.models_center import ReleaseVersion

    rows = center.execute(select(ReleaseVersion).order_by(ReleaseVersion.id.desc())).scalars().all()
    return [_release_payload(row) for row in rows]


def get_release_by_version(center, version: str):
    """按版本号读取发布版本记录（不存在返回 None）。"""
    from app.modules.ops.models_center import ReleaseVersion

    normalized = (version or "").strip()
    if not normalized:
        return None
    return center.execute(
        select(ReleaseVersion).where(ReleaseVersion.version == normalized)
    ).scalar_one_or_none()


def ensure_release(
    center,
    version: str,
    *,
    image_tag: str = "",
    notes: str = "",
    commit_sha: str = "",
    build_config_summary: str = "",
    min_agent_protocol: int = 0,
    center_db_version: str = "",
    tenant_db_version: str = "",
    rollback_compatible: bool = False,
    build_status: str = "built",
) -> bool:
    """按当前构建版本自动登记/补齐发布版本（启动时调用）。"""
    from app.modules.ops.models_center import ReleaseVersion

    version = (version or "").strip()
    if not version:
        return False
    commit_sha = (commit_sha or "").strip().lower()
    existing = get_release_by_version(center, version)
    if existing is not None:
        changed = False
        if commit_sha and existing.commit_sha != commit_sha:
            existing.commit_sha = commit_sha
            changed = True
        for field, value in (
            ("build_config_summary", build_config_summary),
            ("center_db_version", center_db_version),
            ("tenant_db_version", tenant_db_version),
        ):
            if value and getattr(existing, field) != value:
                setattr(existing, field, value)
                changed = True
        if min_agent_protocol and existing.min_agent_protocol != min_agent_protocol:
            existing.min_agent_protocol = min_agent_protocol
            changed = True
        if build_status and existing.build_status != build_status:
            existing.build_status = build_status
            changed = True
        if rollback_compatible and not existing.rollback_compatible:
            existing.rollback_compatible = True
            changed = True
        if changed:
            center.add(existing)
            center.commit()
        return False
    row = ReleaseVersion(
        version=version,
        image_tag=image_tag or version,
        image_digest="",
        status="active",
        commit_sha=commit_sha,
        build_config_summary=build_config_summary,
        min_agent_protocol=min_agent_protocol,
        center_db_version=center_db_version,
        tenant_db_version=tenant_db_version,
        rollback_compatible=rollback_compatible,
        build_status=build_status or "built",
        notes=notes or f"自动登记：当前部署版本 {version}",
        created_by=None,
    )
    center.add(row)
    center.commit()
    return True


def create_release(center, payload, *, actor_user_id: int) -> dict[str, Any]:
    """登记发布版本：只接受完整 Git commit（40/64 hex），并写平台审计。"""
    from app.modules.ops.models_center import ReleaseVersion
    from app.modules.platform.service import record_platform_audit

    commit_sha = (payload.commit_sha or "").strip().lower()
    if not is_full_commit(commit_sha):
        raise AppError(
            ErrorCode.VALIDATION_ERROR, "commit_sha 必须是完整的 40 或 64 位十六进制 Git 提交号"
        )
    if get_release_by_version(center, payload.version) is not None:
        raise ConflictError("版本号已存在")
    row = ReleaseVersion(
        version=payload.version,
        image_tag=payload.image_tag,
        image_digest=payload.image_digest,
        status=payload.status,
        commit_sha=commit_sha,
        build_config_summary=payload.build_config_summary,
        min_agent_protocol=_safe_int(payload.min_agent_protocol),
        center_db_version=payload.center_db_version,
        tenant_db_version=payload.tenant_db_version,
        rollback_compatible=bool(payload.rollback_compatible),
        build_status=payload.build_status,
        notes=payload.notes,
        created_by=actor_user_id,
    )
    center.add(row)
    center.flush()
    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action="release.create",
        resource_type="release_version",
        resource_id=row.id,
        summary=f"登记发布版本：{row.version}",
        payload={
            "version": row.version,
            "commit_sha": commit_sha,
            "build_status": row.build_status,
            "center_db_version": row.center_db_version,
            "tenant_db_version": row.tenant_db_version,
        },
    )
    center.commit()
    return _release_payload(row)


def update_release(center, release_id: int, payload, *, actor_user_id: int) -> dict[str, Any]:
    """更新发布版本（局部更新）：回填旧版本的完整 commit 与构建/数据库目标信息。

    仅允许修改元数据，不允许改版本号（版本号是升级目标的主键语义）；
    提供 ``commit_sha`` 时必须是 40/64 位十六进制；写平台审计 ``release.update``。
    """
    from app.modules.ops.models_center import ReleaseVersion
    from app.modules.platform.service import record_platform_audit

    row = center.get(ReleaseVersion, int(release_id))
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "发布版本不存在")

    changes: list[str] = []
    commit_sha = (payload.commit_sha or "").strip().lower() if payload.commit_sha is not None else None
    if commit_sha is not None:
        if not is_full_commit(commit_sha):
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "commit_sha 必须是完整的 40 或 64 位十六进制 Git 提交号",
            )
        if row.commit_sha != commit_sha:
            row.commit_sha = commit_sha
            changes.append("commit_sha")
    if payload.build_config_summary is not None and row.build_config_summary != payload.build_config_summary:
        row.build_config_summary = payload.build_config_summary
        changes.append("build_config_summary")
    if payload.min_agent_protocol is not None and _safe_int(row.min_agent_protocol) != _safe_int(
        payload.min_agent_protocol
    ):
        row.min_agent_protocol = _safe_int(payload.min_agent_protocol)
        changes.append("min_agent_protocol")
    if payload.center_db_version is not None and row.center_db_version != payload.center_db_version:
        row.center_db_version = payload.center_db_version
        changes.append("center_db_version")
    if payload.tenant_db_version is not None and row.tenant_db_version != payload.tenant_db_version:
        row.tenant_db_version = payload.tenant_db_version
        changes.append("tenant_db_version")
    if payload.rollback_compatible is not None and bool(row.rollback_compatible) != bool(
        payload.rollback_compatible
    ):
        row.rollback_compatible = bool(payload.rollback_compatible)
        changes.append("rollback_compatible")
    if payload.build_status is not None and row.build_status != payload.build_status:
        row.build_status = payload.build_status
        changes.append("build_status")
    if payload.status is not None and row.status != payload.status:
        row.status = payload.status
        changes.append("status")
    if payload.notes is not None and row.notes != payload.notes:
        row.notes = payload.notes
        changes.append("notes")

    if not changes:
        return _release_payload(row)
    row.updated_at = _now()
    center.add(row)
    center.flush()
    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action="release.update",
        resource_type="release_version",
        resource_id=row.id,
        summary=f"更新发布版本：{row.version}",
        payload={"version": row.version, "fields": changes, "commit_sha": row.commit_sha},
    )
    center.commit()
    return _release_payload(row)


def validate_upgrade_target(center, instance: TenantInstance, node: ServerNode | None, target_version: str):
    """校验升级目标版本：已登记、commit 完整、未退役、Agent 协议满足最低要求。

    返回发布版本行；失败抛 ``AppError``（参数问题 422 / 冲突 409）。
    """

    version = (target_version or "").strip()
    if not version:
        raise AppError(ErrorCode.VALIDATION_ERROR, "升级任务必须指定目标版本")
    release = get_release_by_version(center, version)
    if release is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"目标版本未登记：{version}")
    if not is_full_commit(release.commit_sha):
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"目标版本 {version} 未登记完整 Git 提交号，禁止升级",
        )
    if release.status == "retired":
        raise AppError(ErrorCode.VALIDATION_ERROR, f"目标版本 {version} 已退役，禁止升级")
    if (release.build_status or "").strip() != "verified":
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"目标版本 {version} 构建状态不是 verified，禁止升级",
        )
    node_protocol = _safe_int(node.protocol_version) if node is not None else 0
    required_protocol = _safe_int(release.min_agent_protocol)
    if required_protocol and node_protocol and node_protocol < required_protocol:
        raise ConflictError(
            f"节点 Agent 协议版本 {node_protocol} 低于目标版本要求 {required_protocol}，请先升级 Agent"
        )
    return release


def create_operation(center, instance_id: int, payload, *, actor_user_id: int) -> dict[str, Any]:
    from app.modules.ops.models_center import OperationJob
    from app.modules.platform.service import record_platform_audit

    instance = get_instance(center, instance_id)
    # 迁移中 / 已迁出 / 独立部署的实例不再参与旧中心运维任务调度
    if instance_write_blocked(instance):
        raise ConflictError(MIGRATION_WRITE_BLOCKED_MESSAGE)
    node = center.execute(
        select(ServerNode).where(ServerNode.instance_id == instance.id).order_by(ServerNode.id.asc())
    ).scalars().first()
    if node is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "实例尚未接入服务器节点")
    is_upgrade = payload.job_type in UPGRADE_JOB_TYPES
    release = None
    target_version = (payload.target_version or "").strip()
    key = payload.idempotency_key or f"{instance.id}:{payload.job_type}:{_now().isoformat()}:{secrets.token_hex(4)}"
    existing = center.execute(select(OperationJob).where(OperationJob.idempotency_key == key)).scalar_one_or_none()
    if existing is not None:
        # 幂等重放：直接返回既有任务，不因目标版本后续退役/改动而失败
        return _operation_payload(existing, center=center)
    if is_upgrade:
        release = validate_upgrade_target(center, instance, node, target_version)
    running = center.execute(
        select(OperationJob).where(
            OperationJob.instance_id == instance.id,
            OperationJob.status.in_(("pending", "leased", "running", "unknown")),
        )
    ).scalars().first()
    if running is not None:
        raise ConflictError("该实例已有未完成运维任务，请等待完成")
    now = _now()
    job_payload = dict(payload.payload or {})
    if is_upgrade and release is not None:
        # Agent 2 只接受完整 commit；创建任务时由中心从发布清单注入，避免客户端伪造。
        job_payload["commit"] = release.commit_sha
        job_payload["target_version"] = target_version
        job_payload["tenant_db_version"] = release.tenant_db_version
    row = OperationJob(
        instance_id=instance.id, node_id=node.id, job_type=payload.job_type,
        status="pending", idempotency_key=key, target_version=target_version,
        upgrade_status="pending" if is_upgrade else "",
        payload_json=job_payload, requested_by=actor_user_id,
        created_at=now, updated_at=now,
    )
    if is_upgrade:
        instance.target_version = target_version
        instance.upgrade_status = "pending"
        instance.updated_at = now
        center.add(instance)
    center.add(row)
    center.flush()
    record_platform_audit(
        center, actor_user_id=actor_user_id, action="operation.create",
        resource_type="operation_job", resource_id=row.id, tenant_id=instance.tenant_id,
        summary=f"创建运维任务：{payload.job_type}",
        payload={
            "instance_id": instance.id,
            "target_version": target_version,
            "release_commit": (release.commit_sha if release is not None else ""),
            "min_agent_protocol": (
                _safe_int(release.min_agent_protocol) if release is not None else 0
            ),
        },
    )
    center.commit()
    return _operation_payload(row, center=center)


def _operation_payload(row, center=None) -> dict[str, Any]:
    checks = row.checks_json or []
    data = {
        "id": str(row.id), "instance_id": str(row.instance_id), "node_id": str(row.node_id) if row.node_id else None,
        "job_type": row.job_type, "status": row.status, "target_version": row.target_version,
        "actual_version": row.actual_version or "",
        "agent_version": row.agent_version or "",
        "agent_protocol_version": _safe_int(row.agent_protocol_version),
        "db_version": row.db_version or "",
        "upgrade_status": _upgrade_status(row.upgrade_status, ""),
        "attempt_id": row.attempt_id or "",
        "lease_token_present": bool(row.lease_token),
        "lease_expires_at": _iso(row.lease_expires_at),
        "last_heartbeat_at": _iso(row.last_heartbeat_at),
        "event_seq": _safe_int(row.event_seq),
        "checks_status": row.checks_status or "",
        "checks": checks,
        "attempts": row.attempts, "error": row.error, "result": row.result_json,
        "created_at": _iso(row.created_at), "started_at": _iso(row.started_at), "finished_at": _iso(row.finished_at),
    }
    if center is not None:
        from app.modules.ops.models_center import OperationJobStep
        steps = center.execute(select(OperationJobStep).where(OperationJobStep.job_id == row.id).order_by(OperationJobStep.step_no)).scalars().all()
        data["steps"] = [{"step_no": s.step_no, "name": s.name, "status": s.status, "message": s.message} for s in steps]
    return data


def get_operation(center, job_id: int) -> dict[str, Any]:
    from app.modules.ops.models_center import OperationJob
    row = center.get(OperationJob, int(job_id))
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "运维任务不存在")
    return _operation_payload(row, center=center)


def list_operations(center, *, instance_id: int | None = None, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    from app.modules.ops.models_center import OperationJob
    stmt = select(OperationJob)
    if instance_id:
        stmt = stmt.where(OperationJob.instance_id == instance_id)
    total = int(center.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = center.execute(stmt.order_by(OperationJob.id.desc()).offset((page-1)*page_size).limit(page_size)).scalars().all()
    return {"items": [_operation_payload(r) for r in rows], "total": total, "page": page, "page_size": page_size}


def list_backups(center, *, instance_id: int | None = None, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    from app.modules.ops.models_center import BackupRecord
    stmt = select(BackupRecord)
    if instance_id:
        stmt = stmt.where(BackupRecord.instance_id == instance_id)
    total = int(center.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = center.execute(stmt.order_by(BackupRecord.id.desc()).offset((page-1)*page_size).limit(page_size)).scalars().all()
    return {"items": [{"id": str(r.id), "instance_id": str(r.instance_id), "status": r.status, "storage_key": r.storage_key,
                       "size_bytes": int(r.size_bytes or 0), "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
                       "error": r.error} for r in rows], "total": total, "page": page, "page_size": page_size}


TERMINAL_JOB_STATUSES = ("succeeded", "failed", "unknown")


def _validate_job_lease(row, attempt_id: str = "", lease_token: str = "") -> None:
    """校验执行批次/租约：旧 Agent 不传时跳过，新 Agent 传错时拒绝写入。"""
    if attempt_id and row.attempt_id and attempt_id != row.attempt_id:
        raise ConflictError("执行批次已失效，请重新领取任务")
    if lease_token and row.lease_token and lease_token != row.lease_token:
        raise ConflictError("执行租约已失效，请重新领取任务")


def _sync_instance_after_job(center, row, now: datetime) -> None:
    """把升级任务的版本/数据库版本/升级状态同步到实例展示字段。"""
    instance = center.get(TenantInstance, int(row.instance_id))
    if instance is None:
        return
    if row.job_type in UPGRADE_JOB_TYPES:
        instance.upgrade_status = _upgrade_status(
            row.upgrade_status, instance.upgrade_status or "unknown"
        )
        if row.actual_version:
            instance.actual_version = row.actual_version
            instance.app_version = row.actual_version
        if row.db_version:
            instance.db_version = row.db_version
        if row.agent_version:
            instance.agent_version = row.agent_version
        if row.status == "succeeded" and not instance.actual_version:
            instance.actual_version = row.target_version
        instance.updated_at = now
        center.add(instance)


def _apply_job_event(center, row, payload, now: datetime) -> None:
    """把一条 Agent 事件应用到任务（不提交事务）。"""
    from app.modules.ops.models_center import BackupRecord, OperationJobStep

    if payload.agent_protocol_version:
        row.agent_protocol_version = _safe_int(payload.agent_protocol_version)
    if payload.attempt_id and not row.attempt_id:
        row.attempt_id = payload.attempt_id
    if payload.step_no and payload.step_name:
        step = center.execute(
            select(OperationJobStep).where(
                OperationJobStep.job_id == row.id, OperationJobStep.step_no == payload.step_no
            )
        ).scalar_one_or_none()
        if step is None:
            step = OperationJobStep(
                job_id=row.id, step_no=payload.step_no, name=payload.step_name,
                status=payload.step_status or "running", message=payload.message, started_at=now,
            )
            center.add(step)
        else:
            step.status = payload.step_status or step.status
            step.message = payload.message or step.message
            step.finished_at = now if step.status in {"succeeded", "failed", "skipped"} else None
    # 校验结果（数据库版本 / 服务就绪 / 业务只读）；兼容旧 Agent 放在 result.checks
    result = payload.result if isinstance(payload.result, dict) else {}
    checks_source = payload.checks if payload.checks is not None else result.get("checks")
    if checks_source is not None:
        items = normalize_upgrade_checks(checks_source)
        if items:
            row.checks_json = items
            row.checks_status = evaluate_upgrade_checks(items)
            db_item = next(
                (item for item in items if item["name"] == "db_version" and item["value"]), None
            )
            if db_item is not None:
                row.db_version = db_item["value"]
    if result.get("actual_version"):
        row.actual_version = str(result["actual_version"])[:64]
    if result.get("agent_version"):
        row.agent_version = str(result["agent_version"])[:32]
    if result.get("db_version"):
        row.db_version = str(result["db_version"])[:64]
    # Agent 步骤名映射为升级阶段（新协议英文阶段名 / 旧 Agent 中文步骤名）
    phase = upgrade_phase_from_step(payload.step_name)
    if phase:
        row.upgrade_status = phase
    if payload.status in TERMINAL_JOB_STATUSES:
        if payload.status == "succeeded" and row.job_type in UPGRADE_JOB_TYPES:
            checks = row.checks_json or normalize_upgrade_checks(payload.checks)
            conclusion = evaluate_upgrade_checks(checks)
            row.checks_status = conclusion
            if conclusion == "failed":
                names = failed_check_names(checks)
                row.status = "failed"
                row.upgrade_status = "failed"
                row.error = ("升级校验未通过：" + "、".join(names))[:500]
                row.finished_at = now
                row.updated_at = now
                _sync_instance_after_job(center, row, now)
                return
        row.status = payload.status
        row.finished_at = now
        row.error = (payload.error or "")[:500]
        if payload.result is not None:
            row.result_json = payload.result
        if row.job_type in UPGRADE_JOB_TYPES:
            row.upgrade_status = {"succeeded": "succeeded", "failed": "failed"}.get(
                payload.status, "unknown"
            )
            if payload.status == "succeeded" and not row.actual_version:
                row.actual_version = row.target_version
        if row.job_type == "backup":
            center.add(
                BackupRecord(
                    instance_id=row.instance_id, node_id=row.node_id, job_id=row.id,
                    status="succeeded" if payload.status == "succeeded" else "failed",
                    storage_key=str(result.get("storage_key", "")),
                    size_bytes=int(result.get("size_bytes", 0) or 0),
                    started_at=row.started_at or now, finished_at=now,
                    retention_until=now + timedelta(days=30), error=(payload.error or "")[:500],
                )
            )
        row.updated_at = now
        _sync_instance_after_job(center, row, now)
        return
    row.status = "running"
    row.updated_at = now


def claim_agent_job(
    center,
    node: ServerNode,
    *,
    agent_version: str = "",
    executor_version: str = "",
    protocol_version: int = 0,
) -> dict[str, Any] | None:
    """Agent 领取任务：生成执行批次与租约，返回目标版本与租约凭证。

    * 先处理过期租约（标记 unknown，不自动重跑）；
    * 每个任务每次领取生成新的 ``attempt_id`` 与 ``lease_token``，旧批次事件会被拒绝；
    * 旧 Agent 不传版本字段时沿用节点已记录值。
    """
    from app.modules.ops.models_center import OperationJob

    now = _now()
    if agent_version:
        node.agent_version = agent_version
    if executor_version:
        node.executor_version = executor_version
    if protocol_version:
        node.protocol_version = _safe_int(protocol_version)
    if agent_version or executor_version or protocol_version:
        node.updated_at = now
    stale = center.execute(
        select(OperationJob).where(
            OperationJob.node_id == node.id,
            OperationJob.status.in_(("leased", "running")),
            OperationJob.lease_expires_at.is_not(None),
            OperationJob.lease_expires_at < now,
        )
    ).scalars().all()
    for item in stale:
        item.status = "unknown"
        item.error = "执行中失联，结果待核对"
        item.finished_at = now
        item.updated_at = now
        if item.job_type in UPGRADE_JOB_TYPES:
            item.upgrade_status = "unknown"
            _sync_instance_after_job(center, item, now)
    row = center.execute(
        select(OperationJob).where(
            OperationJob.node_id == node.id,
            OperationJob.status == "pending",
        ).order_by(OperationJob.id.asc()).with_for_update()
    ).scalars().first()
    if row is None:
        if stale or agent_version or executor_version or protocol_version:
            center.commit()
        return None
    row.status = "leased"
    row.attempts = int(row.attempts or 0) + 1
    row.attempt_id = str(uuid.uuid4())
    row.lease_token = secrets.token_urlsafe(32)
    row.lease_expires_at = now + timedelta(minutes=AGENT_LEASE_MINUTES)
    row.last_heartbeat_at = now
    row.started_at = row.started_at or now
    row.agent_version = agent_version or node.agent_version or row.agent_version
    protocol = _safe_int(protocol_version) or _safe_int(node.protocol_version)
    if protocol:
        row.agent_protocol_version = protocol
    if row.job_type in UPGRADE_JOB_TYPES and not row.upgrade_status:
        row.upgrade_status = "pending"
    row.updated_at = now
    center.commit()
    return {
        "id": str(row.id),
        "job_type": row.job_type,
        "target_version": row.target_version,
        "payload": row.payload_json or {},
        "attempts": row.attempts,
        "attempt_id": row.attempt_id,
        "lease_token": row.lease_token,
        "lease_expires_at": _iso(row.lease_expires_at),
        "agent_protocol_version": _safe_int(row.agent_protocol_version),
        "upgrade_status": row.upgrade_status or "",
    }


def poll_agent_job(center, node: ServerNode) -> dict[str, Any] | None:
    """兼容旧 Agent 的任务领取接口（等价于 ``claim_agent_job``）。"""
    return claim_agent_job(center, node)


def heartbeat_agent_job(
    center,
    node: ServerNode,
    job_id: int,
    *,
    attempt_id: str = "",
    lease_token: str = "",
    event_seq: int = 0,
    agent_protocol_version: int = 0,
) -> dict[str, Any]:
    """Agent 任务执行心跳：续租、记录心跳与事件水位。"""
    from app.modules.ops.models_center import OperationJob

    row = center.execute(
        select(OperationJob).where(OperationJob.id == int(job_id), OperationJob.node_id == node.id)
    ).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "任务不存在")
    now = _now()
    node.last_heartbeat_at = now
    node.updated_at = now
    if agent_protocol_version:
        row.agent_protocol_version = _safe_int(agent_protocol_version)
    if row.status in TERMINAL_JOB_STATUSES:
        center.commit()
        return {
            "ok": True,
            "job_id": str(row.id),
            "status": row.status,
            "upgrade_status": row.upgrade_status or "",
            "finished": True,
        }
    _validate_job_lease(row, attempt_id, lease_token)
    row.last_heartbeat_at = now
    row.lease_expires_at = now + timedelta(minutes=AGENT_LEASE_MINUTES)
    if event_seq:
        row.event_seq = max(_safe_int(row.event_seq), _safe_int(event_seq))
    row.updated_at = now
    center.commit()
    return {
        "ok": True,
        "job_id": str(row.id),
        "status": row.status,
        "upgrade_status": row.upgrade_status or "",
        "lease_expires_at": _iso(row.lease_expires_at),
        "event_seq": _safe_int(row.event_seq),
    }


def ingest_agent_heartbeat(center, node: ServerNode, payload: AgentHeartbeatIn) -> dict[str, Any]:
    """Agent 心跳：同步协议版本/能力/执行器版本/数据库版本，可选携带任务心跳。"""
    now = _now()
    if payload.agent_version:
        node.agent_version = payload.agent_version
    if payload.executor_version:
        node.executor_version = payload.executor_version
    if payload.protocol_version:
        node.protocol_version = _safe_int(payload.protocol_version)
    if payload.capabilities:
        node.capabilities_json = payload.capabilities
    if payload.db_version:
        node.db_version = payload.db_version
    if payload.app_version:
        node.app_version = payload.app_version
    node.status = "online"
    node.last_seen_at = now
    node.last_heartbeat_at = now
    node.updated_at = now
    instance = center.get(TenantInstance, node.instance_id)
    if instance is not None:
        if payload.app_version:
            instance.actual_version = payload.app_version
            instance.app_version = payload.app_version
        if payload.db_version:
            instance.db_version = payload.db_version
        if payload.agent_version:
            instance.agent_version = payload.agent_version
        if payload.executor_version:
            instance.executor_version = payload.executor_version
        instance.updated_at = now
        center.add(instance)
    job_info: dict[str, Any] | None = None
    if payload.job_id:
        job_info = heartbeat_agent_job(
            center,
            node,
            payload.job_id,
            attempt_id=payload.attempt_id,
            lease_token=payload.lease_token,
            event_seq=payload.event_seq,
            agent_protocol_version=payload.protocol_version,
        )
    else:
        center.commit()
    return {"ok": True, "server_time": _iso(now), "job": job_info}


def record_upgrade_checks(
    center, job_id: int, checks, *, status: str | None = None
) -> dict[str, Any]:
    """写入升级校验结果（数据库版本 / 服务就绪 / 业务只读），更新校验结论与数据库版本。

    供 Agent 事件或人工核对调用；结论为 failed 时调用方应将任务置 failed。
    """
    from app.modules.ops.models_center import OperationJob

    row = center.get(OperationJob, int(job_id))
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "运维任务不存在")
    items = normalize_upgrade_checks(checks)
    if not items:
        raise AppError(ErrorCode.VALIDATION_ERROR, "升级校验结果不能为空")
    row.checks_json = items
    row.checks_status = evaluate_upgrade_checks(items)
    db_item = next((item for item in items if item["name"] == "db_version" and item["value"]), None)
    if db_item is not None:
        row.db_version = db_item["value"]
    if status:
        if status not in ("pending", "leased", "running", "succeeded", "failed", "unknown"):
            raise AppError(ErrorCode.VALIDATION_ERROR, "任务状态不合法")
        row.status = status
        if status in TERMINAL_JOB_STATUSES:
            row.finished_at = _now()
    row.updated_at = _now()
    center.commit()
    return _operation_payload(row)


def append_agent_job_events(
    center,
    node: ServerNode,
    job_id: int,
    events,
    *,
    attempt_id: str = "",
    lease_token: str = "",
) -> dict[str, Any]:
    """批量写入 Agent 事件（按 event_seq 去重，断线重放不会重复推进结果）。"""
    from app.modules.ops.models_center import OperationJob

    row = center.execute(
        select(OperationJob).where(OperationJob.id == int(job_id), OperationJob.node_id == node.id)
    ).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "任务不存在")
    _validate_job_lease(row, attempt_id, lease_token)
    now = _now()
    applied = 0
    max_seq = _safe_int(row.event_seq)
    for event in events or []:
        seq = _safe_int(getattr(event, "event_seq", 0) or 0)
        if seq and seq <= max_seq:
            continue
        _apply_job_event(center, row, event, now)
        applied += 1
        if seq > max_seq:
            max_seq = seq
    row.event_seq = max_seq
    if row.status not in TERMINAL_JOB_STATUSES:
        row.last_heartbeat_at = now
        row.lease_expires_at = now + timedelta(minutes=AGENT_LEASE_MINUTES)
    row.updated_at = now
    center.commit()
    return {
        "ok": True,
        "applied": applied,
        "event_seq": max_seq,
        "status": row.status,
        "upgrade_status": row.upgrade_status or "",
        "checks_status": row.checks_status or "",
    }


def agent_job_event(center, node: ServerNode, job_id: int, payload) -> dict[str, Any]:
    """单条 Agent 任务事件（兼容旧 Agent；新 Agent 携带 attempt/lease/event_seq/checks）。"""
    from app.modules.ops.models_center import OperationJob

    row = center.execute(
        select(OperationJob).where(OperationJob.id == job_id, OperationJob.node_id == node.id)
    ).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "任务不存在")
    _validate_job_lease(row, getattr(payload, "attempt_id", ""), getattr(payload, "lease_token", ""))
    seq = _safe_int(getattr(payload, "event_seq", 0))
    if seq and seq <= _safe_int(row.event_seq):
        return {
            "ok": True,
            "duplicate": True,
            "event_seq": _safe_int(row.event_seq),
            "status": row.status,
            "upgrade_status": row.upgrade_status or "",
        }
    now = _now()
    _apply_job_event(center, row, payload, now)
    row.event_seq = max(_safe_int(row.event_seq), seq)
    if row.status not in TERMINAL_JOB_STATUSES:
        row.last_heartbeat_at = now
        row.lease_expires_at = now + timedelta(minutes=AGENT_LEASE_MINUTES)
    row.updated_at = now
    center.commit()
    return {
        "ok": True,
        "event_seq": _safe_int(row.event_seq),
        "status": row.status,
        "upgrade_status": row.upgrade_status or "",
        "checks_status": row.checks_status or "",
    }


def reconcile_agent_jobs(center, node: ServerNode, active_job_ids: list[int]) -> dict[str, Any]:
    """对账：Agent 上报本机没有的执行中任务 → 中心标记 unknown，等待人工核对。"""
    from app.modules.ops.models_center import OperationJob

    now = _now()
    active = {_safe_int(item) for item in (active_job_ids or [])}
    rows = center.execute(
        select(OperationJob).where(
            OperationJob.node_id == node.id,
            OperationJob.status.in_(("leased", "running")),
        )
    ).scalars().all()
    lost = [row for row in rows if _safe_int(row.id) not in active]
    for row in lost:
        row.status = "unknown"
        row.error = "Agent 对账时缺失该任务，结果待核对"
        row.finished_at = now
        row.updated_at = now
        if row.job_type in UPGRADE_JOB_TYPES:
            row.upgrade_status = "unknown"
            _sync_instance_after_job(center, row, now)
    node.last_heartbeat_at = now
    node.updated_at = now
    center.commit()
    return {
        "ok": True,
        "checked": len(rows),
        "marked_unknown": len(lost),
        "job_ids": [str(row.id) for row in lost],
    }


def list_maintenance_windows(center, instance_id: int) -> list[dict[str, Any]]:
    from app.modules.ops.models_center import MaintenanceWindow

    get_instance(center, instance_id)
    rows = center.execute(
        select(MaintenanceWindow)
        .where(MaintenanceWindow.instance_id == instance_id)
        .order_by(MaintenanceWindow.starts_at.desc())
    ).scalars().all()
    return [
        {"id": str(row.id), "instance_id": str(row.instance_id), "starts_at": _iso(row.starts_at),
         "ends_at": _iso(row.ends_at), "reason": row.reason}
        for row in rows
    ]


def create_maintenance_window(center, instance_id: int, payload, *, actor_user_id: int) -> dict[str, Any]:
    from app.modules.ops.models_center import MaintenanceWindow

    instance = get_instance(center, instance_id)
    starts = _aware_utc(payload.starts_at)
    ends = _aware_utc(payload.ends_at)
    if starts is None or ends is None or ends <= starts:
        raise AppError(ErrorCode.VALIDATION_ERROR, "结束时间必须晚于开始时间")
    row = MaintenanceWindow(instance_id=instance.id, starts_at=starts, ends_at=ends,
                            reason=payload.reason, created_by=actor_user_id)
    center.add(row); center.commit()
    return {"id": str(row.id), "instance_id": str(instance.id), "starts_at": _iso(starts),
            "ends_at": _iso(ends), "reason": row.reason}


def delete_maintenance_window(center, window_id: int) -> dict[str, Any]:
    from app.modules.ops.models_center import MaintenanceWindow

    row = center.get(MaintenanceWindow, int(window_id))
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "维护窗口不存在")
    center.delete(row); center.commit()
    return {"deleted": True}


def check_backup_alerts(center) -> int:
    """最近一次成功备份超过 26 小时或从未备份时告警。"""
    from app.modules.ops.models_center import BackupRecord

    now = _now()
    created = 0
    instances = center.execute(select(TenantInstance)).scalars().all()
    for instance in instances:
        nodes = center.execute(
            select(ServerNode).where(ServerNode.instance_id == instance.id)
        ).scalars().all()
        latest = center.execute(
            select(BackupRecord)
            .where(BackupRecord.instance_id == instance.id, BackupRecord.status == "succeeded")
            .order_by(BackupRecord.finished_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        overdue = latest is None or latest.finished_at is None or (now - latest.finished_at) > timedelta(hours=26)
        for node in nodes:
            if overdue:
                _upsert_alert(
                    center, node, ALERT_BACKUP, "warning",
                    "备份超过 26 小时未成功" if latest else "尚未完成首次备份",
                    "overdue", "26h", now,
                )
                created += 1
            else:
                _resolve_alerts(center, node, (ALERT_BACKUP,), now)
    if created:
        center.commit()
    else:
        center.commit()
    return created


_agg_last_at: datetime | None = None


def aggregate_metrics(center, *, force: bool = False) -> int:
    """把近 2 小时原始指标聚合为 5 分钟桶，再由 5 分钟桶聚合小时桶。"""
    from app.modules.ops.models_center import NodeMetricAggregate

    global _agg_last_at
    now = _now()
    if not force and _agg_last_at is not None and (now - _agg_last_at).total_seconds() < 300:
        return 0
    _agg_last_at = now
    nodes = center.execute(select(ServerNode.id)).scalars().all()
    written = 0
    for node_id in nodes:
        rows = center.execute(
            select(NodeMetricSample).where(
                NodeMetricSample.node_id == node_id,
                NodeMetricSample.sampled_at >= now - timedelta(hours=2),
            )
        ).scalars().all()
        buckets: dict[datetime, list[NodeMetricSample]] = {}
        for row in rows:
            bucket = row.sampled_at.replace(second=0, microsecond=0, minute=(row.sampled_at.minute // 5) * 5)
            buckets.setdefault(bucket, []).append(row)
        for bucket, items in buckets.items():
            disk_max = 0.0
            for item in items:
                for disk in item.disks or []:
                    try:
                        disk_max = max(disk_max, float(disk.get("percent") or 0))
                    except (TypeError, ValueError):
                        continue
            values = {
                "sample_count": len(items),
                "cpu_avg": sum(float(i.cpu_percent or 0) for i in items) / len(items),
                "cpu_max": max(float(i.cpu_percent or 0) for i in items),
                "memory_avg": sum(float(i.memory_percent or 0) for i in items) / len(items),
                "memory_max": max(float(i.memory_percent or 0) for i in items),
                "load_avg": sum(float(i.load1 or 0) for i in items) / len(items),
                "disk_max": disk_max,
            }
            row = center.execute(
                select(NodeMetricAggregate).where(
                    NodeMetricAggregate.node_id == node_id,
                    NodeMetricAggregate.resolution == "5m",
                    NodeMetricAggregate.bucket_start == bucket,
                )
            ).scalar_one_or_none()
            if row is None:
                center.add(NodeMetricAggregate(node_id=node_id, resolution="5m", bucket_start=bucket,
                                               created_at=now, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            written += 1
    center.commit()
    # 小时桶：由 5 分钟桶合并，供 7/30 天趋势使用。
    for node_id in nodes:
        rows5 = center.execute(
            select(NodeMetricAggregate).where(
                NodeMetricAggregate.node_id == node_id,
                NodeMetricAggregate.resolution == "5m",
                NodeMetricAggregate.bucket_start >= now - timedelta(days=2),
            )
        ).scalars().all()
        buckets: dict[datetime, list] = {}
        for row in rows5:
            buckets.setdefault(row.bucket_start.replace(minute=0), []).append(row)
        for bucket, items in buckets.items():
            count = sum(int(i.sample_count or 0) for i in items) or len(items)
            values = {
                "sample_count": count,
                "cpu_avg": sum(float(i.cpu_avg or 0) for i in items) / len(items),
                "cpu_max": max(float(i.cpu_max or 0) for i in items),
                "memory_avg": sum(float(i.memory_avg or 0) for i in items) / len(items),
                "memory_max": max(float(i.memory_max or 0) for i in items),
                "load_avg": sum(float(i.load_avg or 0) for i in items) / len(items),
                "disk_max": max(float(i.disk_max or 0) for i in items),
            }
            row = center.execute(
                select(NodeMetricAggregate).where(
                    NodeMetricAggregate.node_id == node_id,
                    NodeMetricAggregate.resolution == "1h",
                    NodeMetricAggregate.bucket_start == bucket,
                )
            ).scalar_one_or_none()
            if row is None:
                center.add(NodeMetricAggregate(node_id=node_id, resolution="1h", bucket_start=bucket,
                                               created_at=now, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
    center.commit()
    return written


def list_metric_aggregates(center, node_id: int, *, resolution: str = "5m", hours: int = 24) -> dict[str, Any]:
    from app.modules.ops.models_center import NodeMetricAggregate

    node = center.get(ServerNode, int(node_id))
    if node is None:
        raise AppError(ErrorCode.NOT_FOUND, "节点不存在")
    since = _now() - timedelta(hours=max(1, min(hours, 24 * 30)))
    rows = center.execute(
        select(NodeMetricAggregate)
        .where(
            NodeMetricAggregate.node_id == node.id,
            NodeMetricAggregate.resolution == resolution,
            NodeMetricAggregate.bucket_start >= since,
        )
        .order_by(NodeMetricAggregate.bucket_start.asc())
        .limit(1000)
    ).scalars().all()
    return {
        "node_id": str(node.id),
        "resolution": resolution,
        "items": [
            {
                "bucket_start": _iso(row.bucket_start),
                "sample_count": int(row.sample_count or 0),
                "cpu_avg": float(row.cpu_avg or 0), "cpu_max": float(row.cpu_max or 0),
                "memory_avg": float(row.memory_avg or 0), "memory_max": float(row.memory_max or 0),
                "load_avg": float(row.load_avg or 0), "disk_max": float(row.disk_max or 0),
            }
            for row in rows
        ],
    }


def mark_stale_operations(center) -> int:
    """任务租约过期后标记结果待核对，不自动重跑；升级任务同步实例升级状态。"""
    from app.modules.ops.models_center import OperationJob

    now = _now()
    rows = center.execute(
        select(OperationJob).where(
            OperationJob.status.in_(("leased", "running")),
            OperationJob.lease_expires_at.is_not(None),
            OperationJob.lease_expires_at < now,
        )
    ).scalars().all()
    for row in rows:
        row.status = "unknown"
        row.error = "执行中失联，结果待核对"
        row.finished_at = now
        row.updated_at = now
        if row.job_type in UPGRADE_JOB_TYPES:
            row.upgrade_status = "unknown"
            _sync_instance_after_job(center, row, now)
    if rows:
        center.commit()
    return len(rows)


def retry_operation(center, job_id: int, *, actor_user_id: int) -> dict[str, Any]:
    from app.modules.ops.models_center import OperationJob
    from app.modules.ops.schemas import OperationCreate

    old = center.get(OperationJob, int(job_id))
    if old is None:
        raise AppError(ErrorCode.NOT_FOUND, "运维任务不存在")
    if old.status in {"pending", "leased", "running", "unknown"}:
        raise ConflictError("任务仍在执行或待核对，不能直接重试")
    payload = OperationCreate(
        job_type=old.job_type, target_version=old.target_version,
        payload=old.payload_json or {}, idempotency_key="",
    )
    return create_operation(center, old.instance_id, payload, actor_user_id=actor_user_id)


# --------------------------------------------------------------------------- SSH 凭据、主机指纹与命令执行
SSH_CONNECT_TIMEOUT_SECONDS = 10
SSH_COMMAND_TIMEOUT_SECONDS = 60
# WebSocket 会话每 30s 复核账号/平台权限/会话；15 分钟无输入自动断开
SSH_SESSION_RECHECK_SECONDS = 30
SSH_WS_IDLE_SECONDS = 15 * 60
SSH_FINGERPRINT_ALGORITHM_DEFAULT = "SHA256"

_SSH_SECRET_FLAG_RE = re.compile(
    r"(?i)(--?(?:password|passwd|pass|token|secret|api[-_]?key|access[-_]?key|private[-_]?key)(?:[=\s]+))(\S+)"
)
_SSH_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[-_]?key|access[-_]?key|authorization|credential)(\s*[:=]\s*)(\S+)"
)
_SSH_SHORT_PASSWORD_RE = re.compile(r"(?<![\w-])-p([^\s;|&'\"]+)")
_SSH_URL_CREDENTIAL_RE = re.compile(r"(://[^:/\s@]+:)([^@/\s]+)(@)")
_SSH_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(\S+(?:\s+\S+)?)"
)
_SSH_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]+")


def ssh_key_fingerprint(key) -> tuple[str, str]:
    """计算 Paramiko PKey 的 OpenSSH 风格 SHA256 指纹：``SHA256:<base64>``。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    encoded = base64.b64encode(digest).decode("ascii").rstrip("=")
    get_name = getattr(key, "get_name", None)
    algorithm = str(get_name() if callable(get_name) else SSH_FINGERPRINT_ALGORITHM_DEFAULT)
    return f"SHA256:{encoded}", (algorithm or SSH_FINGERPRINT_ALGORITHM_DEFAULT)[:16]


def sanitize_ssh_command(command: str, *, limit: int = 500) -> str:
    """审计用命令脱敏：密码/Token/URL 凭据/Bearer 值替换为 ``***``，并压缩空白/截断。"""
    text = str(command or "")
    text = _SSH_SECRET_FLAG_RE.sub(lambda match: match.group(1) + "***", text)
    text = _SSH_AUTH_HEADER_RE.sub(
        lambda match: match.group(1) + match.group(2) + "***", text
    )
    text = _SSH_BEARER_RE.sub(lambda match: match.group(1) + "***", text)
    text = _SSH_SECRET_ASSIGN_RE.sub(lambda match: match.group(1) + "=***", text)
    text = _SSH_SHORT_PASSWORD_RE.sub(lambda match: "-p***", text)
    text = _SSH_URL_CREDENTIAL_RE.sub(lambda match: match.group(1) + "***" + match.group(3), text)
    text = " ".join(text.split())
    return text[: max(1, int(limit))]


def _sanitize_ssh_error(exc: BaseException) -> str:
    return sanitize_ssh_command(str(exc), limit=300)


def _new_ssh_host_key_policy(expected_fingerprint: str = ""):
    """连接前校验主机指纹的 Paramiko 策略（在认证之前调用，不匹配直接拒绝）。"""
    import paramiko

    class _HostKeyPolicy(paramiko.MissingHostKeyPolicy):
        def __init__(self) -> None:
            self.expected = (expected_fingerprint or "").strip()
            self.fingerprint = ""
            self.algorithm = ""
            self.mismatch = False

        def missing_host_key(self, client, hostname, key) -> None:  # noqa: ANN001 - Paramiko 接口
            self.fingerprint, self.algorithm = ssh_key_fingerprint(key)
            if self.expected and not hmac.compare_digest(self.expected, self.fingerprint):
                self.mismatch = True
                raise paramiko.SSHException("SSH 主机指纹不匹配")

    return _HostKeyPolicy()


def _ssh_node(center, node_id: int) -> ServerNode:
    node = center.get(ServerNode, node_id)
    if node is None:
        raise AppError(ErrorCode.NOT_FOUND, "服务器节点不存在")
    return node


def _detected_ssh_host(node: ServerNode) -> str:
    """Agent 观测/配置的 IP：仅用于页面展示，绝不作为 SSH 连接目标回退。"""
    if node.observed_public_ip:
        return node.observed_public_ip
    if node.configured_public_ip:
        return node.configured_public_ip
    ips = node.internal_ips or []
    return ips[0] if ips else ""


def _record_ssh_audit(
    center,
    node: ServerNode,
    action: str,
    *,
    actor_user_id: int | None,
    request_id: str = "",
    ip: str = "",
    summary: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    """写平台审计（连接 / 命令 / 终端 / 指纹），不提交事务。"""
    from app.modules.platform.service import record_platform_audit

    record_platform_audit(
        center,
        actor_user_id=actor_user_id,
        action=action,
        resource_type="server_node",
        resource_id=int(node.id),
        tenant_id=int(node.tenant_id),
        summary=(summary or action)[:255],
        payload=payload,
        request_id=request_id,
        ip=ip,
    )


def get_ssh_config(center, node_id: int) -> dict[str, Any]:
    """读取 SSH 配置（不返回密码；指纹只读展示）。"""
    from app.core.security import decrypt_secret

    node = _ssh_node(center, node_id)
    username = ""
    if node.ssh_username_cipher:
        try:
            username = decrypt_secret(node.ssh_username_cipher)
        except Exception:
            username = ""
    fingerprint = node.ssh_host_fingerprint or ""
    return {
        "host": node.ssh_host or "",
        "detected_host": _detected_ssh_host(node),
        "port": int(node.ssh_port or 22),
        "username": username,
        "has_password": bool(node.ssh_password_cipher),
        "host_configured": bool((node.ssh_host or "").strip()),
        "credentials_configured": bool(node.ssh_username_cipher and node.ssh_password_cipher),
        "ssh_host_fingerprint": fingerprint,
        "ssh_fingerprint_algorithm": node.ssh_fingerprint_algorithm or "",
        "ssh_host_fingerprint_verified_at": _iso(node.ssh_host_fingerprint_verified_at),
        "fingerprint_status": "verified" if fingerprint else "unverified",
    }


def save_ssh_config(
    center,
    node_id: int,
    payload,
    *,
    actor_user_id: int,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """保存 SSH 显式目标/账号；主机或端口变化时清空旧主机指纹（需重新记录）。"""
    from app.core.security import encrypt_secret

    node = _ssh_node(center, node_id)
    old_host = node.ssh_host or ""
    old_port = int(node.ssh_port or 22)
    new_host = (payload.host or "").strip()
    new_port = int(payload.port or 22)
    target_changed = (old_host, old_port) != (new_host, new_port)

    fingerprint_cleared = False
    previous_fingerprint = node.ssh_host_fingerprint or ""
    if target_changed and previous_fingerprint:
        node.ssh_host_fingerprint = ""
        node.ssh_host_fingerprint_verified_at = None
        node.ssh_fingerprint_algorithm = SSH_FINGERPRINT_ALGORITHM_DEFAULT
        fingerprint_cleared = True

    node.ssh_host = new_host
    node.ssh_port = new_port
    username_changed = payload.username is not None
    password_changed = payload.password is not None
    if payload.username is not None:
        node.ssh_username_cipher = (
            encrypt_secret(payload.username.strip()) if payload.username.strip() else ""
        )
    if payload.password is not None:
        node.ssh_password_cipher = encrypt_secret(payload.password) if payload.password else ""
    center.add(node)
    _record_ssh_audit(
        center,
        node,
        "ssh.config.update",
        actor_user_id=actor_user_id,
        request_id=request_id,
        ip=ip,
        summary=f"更新 SSH 配置：{(new_host or '未配置')}:{new_port}",
        payload={
            "host": new_host,
            "port": new_port,
            "target_changed": target_changed,
            "username_changed": username_changed,
            "password_changed": password_changed,
            "fingerprint_cleared": fingerprint_cleared,
        },
    )
    if fingerprint_cleared:
        _record_ssh_audit(
            center,
            node,
            "ssh.fingerprint.reset",
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip=ip,
            summary="SSH 目标变更，旧主机指纹已清空",
            payload={
                "reason": "target_changed",
                "previous_fingerprint": previous_fingerprint[:128],
                "host": new_host,
                "port": new_port,
            },
        )
    center.commit()
    return get_ssh_config(center, node_id)


def reset_ssh_host_fingerprint(
    center,
    node_id: int,
    *,
    actor_user_id: int,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """管理员重置 SSH 主机指纹：仅清空指纹三字段，不动 host/port/账号密码。"""
    node = _ssh_node(center, node_id)
    previous = node.ssh_host_fingerprint or ""
    node.ssh_host_fingerprint = ""
    node.ssh_host_fingerprint_verified_at = None
    node.ssh_fingerprint_algorithm = SSH_FINGERPRINT_ALGORITHM_DEFAULT
    center.add(node)
    _record_ssh_audit(
        center,
        node,
        "ssh.fingerprint.reset",
        actor_user_id=actor_user_id,
        request_id=request_id,
        ip=ip,
        summary="管理员重置 SSH 主机指纹",
        payload={
            "reason": "admin_reset",
            "previous_fingerprint": previous[:128],
            "host": node.ssh_host or "",
            "port": int(node.ssh_port or 22),
        },
    )
    center.commit()
    config = get_ssh_config(center, node_id)
    config["reset"] = True
    return config


def ssh_connect_params(center, node_id: int) -> dict[str, Any]:
    """SSH 连接参数：主机只取管理员显式配置的 ``ssh_host``，禁止回退 Agent 上报 IP。"""
    from app.core.security import decrypt_secret

    node = _ssh_node(center, node_id)
    host = (node.ssh_host or "").strip()
    if not host:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "请先显式配置服务器 SSH 主机地址（不会使用 Agent 上报 IP 自动回退）",
        )
    if not node.ssh_username_cipher or not node.ssh_password_cipher:
        raise AppError(ErrorCode.VALIDATION_ERROR, "请先保存 SSH 账号和密码")
    try:
        username = decrypt_secret(node.ssh_username_cipher)
        password = decrypt_secret(node.ssh_password_cipher)
    except Exception:
        raise AppError(ErrorCode.VALIDATION_ERROR, "SSH 凭据无法解密，请重新保存") from None
    return {
        "host": host,
        "port": int(node.ssh_port or 22),
        "username": username,
        "password": password,
        "node_id": int(node.id),
    }


def ssh_prepare_terminal(node_id: int) -> dict[str, Any]:
    """WebSocket 连接前准备（独立中心库 Session，供线程池调用）。"""
    from app.db.session import center_session

    with center_session() as center:
        return ssh_connect_params(center, node_id)


def confirm_ssh_host_fingerprint(
    center,
    node: ServerNode,
    fingerprint: str,
    algorithm: str,
    *,
    actor_user_id: int | None,
    request_id: str = "",
    ip: str = "",
    host: str = "",
) -> dict[str, Any]:
    """首次连接记录指纹；后续不匹配拒绝；匹配则仅返回信息（不覆盖已记录值）。"""
    actual = (fingerprint or "").strip()
    if not actual:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "SSH 服务端未返回主机指纹，已拒绝连接")
    actual_algorithm = (algorithm or SSH_FINGERPRINT_ALGORITHM_DEFAULT)[:16]
    stored = (node.ssh_host_fingerprint or "").strip()
    target = host or node.ssh_host or ""
    if stored:
        if not hmac.compare_digest(stored, actual):
            _record_ssh_audit(
                center,
                node,
                "ssh.fingerprint.mismatch",
                actor_user_id=actor_user_id,
                request_id=request_id,
                ip=ip,
                summary="SSH 主机指纹不匹配，拒绝连接",
                payload={
                    "host": target,
                    "expected": stored[:128],
                    "actual": actual[:128],
                    "algorithm": actual_algorithm,
                },
            )
            center.commit()
            raise AppError(
                ErrorCode.CONFLICT,
                "SSH 主机指纹不匹配，已拒绝连接；如确认服务器变更，请管理员核对后重置指纹",
                details={
                    "reason": "fingerprint_mismatch",
                    "expected": stored[:128],
                    "actual": actual[:128],
                    "algorithm": actual_algorithm,
                    "host": target,
                },
            )
        return {"recorded": False, "fingerprint": stored, "algorithm": node.ssh_fingerprint_algorithm or actual_algorithm}

    node.ssh_host_fingerprint = actual[:128]
    node.ssh_fingerprint_algorithm = actual_algorithm
    node.ssh_host_fingerprint_verified_at = _now()
    center.add(node)
    _record_ssh_audit(
        center,
        node,
        "ssh.fingerprint.recorded",
        actor_user_id=actor_user_id,
        request_id=request_id,
        ip=ip,
        summary="首次连接记录 SSH 主机指纹",
        payload={
            "host": target,
            "fingerprint": actual[:128],
            "algorithm": actual_algorithm,
        },
    )
    center.commit()
    return {"recorded": True, "fingerprint": actual[:128], "algorithm": actual_algorithm}


def _open_ssh_client(
    center,
    node: ServerNode,
    params: dict[str, Any],
    *,
    actor_user_id: int | None,
    request_id: str = "",
    ip: str = "",
    purpose: str = "command",
):
    """建立 SSH 连接：认证前校验主机指纹，连接/失败均写审计。"""
    try:
        import paramiko
    except ImportError:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "服务器缺少 SSH 依赖") from None

    expected = (node.ssh_host_fingerprint or "").strip()
    policy = _new_ssh_host_key_policy(expected)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(policy)
    started = time.perf_counter()
    try:
        client.connect(
            hostname=params["host"],
            port=int(params["port"]),
            username=params["username"],
            password=params["password"],
            timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            banner_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            auth_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转受控错误
        try:
            client.close()
        except Exception:
            pass
        duration_ms = int((time.perf_counter() - started) * 1000)
        if policy.mismatch:
            _record_ssh_audit(
                center,
                node,
                "ssh.fingerprint.mismatch",
                actor_user_id=actor_user_id,
                request_id=request_id,
                ip=ip,
                summary="SSH 主机指纹不匹配，拒绝连接",
                payload={
                    "host": params["host"],
                    "port": int(params["port"]),
                    "purpose": purpose,
                    "expected": expected[:128],
                    "actual": (policy.fingerprint or "")[:128],
                    "algorithm": policy.algorithm or "",
                },
            )
            center.commit()
            raise AppError(
                ErrorCode.CONFLICT,
                "SSH 主机指纹不匹配，已拒绝连接；如确认服务器变更，请管理员核对后重置指纹",
                details={
                    "reason": "fingerprint_mismatch",
                    "expected": expected[:128],
                    "actual": (policy.fingerprint or "")[:128],
                    "algorithm": policy.algorithm or "",
                    "host": params["host"],
                },
            ) from None
        error = _sanitize_ssh_error(exc)
        _record_ssh_audit(
            center,
            node,
            "ssh.connect.failed",
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip=ip,
            summary="SSH 连接失败",
            payload={
                "host": params["host"],
                "port": int(params["port"]),
                "purpose": purpose,
                "error": error,
                "duration_ms": duration_ms,
            },
        )
        center.commit()
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, f"SSH 连接失败：{error}") from None

    info = confirm_ssh_host_fingerprint(
        center,
        node,
        policy.fingerprint,
        policy.algorithm,
        actor_user_id=actor_user_id,
        request_id=request_id,
        ip=ip,
        host=params["host"],
    )
    _record_ssh_audit(
        center,
        node,
        "ssh.connect",
        actor_user_id=actor_user_id,
        request_id=request_id,
        ip=ip,
        summary=f"SSH 连接成功：{params['host']}",
        payload={
            "host": params["host"],
            "port": int(params["port"]),
            "purpose": purpose,
            "fingerprint": info["fingerprint"],
            "algorithm": info["algorithm"],
            "fingerprint_recorded": bool(info.get("recorded")),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        },
    )
    center.commit()
    return client, params, info


def ssh_execute(
    center,
    node_id: int,
    command: str,
    *,
    actor_user_id: int | None = None,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """非交互命令执行：线程池由调用方（FastAPI sync 路由）保证；连接/命令/断开写审计。"""
    node = _ssh_node(center, node_id)
    params = ssh_connect_params(center, node_id)
    sanitized = sanitize_ssh_command(command)
    started = time.perf_counter()
    client, params, info = _open_ssh_client(
        center,
        node,
        params,
        actor_user_id=actor_user_id,
        request_id=request_id,
        ip=ip,
        purpose="command",
    )
    output = ""
    exit_status: int | None = None
    error = ""
    try:
        _, stdout, stderr = client.exec_command(command, timeout=SSH_COMMAND_TIMEOUT_SECONDS)
        output = (stdout.read() + stderr.read()).decode("utf-8", "replace")
        exit_status = int(stdout.channel.recv_exit_status())
    except Exception as exc:  # noqa: BLE001 - 返回受控错误给平台管理员
        error = _sanitize_ssh_error(exc)
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            client.close()
        except Exception:
            pass
        _record_ssh_audit(
            center,
            node,
            "ssh.command",
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip=ip,
            summary=f"SSH 执行命令（exit={exit_status if exit_status is not None else 'error'}）",
            payload={
                "host": params["host"],
                "port": int(params["port"]),
                "command": sanitized,
                "exit_status": exit_status,
                "duration_ms": duration_ms,
                "fingerprint": info["fingerprint"],
                "error": error,
            },
        )
        _record_ssh_audit(
            center,
            node,
            "ssh.disconnect",
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip=ip,
            summary="SSH 连接已关闭（命令执行结束）",
            payload={
                "host": params["host"],
                "port": int(params["port"]),
                "reason": "command_completed" if not error else "command_error",
                "duration_ms": duration_ms,
            },
        )
        center.commit()
    if error:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, f"SSH 执行失败：{error}")
    return {
        "output": output[-20000:],
        "exit_status": int(exit_status or 0),
        "host": params["host"],
        "fingerprint": info["fingerprint"],
        "algorithm": info["algorithm"],
    }


def ssh_open_terminal(
    node_id: int,
    *,
    actor_user_id: int,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """打开交互式 SSH 终端（阻塞调用，WebSocket 侧通过线程池执行）。"""
    from app.db.session import center_session

    with center_session() as center:
        node = _ssh_node(center, node_id)
        params = ssh_connect_params(center, node_id)
        client, params, info = _open_ssh_client(
            center,
            node,
            params,
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip=ip,
            purpose="terminal",
        )
        try:
            channel = client.invoke_shell(term="xterm-256color", width=120, height=32)
            channel.settimeout(0.2)
        except Exception as exc:  # noqa: BLE001
            try:
                client.close()
            except Exception:
                pass
            error = _sanitize_ssh_error(exc)
            _record_ssh_audit(
                center,
                node,
                "ssh.terminal.open.failed",
                actor_user_id=actor_user_id,
                request_id=request_id,
                ip=ip,
                summary="SSH 交互终端打开失败",
                payload={"host": params["host"], "port": int(params["port"]), "error": error},
            )
            center.commit()
            raise AppError(
                ErrorCode.SERVICE_UNAVAILABLE, f"SSH 交互终端打开失败：{error}"
            ) from None
        _record_ssh_audit(
            center,
            node,
            "ssh.terminal.open",
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip=ip,
            summary=f"打开交互式 SSH 终端：{params['host']}",
            payload={
                "host": params["host"],
                "port": int(params["port"]),
                "fingerprint": info["fingerprint"],
                "algorithm": info["algorithm"],
            },
        )
        center.commit()
        return {
            "node_id": int(node.id),
            "client": client,
            "channel": channel,
            "host": params["host"],
            "port": int(params["port"]),
            "fingerprint": info["fingerprint"],
            "algorithm": info["algorithm"],
        }


def ssh_close_terminal(
    resources: dict[str, Any],
    *,
    actor_user_id: int,
    request_id: str = "",
    ip: str = "",
    reason: str = "client_closed",
    duration_seconds: int = 0,
    input_bytes: int = 0,
) -> None:
    """关闭交互式 SSH 终端并写终端关闭 / 断开审计（阻塞调用，线程池执行）。"""
    client = resources.get("client")
    channel = resources.get("channel")
    try:
        if channel is not None:
            channel.close()
    except Exception:
        pass
    try:
        if client is not None:
            client.close()
    except Exception:
        pass
    node_id = resources.get("node_id")
    if not node_id:
        return
    from app.db.session import center_session

    with center_session() as center:
        node = center.get(ServerNode, int(node_id))
        if node is None:
            return
        _record_ssh_audit(
            center,
            node,
            "ssh.terminal.close",
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip=ip,
            summary=f"关闭交互式 SSH 终端（{reason}）",
            payload={
                "host": resources.get("host", ""),
                "reason": reason,
                "duration_seconds": int(duration_seconds),
                "input_bytes": int(input_bytes),
                "fingerprint": resources.get("fingerprint", ""),
            },
        )
        _record_ssh_audit(
            center,
            node,
            "ssh.disconnect",
            actor_user_id=actor_user_id,
            request_id=request_id,
            ip=ip,
            summary=f"SSH 连接已断开（{reason}）",
            payload={
                "host": resources.get("host", ""),
                "reason": reason,
                "duration_seconds": int(duration_seconds),
            },
        )
        center.commit()


# --------------------------------------------------------------------------- WebSocket 会话复核
def _read_ws_session(session_id: str) -> dict[str, Any] | None:
    from app.core.redis import read_session

    return read_session(session_id)


def _load_ws_user(user_id: int):
    from app.db.session import center_session
    from app.modules.identity.service import load_authenticated_user

    with center_session() as center:
        return load_authenticated_user(center, user_id)


def load_platform_ssh_user(user_id: int):
    """WebSocket 初始鉴权：加载用户并复核平台管理员身份（线程池调用）。"""
    return _load_ws_user(user_id)


def ssh_session_recheck(session_id: str, user_id: int) -> tuple[bool, str]:
    """每 30s 复核：会话仍有效、账号 active、仍具平台管理员权限。

    返回 ``(ok, reason)``；reason 取值：session_revoked / account_disabled /
    permission_revoked。撤权/注销后 WebSocket 必须关闭连接。
    """
    from app.core.auth import session_scope

    payload = _read_ws_session(session_id)
    if not payload:
        return False, "session_revoked"
    if int(payload.get("user_id") or 0) != int(user_id):
        return False, "session_revoked"
    if session_scope(payload) not in (None, "platform"):
        return False, "session_revoked"
    user = _load_ws_user(int(user_id))
    if user is None:
        return False, "account_disabled"
    if user.status != "active":
        return False, "account_disabled"
    if not user.is_platform_admin:
        return False, "permission_revoked"
    return True, ""
