"""运维告警通知：默认持续阈值、维护窗口、重复抑制与恢复邮件。

职责边界（task-30）：

* **默认持续阈值**：CPU / 内存 / 磁盘 / Agent 失联 / 公网探测 / 备份 / 请求性能的
  默认值集中在 :func:`default_thresholds`，可由 ``app.core.config`` 中的环境变量覆盖；
  具体判定（采样连续性、root/api 分离）由 task-29 的采集模块使用这些默认值。
* **维护窗口**：窗口内不发送告警 / 恢复邮件（事件仍记录，避免丢数据）；
* **重复告警抑制**：同一未恢复告警在 ``ALERT_REMIND_SECONDS`` 内只提醒一次；维护窗口
  结束时立即恢复通知；对外返回 ``suppressed`` / ``suppress_until`` 供前端展示。
* **恢复邮件**：告警由采集侧置为 ``resolved`` 后，本模块只读取该状态并补发恢复邮件，
  **不会修改告警状态、不重复置 resolved**（与 task-29 采集逻辑解耦）；发送标记复用
  ``last_notified_at``（恢复时间 > resolved_at 即表示已发过恢复邮件，无需新增表列）。

本模块不阻塞业务请求：只被调度进程 / 告警列表接口调用，邮件失败仅记录日志。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import or_, select

from app.core.config import settings

logger = logging.getLogger("ventasync.ops.alerting")

NOTIFY_BATCH_LIMIT = 20
RECOVERY_WINDOW_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


# --------------------------------------------------------------------------- 默认阈值
def default_thresholds() -> dict[str, dict[str, Any]]:
    """默认持续阈值（环境变量覆盖），供采集/页面/文档统一引用。

    ``sustain_seconds`` 表示必须持续满足条件的时长；``min_samples`` 为最小样本数，
    采集侧应按「时间窗口 + 采样连续性」判定，而不是固定样本数（task-29 负责实现）。
    """
    return {
        "cpu": {
            "percent": float(settings.alert_cpu_percent),
            "sustain_seconds": int(settings.alert_cpu_sustain_seconds),
        },
        "memory": {
            "percent": float(settings.alert_memory_percent),
            "sustain_seconds": int(settings.alert_memory_sustain_seconds),
        },
        "disk": {
            "warning_percent": float(settings.alert_disk_warning_percent),
            "critical_percent": float(settings.alert_disk_critical_percent),
            "sustain_seconds": 0,
        },
        "agent": {
            "offline_seconds": int(settings.alert_agent_offline_seconds),
            "sustain_seconds": int(settings.alert_agent_offline_seconds),
        },
        "public": {
            "failure_count": int(settings.alert_public_failure_count),
            "sustain_seconds": 0,
        },
        "backup": {"overdue_hours": 26, "sustain_seconds": 0},
        "request": {
            "p95_ms": int(settings.alert_request_p95_ms),
            "rate_5xx": float(settings.alert_request_5xx_rate),
            "min_requests": int(settings.alert_request_min_requests),
            "window_seconds": int(settings.request_metrics_window_seconds),
        },
        "notify": {
            "remind_seconds": int(settings.alert_remind_seconds),
            "recovery_email_enabled": bool(settings.alert_recovery_email_enabled),
        },
    }


def threshold_for(alert_type: str) -> Mapping[str, Any]:
    """按告警类型取默认阈值（未知类型返回空字典）。"""
    key = (alert_type or "").strip().lower()
    table = default_thresholds()
    return table.get(key, {})


# --------------------------------------------------------------------------- 维护窗口
def maintenance_window_for(
    center, instance_id: int, *, now: datetime | None = None
):
    """读取实例当前生效的维护窗口（没有返回 ``None``）。"""
    from app.modules.ops.models_center import MaintenanceWindow

    moment = now or _now()
    return center.execute(
        select(MaintenanceWindow)
        .where(
            MaintenanceWindow.instance_id == int(instance_id),
            MaintenanceWindow.starts_at <= moment,
            MaintenanceWindow.ends_at >= moment,
        )
        .order_by(MaintenanceWindow.ends_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def maintenance_windows_for_instances(
    center, instance_ids: Iterable[int], *, now: datetime | None = None
) -> dict[int, Any]:
    """批量读取当前生效的维护窗口（避免列表接口 N+1 查询）。"""
    from app.modules.ops.models_center import MaintenanceWindow

    ids = sorted({int(item) for item in instance_ids if item is not None})
    if not ids:
        return {}
    moment = now or _now()
    rows = center.execute(
        select(MaintenanceWindow)
        .where(
            MaintenanceWindow.instance_id.in_(ids),
            MaintenanceWindow.starts_at <= moment,
            MaintenanceWindow.ends_at >= moment,
        )
        .order_by(MaintenanceWindow.ends_at.asc())
    ).scalars().all()
    result: dict[int, Any] = {}
    for row in rows:
        result[int(row.instance_id)] = row  # 同一实例取最晚结束的窗口
    return result


# --------------------------------------------------------------------------- 抑制状态
def alert_suppression_fields(
    alert: Any,
    *,
    maintenance: Any | None = None,
    now: datetime | None = None,
    remind_seconds: int | None = None,
) -> dict[str, Any]:
    """计算告警的维护窗口 / 抑制 / 恢复通知展示字段（纯逻辑，可单测）。

    * ``suppressed``：维护窗口内或重复提醒抑制期内为真；
    * ``suppress_until``：下次允许通知的时间（维护结束与提醒窗口取较晚者）；
    * ``recovery_notified_at``：已恢复且恢复邮件已发送的时间。
    """
    moment = now or _now()
    remind = int(
        remind_seconds if remind_seconds is not None else settings.alert_remind_seconds
    )
    in_maintenance = bool(
        maintenance is not None
        and maintenance.starts_at is not None
        and maintenance.ends_at is not None
        and maintenance.starts_at <= moment <= maintenance.ends_at
    )
    status = str(getattr(alert, "status", "") or "")
    resolved_at = getattr(alert, "resolved_at", None)
    last_notified_at = getattr(alert, "last_notified_at", None)
    remind_until: datetime | None = None
    if (
        status in {"open", "acknowledged"}
        and last_notified_at is not None
        and remind > 0
    ):
        candidate = last_notified_at + timedelta(seconds=remind)
        if candidate > moment:
            remind_until = candidate
    if status == "resolved":
        in_maintenance = False
        remind_until = None
    suppress_until: datetime | None = None
    if in_maintenance and maintenance is not None:
        suppress_until = maintenance.ends_at
    if remind_until is not None and (suppress_until is None or remind_until > suppress_until):
        suppress_until = remind_until
    recovery_notified_at: datetime | None = None
    if (
        status == "resolved"
        and resolved_at is not None
        and last_notified_at is not None
        and last_notified_at >= resolved_at
    ):
        recovery_notified_at = last_notified_at
    return {
        "in_maintenance": in_maintenance,
        "maintenance_reason": (
            str(getattr(maintenance, "reason", "") or "") if in_maintenance else ""
        ),
        "suppressed": bool(in_maintenance or remind_until is not None),
        "suppress_until": _iso(suppress_until),
        "recovery_notified_at": _iso(recovery_notified_at),
    }


def enrich_alerts(
    center, alerts: Sequence[Any], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """为告警列表批量附加维护 / 抑制 / 恢复字段（返回与入参同序的字典列表）。"""
    moment = now or _now()
    windows = maintenance_windows_for_instances(
        center, (getattr(alert, "instance_id", 0) for alert in alerts), now=moment
    )
    return [
        alert_suppression_fields(
            alert,
            maintenance=windows.get(int(getattr(alert, "instance_id", 0) or 0)),
            now=moment,
        )
        for alert in alerts
    ]


def recovery_notified_at(alert: Any) -> datetime | None:
    """已恢复告警的恢复邮件发送时间；未发送返回 ``None``。"""
    status = str(getattr(alert, "status", "") or "")
    resolved_at = getattr(alert, "resolved_at", None)
    last_notified_at = getattr(alert, "last_notified_at", None)
    if status != "resolved" or resolved_at is None or last_notified_at is None:
        return None
    return last_notified_at if last_notified_at >= resolved_at else None


# --------------------------------------------------------------------------- 收件人 / 发信
def platform_admin_emails(center) -> list[str]:
    """当前有效平台管理员邮箱（与 service._platform_admin_emails 语义一致）。"""
    from app.modules.identity.models_center import PlatformRole, PlatformUserRole, User

    rows = center.execute(
        select(User.email)
        .join(PlatformUserRole, PlatformUserRole.user_id == User.id)
        .join(PlatformRole, PlatformRole.id == PlatformUserRole.role_id)
        .where(PlatformRole.code == "platform_admin", User.status == "active")
        .distinct()
    ).all()
    return [str(row[0]) for row in rows if row[0]]


def _send_mail(center, *, recipients: Sequence[str], subject: str, text: str) -> bool:
    if not recipients:
        return False
    from app.modules.email import service as email_service

    try:
        email_service.send_default(center, to=list(recipients), subject=subject, text=text)
        return True
    except Exception:  # noqa: BLE001 - 邮件服务未配置 / 临时失败不阻塞监控
        logger.warning("运维告警邮件发送失败（忽略）：%s", subject[:120])
        return False


def _alert_text(alert: Any) -> str:
    return (
        f"告警类型：{getattr(alert, 'alert_type', '')}\n"
        f"级别：{getattr(alert, 'severity', '')}\n"
        f"节点 ID：{getattr(alert, 'node_id', '')}\n"
        f"当前值：{getattr(alert, 'metric_value', '')}\n"
        f"阈值：{getattr(alert, 'threshold', '')}\n"
        f"首次触发：{_iso(getattr(alert, 'first_seen_at', None))}\n"
        f"最近触发：{_iso(getattr(alert, 'last_seen_at', None))}\n\n"
        "请登录统一 ERP 管理中心 → 服务器与实例 → 告警中心处理。"
    )


# --------------------------------------------------------------------------- 通知
def notify_open_alerts(center) -> int:
    """发送未恢复告警邮件：维护窗口内抑制，同一告警按提醒间隔抑制重复。

    ``notify_open_alerts`` 同时触发恢复邮件扫描，调度进程只需调用一次。
    """
    from app.modules.ops.models_center import AlertEvent

    now = _now()
    recipients = platform_admin_emails(center)
    sent = notify_resolved_alerts(center)
    if not recipients:
        return sent
    remind_seconds = max(60, int(settings.alert_remind_seconds))
    rows = center.execute(
        select(AlertEvent).where(
            AlertEvent.status.in_(("open", "acknowledged")),
            or_(
                AlertEvent.last_notified_at.is_(None),
                AlertEvent.last_notified_at < now - timedelta(seconds=remind_seconds),
            ),
        ).limit(NOTIFY_BATCH_LIMIT)
    ).scalars().all()
    if not rows:
        return sent
    windows = maintenance_windows_for_instances(
        center, (row.instance_id for row in rows), now=now
    )
    for row in rows:
        if int(row.instance_id) in windows:
            # 维护窗口内：只记录状态，不发邮件；窗口结束后下一轮立即补发
            continue
        ok = _send_mail(
            center,
            recipients=recipients,
            subject=f"[ERP 运维告警][{row.severity}] {str(row.message or '')[:80]}",
            text=_alert_text(row),
        )
        if not ok:
            continue
        row.notify_count = int(row.notify_count or 0) + 1
        row.last_notified_at = now
        row.updated_at = now
        sent += 1
    if sent:
        center.commit()
    return sent


def notify_resolved_alerts(center) -> int:
    """补发恢复邮件：只读取采集侧已置为 resolved 的告警，不改告警状态。"""
    from app.modules.ops.models_center import AlertEvent

    if not bool(settings.alert_recovery_email_enabled):
        return 0
    now = _now()
    recipients = platform_admin_emails(center)
    if not recipients:
        return 0
    rows = center.execute(
        select(AlertEvent)
        .where(
            AlertEvent.status == "resolved",
            AlertEvent.resolved_at.is_not(None),
            AlertEvent.resolved_at >= now - timedelta(seconds=RECOVERY_WINDOW_SECONDS),
            AlertEvent.notify_count > 0,
            or_(
                AlertEvent.last_notified_at.is_(None),
                AlertEvent.last_notified_at < AlertEvent.resolved_at,
            ),
        )
        .order_by(AlertEvent.resolved_at.asc())
        .limit(NOTIFY_BATCH_LIMIT)
    ).scalars().all()
    if not rows:
        return 0
    windows = maintenance_windows_for_instances(
        center, (row.instance_id for row in rows), now=now
    )
    sent = 0
    for row in rows:
        if int(row.instance_id) in windows:
            continue  # 维护窗口内不打扰，窗口结束后补发
        duration = ""
        if row.first_seen_at and row.resolved_at:
            seconds = max(0, int((row.resolved_at - row.first_seen_at).total_seconds()))
            duration = f"\n持续时间：{seconds // 60} 分钟"
        ok = _send_mail(
            center,
            recipients=recipients,
            subject=f"[ERP 运维恢复][{row.severity}] {str(row.message or '')[:80]}",
            text=(
                f"告警已恢复：{getattr(row, 'message', '')}\n"
                f"告警类型：{getattr(row, 'alert_type', '')}\n"
                f"节点 ID：{getattr(row, 'node_id', '')}\n"
                f"首次触发：{_iso(row.first_seen_at)}\n"
                f"恢复时间：{_iso(row.resolved_at)}{duration}\n\n"
                "无需处理；如仍异常请检查采集与实例状态。"
            ),
        )
        if not ok:
            continue
        # 复用 last_notified_at 作为恢复邮件已发送标记（> resolved_at），不新增表列
        row.last_notified_at = now
        row.updated_at = now
        sent += 1
    if sent:
        center.commit()
    return sent


__all__ = [
    "alert_suppression_fields",
    "default_thresholds",
    "enrich_alerts",
    "maintenance_window_for",
    "maintenance_windows_for_instances",
    "notify_open_alerts",
    "notify_resolved_alerts",
    "platform_admin_emails",
    "recovery_notified_at",
    "threshold_for",
]
