"""统一运维中心：实例、节点、指标、探测与告警服务。"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
from sqlalchemy import and_, func, or_, select

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
    InstanceCreate,
    InstanceUpdate,
    NodeEnrollIn,
    TelemetryPayload,
)

ENROLL_TOKEN_TTL_MINUTES = 30
OFFLINE_AFTER_SECONDS = 90
METRIC_RETENTION_DAYS = 7
ALERT_REMIND_SECONDS = 30 * 60

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
        "agent_version": node.agent_version,
        "app_version": node.app_version,
        "status": effective_node_status(node),
        "enrolled_at": _iso(node.enrolled_at),
        "last_seen_at": _iso(node.last_seen_at),
        "last_metrics_at": _iso(node.last_metrics_at),
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
    return {
        "id": str(instance.id),
        "tenant_id": str(instance.tenant_id),
        "tenant_name": tenant_name,
        "name": instance.name,
        "domain": instance.domain,
        "environment": instance.environment,
        "status": instance.status,
        "app_version": instance.app_version or next((n.app_version for n in nodes if n.app_version), ""),
        "notes": instance.notes,
        "created_at": _iso(instance.created_at),
        "node_count": len(nodes),
        "online_node_count": online,
        "open_alert_count": open_alerts,
        "nodes": node_payloads,
    }


def list_instances(
    center,
    *,
    page: int,
    page_size: int,
    q: str = "",
    status: str = "",
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
    if payload.status is not None and payload.status != row.status:
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
            payload={"fields": changes},
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
    node.app_version = payload.app_version or node.app_version
    if payload.app_version:
        instance = center.get(TenantInstance, node.instance_id)
        if instance is not None and instance.app_version != payload.app_version:
            instance.app_version = payload.app_version
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
def list_releases(center) -> list[dict[str, Any]]:
    from app.modules.ops.models_center import ReleaseVersion

    rows = center.execute(select(ReleaseVersion).order_by(ReleaseVersion.id.desc())).scalars().all()
    return [
        {
            "id": str(row.id),
            "version": row.version,
            "image_tag": row.image_tag,
            "image_digest": row.image_digest,
            "status": row.status,
            "notes": row.notes,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def create_release(center, payload, *, actor_user_id: int) -> dict[str, Any]:
    from app.modules.ops.models_center import ReleaseVersion

    if center.execute(select(ReleaseVersion).where(ReleaseVersion.version == payload.version)).scalar_one_or_none():
        raise ConflictError("版本号已存在")
    row = ReleaseVersion(
        version=payload.version, image_tag=payload.image_tag,
        image_digest=payload.image_digest, notes=payload.notes, created_by=actor_user_id,
    )
    center.add(row); center.commit()
    return {"id": str(row.id), "version": row.version}


def create_operation(center, instance_id: int, payload, *, actor_user_id: int) -> dict[str, Any]:
    from app.modules.ops.models_center import OperationJob
    from app.modules.platform.service import record_platform_audit

    instance = get_instance(center, instance_id)
    node = center.execute(
        select(ServerNode).where(ServerNode.instance_id == instance.id).order_by(ServerNode.id.asc())
    ).scalars().first()
    if node is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "实例尚未接入服务器节点")
    key = payload.idempotency_key or f"{instance.id}:{payload.job_type}:{_now().isoformat()}:{secrets.token_hex(4)}"
    existing = center.execute(select(OperationJob).where(OperationJob.idempotency_key == key)).scalar_one_or_none()
    if existing is not None:
        return _operation_payload(existing)
    running = center.execute(
        select(OperationJob).where(
            OperationJob.instance_id == instance.id,
            OperationJob.status.in_(("pending", "leased", "running")),
        )
    ).scalars().first()
    if running is not None:
        raise ConflictError("该实例已有未完成运维任务，请等待完成")
    row = OperationJob(
        instance_id=instance.id, node_id=node.id, job_type=payload.job_type,
        status="pending", idempotency_key=key, target_version=payload.target_version,
        payload_json=payload.payload, requested_by=actor_user_id,
    )
    center.add(row); center.commit()
    record_platform_audit(
        center, actor_user_id=actor_user_id, action="operation.create",
        resource_type="operation_job", resource_id=row.id, tenant_id=instance.tenant_id,
        summary=f"创建运维任务：{payload.job_type}", payload={"instance_id": instance.id},
    )
    center.commit()
    return _operation_payload(row, center=center)


def _operation_payload(row, center=None) -> dict[str, Any]:
    data = {
        "id": str(row.id), "instance_id": str(row.instance_id), "node_id": str(row.node_id) if row.node_id else None,
        "job_type": row.job_type, "status": row.status, "target_version": row.target_version,
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


def poll_agent_job(center, node: ServerNode) -> dict[str, Any] | None:
    from app.modules.ops.models_center import OperationJob
    now = _now()
    # 过期租约先标记为 unknown；绝不自动重新执行，必须人工核对。
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
    if stale:
        center.commit()
    row = center.execute(
        select(OperationJob).where(
            OperationJob.node_id == node.id,
            OperationJob.status == "pending",
        ).order_by(OperationJob.id.asc()).with_for_update()
    ).scalars().first()
    if row is None:
        return None
    row.status = "leased"; row.attempts = int(row.attempts or 0) + 1
    row.lease_expires_at = now + timedelta(minutes=10); row.started_at = row.started_at or now
    center.commit()
    return {"id": str(row.id), "job_type": row.job_type, "target_version": row.target_version, "payload": row.payload_json or {}, "attempts": row.attempts}


def agent_job_event(center, node: ServerNode, job_id: int, payload) -> dict[str, Any]:
    from app.modules.ops.models_center import BackupRecord, OperationJob, OperationJobStep
    row = center.execute(select(OperationJob).where(OperationJob.id == job_id, OperationJob.node_id == node.id)).scalar_one_or_none()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "任务不存在")
    now = _now()
    if payload.step_no and payload.step_name:
        step = center.execute(select(OperationJobStep).where(OperationJobStep.job_id == row.id, OperationJobStep.step_no == payload.step_no)).scalar_one_or_none()
        if step is None:
            step = OperationJobStep(job_id=row.id, step_no=payload.step_no, name=payload.step_name, status=payload.step_status or "running", message=payload.message, started_at=now)
            center.add(step)
        else:
            step.status = payload.step_status or step.status
            step.message = payload.message or step.message
            step.finished_at = now if step.status in {"succeeded", "failed", "skipped"} else None
    if payload.status in {"succeeded", "failed", "unknown"}:
        row.status = payload.status; row.finished_at = now; row.error = payload.error[:500]
        if payload.result is not None:
            row.result_json = payload.result
        if row.job_type == "backup":
            backup = BackupRecord(instance_id=row.instance_id, node_id=row.node_id, job_id=row.id,
                                  status="succeeded" if payload.status == "succeeded" else "failed",
                                  storage_key=str((payload.result or {}).get("storage_key", "")),
                                  size_bytes=int((payload.result or {}).get("size_bytes", 0) or 0),
                                  started_at=row.started_at or now, finished_at=now,
                                  retention_until=now + timedelta(days=30), error=payload.error[:500])
            center.add(backup)
    else:
        row.status = "running"
    center.commit()
    return {"ok": True}


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
    """任务租约过期后标记结果待核对，不自动重跑。"""
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
    if rows:
        center.commit()
    return len(rows)


def retry_operation(center, job_id: int, *, actor_user_id: int) -> dict[str, Any]:
    from app.modules.ops.models_center import OperationJob

    old = center.get(OperationJob, int(job_id))
    if old is None:
        raise AppError(ErrorCode.NOT_FOUND, "运维任务不存在")
    if old.status in {"pending", "leased", "running"}:
        raise ConflictError("任务仍在执行中，不能重试")
    payload = OperationCreate(
        job_type=old.job_type, target_version=old.target_version,
        payload=old.payload_json or {}, idempotency_key="",
    )
    return create_operation(center, old.instance_id, payload, actor_user_id=actor_user_id)
