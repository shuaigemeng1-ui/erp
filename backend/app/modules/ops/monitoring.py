"""监控判定与采集准确性（批次3：task-29）。

本模块独立于 ``ops/service.py`` 的运维业务，承载：

* 公网探测：首页 ``root``（/）与 API ``api``（/readyz）分开累计连续失败，
  API 连续 3 次失败独立告警，不被首页成功掩盖；
* CPU / 内存告警：按持续时间 + 采样连续性判定，不依赖固定样本数；
* 心跳与历史样本分离：旧 ``sampled_at`` 的补报只入库，不改变实时在线/告警；
* 数据盘与网络速率：服务端按真实 ``sampled_at`` 时间差归一化；
* 监控分项状态：身份库 / 业务库 / Redis / Worker / scheduler；
* ``/platform/monitoring/overview`` 聚合（performance 由 task-30 模块提供，缺失时降级为空结构）。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from importlib import import_module
from typing import Any, Callable, Iterable, Sequence

import httpx
from sqlalchemy import and_, func, select, text

from app.core.config import settings
from app.db.base import utcnow
from app.modules.ops.models_center import (
    AlertEvent,
    NodeCheck,
    NodeMetricSample,
    ServerNode,
    TenantInstance,
)

logger = logging.getLogger("ventasync.ops.monitoring")

# --------------------------------------------------------------------------- 阈值
# 实时样本窗口：超过该窗口的 sampled_at 视为历史补报（不改变健康）
TELEMETRY_LIVE_WINDOW_SECONDS = 120
# 采样连续性：相邻样本间隔超过该值视为断档
SAMPLE_MAX_GAP_SECONDS = 90
CPU_ALERT_PERCENT = 85.0
CPU_ALERT_SECONDS = 10 * 60
MEMORY_ALERT_PERCENT = 90.0
MEMORY_ALERT_SECONDS = 5 * 60
# 公网探测
PROBE_TARGETS: tuple[tuple[str, str], ...] = (("root", "/"), ("api", "/readyz"))
PROBE_ALERT_CONSECUTIVE_FAILURES = 3
PROBE_NODE_BUDGET_SECONDS = 10.0
PROBE_REQUEST_TIMEOUT_SECONDS = 4.0
PROBE_CERT_TIMEOUT_SECONDS = 4.0
PROBE_RESULT_HISTORY_LIMIT = 32
# 报错类型（与 service 现有告警类型保持一致）
ALERT_ROOT = "public_unreachable"
ALERT_API = "api_unreachable"
ALERT_CPU = "cpu_high"
ALERT_MEMORY = "memory_high"
ALERT_DISK = "disk_high"
ALERT_CERT = "cert_expiring"
ALERT_AGENT = "agent_offline"
# 组件状态
COMPONENT_LABELS: dict[str, str] = {
    "identity_db": "身份库",
    "business_db": "业务库",
    "redis": "Redis",
    "worker": "Worker",
    "scheduler": "调度器",
}
HEARTBEAT_OK_SECONDS = 90
HEARTBEAT_STALE_SECONDS = 600


def _now() -> datetime:
    return utcnow()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat() + "Z"


def _aware(value: datetime | None) -> datetime | None:
    """兼容旧调用：统一转为 naive UTC（保留函数名，避免外部 import 失败）。"""
    return _utc_naive(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _utc_naive(value: datetime | None) -> datetime | None:
    """统一为 naive UTC（数据库存 UTC naive）。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        from datetime import timezone

        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# --------------------------------------------------------------------------- 样本时间/连续性
def is_historical_sample(sampled_at: datetime | None, *, now: datetime | None = None) -> bool:
    """采样时间早于实时窗口 → 历史补报（不得改变实时健康）。"""
    moment = _utc_naive(sampled_at)
    if moment is None:
        return False
    return moment < (now or _now()) - timedelta(seconds=TELEMETRY_LIVE_WINDOW_SECONDS)


def evaluate_sustained(
    samples: Sequence[tuple[datetime, float]],
    *,
    threshold: float,
    duration_seconds: int,
    now: datetime | None = None,
    max_gap_seconds: int = SAMPLE_MAX_GAP_SECONDS,
) -> dict[str, Any]:
    """按持续时间 + 采样连续性判断是否持续超过阈值（纯函数，便于测试）。

    * 不依赖固定样本数：采样间隔 40s、窗口 10min 也能判定；
    * 相邻样本间隔 > ``max_gap_seconds`` 视为断档，从新样本重新累计；
    * 最近样本必须新鲜（距 now <= ``max_gap_seconds``），历史样本不触发告警。
    """
    moment = _utc_naive(now) or _now()
    ordered = sorted(
        ((_utc_naive(t), _safe_float(v)) for t, v in samples if _utc_naive(t) is not None),
        key=lambda item: item[0],
    )
    if not ordered:
        return {
            "active": False,
            "run_seconds": 0.0,
            "sample_count": 0,
            "max_gap_seconds": 0.0,
            "last_sampled_at": None,
        }
    last_at, last_value = ordered[-1]
    fresh = (moment - last_at).total_seconds() <= max_gap_seconds
    run_start: datetime | None = None
    previous_at: datetime | None = None
    run_count = 0
    max_gap = 0.0
    for sampled_at, value in ordered:
        if value < threshold:
            run_start = None
            previous_at = sampled_at
            run_count = 0
            continue
        if run_start is None:
            run_start = sampled_at
            run_count = 1
        else:
            gap = abs((sampled_at - previous_at).total_seconds()) if previous_at else 0.0
            max_gap = max(max_gap, gap)
            if gap > max_gap_seconds:
                run_start = sampled_at
                run_count = 1
            else:
                run_count += 1
        previous_at = sampled_at
    run_seconds = max(0.0, (moment - run_start).total_seconds()) if run_start else 0.0
    active = bool(
        run_start is not None
        and fresh
        and last_value >= threshold
        and run_seconds >= duration_seconds
    )
    return {
        "active": active,
        "run_seconds": round(run_seconds, 1),
        "sample_count": run_count,
        "max_gap_seconds": round(max_gap, 1),
        "last_sampled_at": last_at,
    }


def _sustained_from_db(
    center,
    node_id: int,
    column,
    *,
    threshold: float,
    duration_seconds: int,
    now: datetime,
) -> dict[str, Any]:
    since = now - timedelta(seconds=duration_seconds + SAMPLE_MAX_GAP_SECONDS * 2)
    rows = center.execute(
        select(NodeMetricSample.sampled_at, column)
        .where(
            NodeMetricSample.node_id == node_id,
            NodeMetricSample.sampled_at >= since,
            NodeMetricSample.sampled_at <= now,
        )
        .order_by(NodeMetricSample.sampled_at.asc())
    ).all()
    samples = [(row[0], _safe_float(row[1])) for row in rows]
    return evaluate_sustained(
        samples,
        threshold=threshold,
        duration_seconds=duration_seconds,
        now=now,
    )


# --------------------------------------------------------------------------- 数据盘 / 网络速率
_BOOT_MOUNTS = frozenset({"/boot", "/boot/efi"})


def data_disk_summary(disks: Iterable[dict[str, Any]] | None) -> dict[str, Any] | None:
    """选择实际数据盘（排除 /boot、/boot/efi，优先总容量最大者）。"""
    candidates: list[dict[str, Any]] = []
    for disk in disks or []:
        if not isinstance(disk, dict):
            continue
        mount = str(disk.get("mount") or "")
        if mount in _BOOT_MOUNTS:
            continue
        total = _safe_float(disk.get("total_gb"))
        candidates.append({**disk, "mount": mount, "total_gb": total})
    if not candidates:
        return None
    chosen = max(candidates, key=lambda item: item["total_gb"])
    total = _safe_float(chosen.get("total_gb"))
    percent = _safe_float(chosen.get("percent"))
    used = round(total * percent / 100.0, 1) if total else 0.0
    free = round(max(0.0, total - used), 1)
    return {
        "mount": chosen.get("mount") or "",
        "total_gb": round(total, 1),
        "used_gb": used,
        "free_gb": free,
        "percent": round(percent, 2),
    }


def network_with_actual_rates(
    network: dict[str, Any] | None,
    previous_network: dict[str, Any] | None,
    interval_seconds: float | None,
) -> dict[str, Any]:
    """按真实采样时间差计算网络速率（覆盖 Agent 按固定 30s 估算的值）。"""
    result = dict(network) if isinstance(network, dict) else {}
    interval = _safe_float(interval_seconds)
    if previous_network and interval > 0:
        try:
            rx_now = int(result.get("rx_bytes") or 0)
            tx_now = int(result.get("tx_bytes") or 0)
            rx_prev = int(previous_network.get("rx_bytes") or 0)
            tx_prev = int(previous_network.get("tx_bytes") or 0)
        except (TypeError, ValueError):
            rx_now = tx_now = rx_prev = tx_prev = 0
        if rx_now or rx_prev or tx_now or tx_prev:
            result["rx_bytes_per_sec"] = round(max(0, rx_now - rx_prev) / interval, 2)
            result["tx_bytes_per_sec"] = round(max(0, tx_now - tx_prev) / interval, 2)
            result["interval_seconds"] = round(interval, 1)
    elif interval > 0:
        result["interval_seconds"] = round(interval, 1)
    return result


# --------------------------------------------------------------------------- 告警写入
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


# --------------------------------------------------------------------------- 指标入库
def ingest_telemetry(center, node: ServerNode, payload, *, now: datetime | None = None) -> dict[str, Any]:
    """指标入库：实时样本更新健康/告警；历史补报只入库不改变实时健康。"""
    moment = now or _now()
    sampled_at = _utc_naive(getattr(payload, "sampled_at", None)) or moment
    live = not is_historical_sample(sampled_at, now=moment)

    previous = center.execute(
        select(NodeMetricSample)
        .where(
            NodeMetricSample.node_id == node.id,
            NodeMetricSample.sampled_at < sampled_at,
        )
        .order_by(NodeMetricSample.sampled_at.desc(), NodeMetricSample.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    interval_seconds = (
        (sampled_at - previous.sampled_at).total_seconds() if previous is not None else None
    )
    network = network_with_actual_rates(
        getattr(payload, "network", None),
        previous.network if previous is not None else None,
        interval_seconds,
    )
    disks = list(getattr(payload, "disks", None) or [])
    disk = data_disk_summary(disks)

    if live:
        node.hostname = getattr(payload, "hostname", "") or node.hostname
        node.os_name = getattr(payload, "os_name", "") or node.os_name
        node.arch = getattr(payload, "arch", "") or node.arch
        node.cpu_cores = _safe_int(getattr(payload, "cpu_cores", 0)) or node.cpu_cores
        node.memory_total_mb = _safe_int(getattr(payload, "memory_total_mb", 0)) or node.memory_total_mb
        if disk is not None:
            node.disk_total_gb = int(round(_safe_float(disk.get("total_gb"))))
        if getattr(payload, "internal_ips", None):
            node.internal_ips = payload.internal_ips
        if getattr(payload, "public_ip", ""):
            node.observed_public_ip = payload.public_ip
        if getattr(payload, "agent_version", ""):
            node.agent_version = payload.agent_version
        if getattr(payload, "executor_version", ""):
            node.executor_version = payload.executor_version
        if getattr(payload, "protocol_version", 0):
            node.protocol_version = _safe_int(payload.protocol_version)
        if getattr(payload, "capabilities", None):
            node.capabilities_json = list(payload.capabilities)
        if getattr(payload, "db_version", ""):
            node.db_version = payload.db_version
        if getattr(payload, "app_version", ""):
            node.app_version = payload.app_version
        node.status = "online"
        node.last_seen_at = moment
        node.last_metrics_at = moment
        instance = center.get(TenantInstance, node.instance_id)
        if instance is not None:
            if getattr(payload, "app_version", ""):
                instance.actual_version = payload.app_version
                if not instance.app_version:
                    instance.app_version = payload.app_version
            if getattr(payload, "db_version", ""):
                instance.db_version = payload.db_version
            if getattr(payload, "agent_version", ""):
                instance.agent_version = payload.agent_version
            if getattr(payload, "executor_version", ""):
                instance.executor_version = payload.executor_version
            if instance.status == "pending":
                instance.status = "active"
            instance.updated_at = moment
    node.updated_at = moment

    sample = NodeMetricSample(
        node_id=node.id,
        sampled_at=sampled_at,
        received_at=moment,
        cpu_percent=_safe_float(getattr(payload, "cpu_percent", 0)),
        load1=_safe_float(getattr(payload, "load1", 0)),
        load5=_safe_float(getattr(payload, "load5", 0)),
        load15=_safe_float(getattr(payload, "load15", 0)),
        memory_total_mb=_safe_int(getattr(payload, "memory_total_mb", 0)),
        memory_used_mb=_safe_int(getattr(payload, "memory_used_mb", 0)),
        memory_available_mb=_safe_int(getattr(payload, "memory_available_mb", 0)),
        memory_percent=_safe_float(getattr(payload, "memory_percent", 0)),
        swap_total_mb=_safe_int(getattr(payload, "swap_total_mb", 0)),
        swap_used_mb=_safe_int(getattr(payload, "swap_used_mb", 0)),
        disks=disks,
        network=network,
        services=getattr(payload, "services", None) or {},
        public_ip=getattr(payload, "public_ip", ""),
    )
    center.add(sample)

    cpu_result: dict[str, Any] | None = None
    memory_result: dict[str, Any] | None = None
    if live:
        center.flush()
        _resolve_alerts(center, node, (ALERT_AGENT,), moment)
        cpu_result = _sustained_from_db(
            center,
            node.id,
            NodeMetricSample.cpu_percent,
            threshold=CPU_ALERT_PERCENT,
            duration_seconds=CPU_ALERT_SECONDS,
            now=moment,
        )
        cpu_now = _safe_float(getattr(payload, "cpu_percent", 0))
        if cpu_result["active"]:
            _upsert_alert(
                center,
                node,
                ALERT_CPU,
                "critical" if cpu_now >= 95 else "warning",
                f"CPU 使用率持续 {CPU_ALERT_SECONDS // 60} 分钟过高：{cpu_now:.1f}%",
                f"{cpu_now:.1f}%",
                f"{CPU_ALERT_PERCENT:.0f}% / {CPU_ALERT_SECONDS // 60}分钟",
                moment,
            )
        elif cpu_now < CPU_ALERT_PERCENT:
            _resolve_alerts(center, node, (ALERT_CPU,), moment)

        memory_result = _sustained_from_db(
            center,
            node.id,
            NodeMetricSample.memory_percent,
            threshold=MEMORY_ALERT_PERCENT,
            duration_seconds=MEMORY_ALERT_SECONDS,
            now=moment,
        )
        memory_now = _safe_float(getattr(payload, "memory_percent", 0))
        if memory_result["active"]:
            _upsert_alert(
                center,
                node,
                ALERT_MEMORY,
                "critical",
                f"内存使用率持续 {MEMORY_ALERT_SECONDS // 60} 分钟过高：{memory_now:.1f}%",
                f"{memory_now:.1f}%",
                f"{MEMORY_ALERT_PERCENT:.0f}% / {MEMORY_ALERT_SECONDS // 60}分钟",
                moment,
            )
        elif memory_now < MEMORY_ALERT_PERCENT:
            _resolve_alerts(center, node, (ALERT_MEMORY,), moment)

        disk_percent = _safe_float(disk.get("percent")) if disk else 0.0
        if disk_percent >= 95:
            _upsert_alert(
                center,
                node,
                ALERT_DISK,
                "critical",
                f"数据盘使用率过高：{disk_percent:.1f}%（{disk.get('mount', '')}）",
                f"{disk_percent:.1f}%",
                "95%",
                moment,
            )
        elif disk_percent >= 85:
            _upsert_alert(
                center,
                node,
                ALERT_DISK,
                "warning",
                f"数据盘使用率预警：{disk_percent:.1f}%（{disk.get('mount', '')}）",
                f"{disk_percent:.1f}%",
                "85%",
                moment,
            )
        else:
            _resolve_alerts(center, node, (ALERT_DISK,), moment)

    center.commit()
    return {
        "ok": True,
        "node_id": str(node.id),
        "next_report_seconds": 30,
        "server_time": _iso(moment),
        "live": live,
        "historical": not live,
        "sampled_at": _iso(sampled_at),
        "data_disk": disk,
        "network_interval_seconds": round(interval_seconds, 1) if interval_seconds else None,
        "cpu_sustained": cpu_result,
        "memory_sustained": memory_result,
    }


# --------------------------------------------------------------------------- 公网探测
def _probe_ok(target: str, status_code: int | None) -> bool:
    if status_code is None:
        return False
    if target == "api":
        return 200 <= status_code < 300
    return 200 <= status_code < 400


def _default_http_get(url: str, *, timeout: float, follow_redirects: bool = False):
    return httpx.get(url, timeout=timeout, follow_redirects=follow_redirects)


def _cert_days_left(domain: str, *, timeout: float = PROBE_CERT_TIMEOUT_SECONDS) -> int | None:
    import socket
    import ssl

    try:
        context = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=max(1.0, timeout)) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        not_after = cert.get("notAfter")
        if not not_after:
            return None
        return int((ssl.cert_time_to_seconds(not_after) - time.time()) // 86400)
    except (OSError, ssl.SSLError, ValueError):
        return None


def consecutive_failures(center, node_id: int, target: str, *, limit: int = PROBE_RESULT_HISTORY_LIMIT) -> int:
    """某目标最近连续失败次数（按该目标自己的历史，不与其它目标交叉）。"""
    rows = center.execute(
        select(NodeCheck.ok)
        .where(NodeCheck.node_id == int(node_id), NodeCheck.target == target)
        .order_by(NodeCheck.id.desc())
        .limit(max(1, int(limit)))
    ).scalars().all()
    failures = 0
    for ok in rows:
        if ok:
            break
        failures += 1
    return failures


def _latest_checks(center, node_id: int, target: str, *, limit: int = PROBE_RESULT_HISTORY_LIMIT) -> list[NodeCheck]:
    return list(
        center.execute(
            select(NodeCheck)
            .where(NodeCheck.node_id == int(node_id), NodeCheck.target == target)
            .order_by(NodeCheck.id.desc())
            .limit(max(1, int(limit)))
        ).scalars().all()
    )


def probe_node(
    center,
    node_id: int,
    *,
    now: datetime | None = None,
    http_get: Callable[..., Any] | None = None,
    cert_days: Callable[..., int | None] | None = None,
    budget_seconds: float = PROBE_NODE_BUDGET_SECONDS,
) -> dict[str, Any]:
    """探测单个节点：首页 root 与 API api 分开记录、分开连续失败、独立告警。"""
    from app.modules.ops.service import get_instance  # 延迟导入，避免模块循环

    moment = now or _now()
    node = center.get(ServerNode, int(node_id))
    if node is None:
        from app.core.errors import AppError, ErrorCode

        raise AppError(ErrorCode.NOT_FOUND, "节点不存在")
    instance = get_instance(center, node.instance_id)
    getter = http_get or _default_http_get
    deadline = time.monotonic() + max(0.01, float(budget_seconds))
    checks: list[NodeCheck] = []

    for target, path in PROBE_TARGETS:
        url = f"https://{instance.domain}{path}"
        started = _now()
        ok = False
        status_code: int | None = None
        error = ""
        remaining = deadline - time.monotonic()
        if remaining <= 0.2:
            error = "探测预算耗尽（10s）"
        else:
            timeout = max(0.5, min(PROBE_REQUEST_TIMEOUT_SECONDS, remaining))
            try:
                response = getter(url, timeout=timeout, follow_redirects=False)
                status_code = int(getattr(response, "status_code", 0) or 0)
                ok = _probe_ok(target, status_code)
                if not ok:
                    error = f"HTTP {status_code}"
            except httpx.HTTPError as exc:
                error = str(exc)[:255]
            except Exception as exc:  # noqa: BLE001 - 探测量不能影响调度
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
            cert_days_left=None,
            error=error,
        )
        center.add(row)
        checks.append(row)
    center.flush()

    cert_reader = cert_days or _cert_days_left
    remaining = deadline - time.monotonic()
    cert_value = None
    if remaining > 0.5:
        try:
            cert_value = cert_reader(instance.domain, timeout=min(PROBE_CERT_TIMEOUT_SECONDS, remaining))
        except Exception:  # noqa: BLE001
            cert_value = None
    for row in checks:
        row.cert_days_left = cert_value

    target_summary: dict[str, dict[str, Any]] = {}
    root_ok = False
    api_ok = False
    for target, _path in PROBE_TARGETS:
        recent = _latest_checks(center, node.id, target)
        latest = recent[0] if recent else None
        failures = consecutive_failures(center, node.id, target)
        target_ok = bool(latest.ok) if latest is not None else False
        if target == "root":
            root_ok = target_ok
            if latest is not None and target_ok:
                _resolve_alerts(center, node, (ALERT_ROOT,), moment)
            elif failures >= PROBE_ALERT_CONSECUTIVE_FAILURES:
                _upsert_alert(
                    center,
                    node,
                    ALERT_ROOT,
                    "critical",
                    f"首页连续 {failures} 次公网探测失败",
                    "failed",
                    f"连续{PROBE_ALERT_CONSECUTIVE_FAILURES}次",
                    moment,
                )
        else:
            api_ok = target_ok
            if latest is not None and target_ok:
                _resolve_alerts(center, node, (ALERT_API,), moment)
            elif failures >= PROBE_ALERT_CONSECUTIVE_FAILURES:
                message = f"API 接口连续 {failures} 次探测失败"
                if root_ok:
                    message += "（首页正常，API 独立不可用）"
                _upsert_alert(
                    center,
                    node,
                    ALERT_API,
                    "critical",
                    message,
                    "failed",
                    f"连续{PROBE_ALERT_CONSECUTIVE_FAILURES}次",
                    moment,
                )
        target_summary[target] = {
            "target": target,
            "url": f"https://{instance.domain}{_path}",
            "ok": target_ok,
            "status_code": int(latest.status_code) if latest is not None and latest.status_code is not None else None,
            "latency_ms": int(latest.latency_ms) if latest is not None else None,
            "consecutive_failures": failures,
            "checked_at": _iso(latest.checked_at) if latest is not None else None,
        }

    if cert_value is not None and cert_value < 14:
        _upsert_alert(
            center,
            node,
            ALERT_CERT,
            "critical" if cert_value < 7 else "warning",
            f"HTTPS 证书剩余 {cert_value} 天",
            str(cert_value),
            "14天",
            moment,
        )
    elif cert_value is not None:
        _resolve_alerts(center, node, (ALERT_CERT,), moment)

    center.commit()
    return {
        "node_id": str(node.id),
        "instance_id": str(instance.id),
        "domain": instance.domain,
        "targets": target_summary,
        "home_ok": root_ok,
        "api_ok": api_ok,
        "api_consecutive_failures": target_summary["api"]["consecutive_failures"],
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


def public_probe_summary(center, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """所有节点按 root/api 目标汇总的最新探测状态（供 overview）。"""
    moment = now or _now()
    subquery = (
        select(NodeCheck.target, NodeCheck.node_id, func.max(NodeCheck.id).label("latest_id"))
        .where(NodeCheck.target.in_([item[0] for item in PROBE_TARGETS]))
        .group_by(NodeCheck.target, NodeCheck.node_id)
        .subquery()
    )
    rows = center.execute(
        select(NodeCheck).join(
            subquery, and_(NodeCheck.id == subquery.c.latest_id)
        )
    ).scalars().all()
    result: list[dict[str, Any]] = []
    for target, _path in PROBE_TARGETS:
        items = [row for row in rows if row.target == target]
        failures = [row for row in items if not row.ok]
        latencies = [int(row.latency_ms or 0) for row in items if row.ok]
        if not items:
            status = "unknown"
        elif failures:
            status = "failed"
        else:
            status = "ok"
        result.append(
            {
                "target": target,
                "status": status,
                "latency_ms": int(sum(latencies) / len(latencies)) if latencies else None,
                "ok_nodes": len(items) - len(failures),
                "failed_nodes": len(failures),
                "total_nodes": len(items),
                "checked_at": _iso(max((row.checked_at for row in items), default=None)),
            }
        )
    return result


# --------------------------------------------------------------------------- 分项状态
def _ping_center(center) -> None:
    center.execute(text("SELECT 1"))


def _ping_redis() -> None:
    from app.core.redis import cache_redis

    cache_redis().ping()


def _business_db_status(center) -> tuple[str, str]:
    """业务库：tenant 模式绑定单一租户；中心模式未绑定单一租户。"""
    tenant_id = int(getattr(settings, "tenant_id", 0) or 0)
    if tenant_id <= 0:
        return "unknown", "中心模式未绑定单一租户，业务库按租户独立"
    from app.db.session import tenant_session
    from app.modules.identity.service import load_tenant_db_route

    route = load_tenant_db_route(center, tenant_id)
    if route is None:
        return "error", f"租户 {tenant_id} 数据库连接映射不可用"
    with tenant_session(route) as db:
        db.execute(text("SELECT 1"))
    return "ok", f"租户 {tenant_id} 业务库连接正常"


def _component_heartbeat(component: str) -> dict[str, Any]:
    try:
        from app.workers.monitor import component_heartbeat_status

        return component_heartbeat_status(component)
    except Exception:  # noqa: BLE001 - 监控模块未就绪/ Redis 不可用
        return {
            "status": "unknown",
            "message": "心跳状态不可用",
            "last_heartbeat_at": None,
            "age_seconds": None,
        }


def _component(
    name: str,
    status: str,
    message: str,
    *,
    latency_ms: int | None = None,
    last_heartbeat_at: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": COMPONENT_LABELS.get(name, name),
        "status": status,
        "message": message,
        "latency_ms": latency_ms,
        "last_heartbeat_at": last_heartbeat_at,
    }


def components_status(
    center,
    *,
    now: datetime | None = None,
    center_ping: Callable[[Any], None] | None = None,
    business_db_status: Callable[[Any], tuple[str, str]] | None = None,
    redis_ping: Callable[[], None] | None = None,
    heartbeat_status: Callable[[str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """分项状态：身份库 / 业务库 / Redis / Worker / scheduler。"""
    moment = now or _now()
    ping_center = center_ping or _ping_center
    ping_redis = redis_ping or _ping_redis
    business = business_db_status or _business_db_status
    heartbeat = heartbeat_status or _component_heartbeat

    components: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        ping_center(center)
        components.append(
            _component("identity_db", "ok", "身份库连接正常", latency_ms=int((time.monotonic() - started) * 1000))
        )
    except Exception as exc:  # noqa: BLE001
        components.append(_component("identity_db", "error", f"身份库不可用：{str(exc)[:200]}"))

    started = time.monotonic()
    try:
        status, message = business(center)
        components.append(
            _component(
                "business_db",
                status,
                message,
                latency_ms=int((time.monotonic() - started) * 1000) if status == "ok" else None,
            )
        )
    except Exception as exc:  # noqa: BLE001
        components.append(_component("business_db", "error", f"业务库不可用：{str(exc)[:200]}"))

    started = time.monotonic()
    try:
        ping_redis()
        components.append(
            _component("redis", "ok", "Redis 连接正常", latency_ms=int((time.monotonic() - started) * 1000))
        )
    except Exception as exc:  # noqa: BLE001
        components.append(_component("redis", "error", f"Redis 不可用：{str(exc)[:200]}"))

    for name in ("worker", "scheduler"):
        try:
            info = heartbeat(name)
            status = str(info.get("status") or "unknown")
            components.append(
                _component(
                    name,
                    status,
                    str(info.get("message") or ""),
                    last_heartbeat_at=info.get("last_heartbeat_at"),
                )
            )
        except Exception as exc:  # noqa: BLE001
            components.append(_component(name, "unknown", f"心跳状态不可用：{str(exc)[:200]}"))
    return components


def overall_status(components: Sequence[dict[str, Any]]) -> str:
    by_name = {item.get("name"): item.get("status") for item in components}
    if by_name.get("identity_db") != "ok" or by_name.get("redis") != "ok":
        return "unhealthy"
    degraded = False
    for name in ("business_db", "worker", "scheduler"):
        if by_name.get(name) not in (None, "ok"):
            degraded = True
    return "degraded" if degraded else "healthy"


# --------------------------------------------------------------------------- performance 降级读取
def _module_overview(
    module_names: Sequence[str],
    function_names: Sequence[str],
    default: dict[str, Any],
) -> dict[str, Any]:
    for module_name in module_names:
        try:
            module = import_module(module_name)
        except ImportError:
            continue
        for function_name in function_names:
            function = getattr(module, function_name, None)
            if not callable(function):
                continue
            try:
                result = function()
            except TypeError:
                continue
            except Exception:  # noqa: BLE001 - 指标模块故障不影响 overview
                logger.exception("读取性能指标失败：%s.%s", module_name, function_name)
                continue
            if isinstance(result, dict):
                merged = dict(default)
                merged.update(result)
                merged["available"] = True
                return merged
    return dict(default)


PERFORMANCE_DEFAULT: dict[str, Any] = {
    "request_count": 0,
    "error_count": 0,
    "error_5xx": 0,
    "rate_5xx": 0.0,
    "p95_ms": 0.0,
    "p99_ms": 0.0,
    "window_seconds": 900,
    "routes": [],
    "available": False,
}
BUSINESS_REQUEST_DEFAULT: dict[str, Any] = {
    "request_count": 0,
    "error_count": 0,
    "error_5xx": 0,
    "rate_5xx": 0.0,
    "p95_ms": 0.0,
    "p99_ms": 0.0,
    "window_seconds": 900,
    "available": False,
}


def performance_overview() -> dict[str, Any]:
    return _module_overview(
        ("app.modules.ops.performance", "app.core.performance"),
        ("performance_overview", "request_performance_overview", "overview"),
        PERFORMANCE_DEFAULT,
    )


def business_requests_overview() -> dict[str, Any]:
    return _module_overview(
        ("app.modules.ops.performance", "app.core.performance"),
        ("business_requests_overview", "business_overview", "performance_overview"),
        BUSINESS_REQUEST_DEFAULT,
    )


def performance_parts() -> dict[str, Any]:
    """优先使用 task-30 的 ``performance.monitoring_overview_parts``（窗口一致）。"""
    try:
        from app.modules.ops.performance import monitoring_overview_parts

        parts = monitoring_overview_parts()
    except Exception:  # noqa: BLE001 - 性能模块未就绪/Redis 不可用不影响监控
        parts = None
    if isinstance(parts, dict):
        performance = parts.get("performance")
        business = parts.get("business_requests")
        if isinstance(performance, dict) and isinstance(business, dict):
            return {
                "performance": {**PERFORMANCE_DEFAULT, **performance, "available": True},
                "business_requests": {
                    **BUSINESS_REQUEST_DEFAULT,
                    **business,
                    "available": True,
                },
            }
    return {
        "performance": performance_overview(),
        "business_requests": business_requests_overview(),
    }


def monitoring_overview(center, *, now: datetime | None = None) -> dict[str, Any]:
    """`GET /platform/monitoring/overview` 的数据聚合。"""
    moment = now or _now()
    components = components_status(center, now=moment)
    parts = performance_parts()
    return {
        "overall": overall_status(components),
        "checked_at": _iso(moment),
        "components": components,
        "performance": parts["performance"],
        "public_probes": public_probe_summary(center, now=moment),
        "business_requests": parts["business_requests"],
    }


__all__ = [
    "TELEMETRY_LIVE_WINDOW_SECONDS",
    "SAMPLE_MAX_GAP_SECONDS",
    "PROBE_TARGETS",
    "PROBE_ALERT_CONSECUTIVE_FAILURES",
    "PROBE_NODE_BUDGET_SECONDS",
    "ALERT_ROOT",
    "ALERT_API",
    "ALERT_CPU",
    "ALERT_MEMORY",
    "ALERT_DISK",
    "ALERT_CERT",
    "is_historical_sample",
    "evaluate_sustained",
    "data_disk_summary",
    "network_with_actual_rates",
    "ingest_telemetry",
    "probe_node",
    "consecutive_failures",
    "public_probe_summary",
    "components_status",
    "overall_status",
    "performance_overview",
    "business_requests_overview",
    "monitoring_overview",
]
