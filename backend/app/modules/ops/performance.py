"""请求性能指标查询与公网探测耗时查询（运维中心页面数据源）。

数据来源：

* 业务请求：``app.core.request_metrics`` 中间件写入的 Redis 分钟桶直方图；
* 公网探测：``node_checks`` 表（探测进程按节点记录 root/healthz/api 的耗时与结果）。

设计约束（task-30）：

* 只读，不写库、不改表；Redis 不可用时降级到进程内最近窗口样本并标记 ``degraded``；
* P95/P99 一律由直方图桶插值计算，不保存逐请求样本；
* 公网探测耗时（``/platform/nodes/{id}/probe-latency``）与业务请求耗时
  （``/platform/metrics/http``）分离，页面分别展示；
* 字段名是前后端契约，改动需同步 Lead / 前端。
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select

from app.core import request_metrics as core_metrics
from app.core.config import settings
from app.core.errors import AppError, ErrorCode

logger = logging.getLogger("ventasync.ops.performance")

DEFAULT_WINDOW_SECONDS = 15 * 60
MAX_WINDOW_SECONDS = 24 * 60 * 60
MAX_ROUTES_IN_PAYLOAD = 50
MAX_PROBE_SERIES_POINTS = 500


# --------------------------------------------------------------------------- 工具
def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_window(window_seconds: int | None) -> int:
    try:
        value = int(window_seconds or settings.request_metrics_window_seconds or DEFAULT_WINDOW_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_WINDOW_SECONDS
    return max(60, min(value, MAX_WINDOW_SECONDS))


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(max(0.0, numerator) / denominator, 4)


def _round_ms(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


# --------------------------------------------------------------------------- 数据读取
def _load_window_increments(window_seconds: int) -> tuple[dict[int, dict[str, int]], bool, str]:
    """读取窗口内指标；返回 ``(increments, degraded, degraded_reason)``。"""
    collector = core_metrics.get_collector()
    try:
        # 先落盘队列内最新样本，避免页面比实际延迟一个刷新周期（失败则忽略）
        collector.flush()
    except Exception:  # noqa: BLE001 - Redis 不可用时后面回落到内存
        pass
    try:
        from app.core.redis import cache_redis

        increments = core_metrics.read_increments(
            cache_redis(), window_seconds=window_seconds
        )
        return increments, False, ""
    except Exception as exc:  # noqa: BLE001 - fail-open，降级本地内存
        logger.warning("读取请求指标失败，降级本地内存样本：%s", str(exc)[:160])
        minutes = max(1, (int(window_seconds) + 59) // 60)
        return collector.memory_increments(minutes=minutes), True, "metrics_store_unavailable"


# --------------------------------------------------------------------------- 窗口聚合
def _empty_route_stats() -> dict[str, Any]:
    return {
        "counts": [0] * (len(core_metrics.HISTOGRAM_BUCKET_BOUNDS_MS) + 1),
        "status_counts": {name: 0 for name in core_metrics.STATUS_CLASSES},
        "sum_ms": 0,
        "slow": 0,
    }


def _collect_route_stats(
    increments: Mapping[int, Mapping[str, int]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """把 Redis 字段增量还原为 ``(method, route) -> 统计``。"""
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for fields in increments.values():
        for field, value in fields.items():
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            decoded = core_metrics.decode_field(field)
            if decoded is None:
                continue
            kind, parts = decoded
            if kind == "count":
                method, status, route, bucket_raw = parts
                try:
                    bucket = int(bucket_raw)
                except ValueError:
                    continue
                if bucket < 0 or bucket >= len(core_metrics.HISTOGRAM_BUCKET_BOUNDS_MS) + 1:
                    continue
                target = stats.setdefault((method, route), _empty_route_stats())
                target["counts"][bucket] += count
                target["status_counts"][status] = target["status_counts"].get(status, 0) + count
            elif kind == "sum":
                method, route = parts
                target = stats.setdefault((method, route), _empty_route_stats())
                target["sum_ms"] += count
            elif kind == "slow":
                method, route = parts
                target = stats.setdefault((method, route), _empty_route_stats())
                target["slow"] += count
    return stats


def _merge_stats(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    merged = _empty_route_stats()
    for item in items:
        counts = item.get("counts") or []
        for index, value in enumerate(counts[: len(merged["counts"])]):
            merged["counts"][index] += int(value or 0)
        for key, value in (item.get("status_counts") or {}).items():
            if key in merged["status_counts"]:
                merged["status_counts"][key] += int(value or 0)
        merged["sum_ms"] += int(item.get("sum_ms") or 0)
        merged["slow"] += int(item.get("slow") or 0)
    return merged


def _stats_payload(stats: Mapping[str, Any], *, slow_threshold_ms: int) -> dict[str, Any]:
    counts = list(stats.get("counts") or [])
    request_count = sum(counts)
    status_counts = stats.get("status_counts") or {}
    error_4xx = int(status_counts.get("4xx") or 0)
    error_5xx = int(status_counts.get("5xx") or 0)
    sum_ms = int(stats.get("sum_ms") or 0)
    avg_ms = round(sum_ms / request_count, 1) if request_count else 0.0
    return {
        "request_count": request_count,
        "error_count": error_4xx + error_5xx,
        "error_5xx": error_5xx,
        "rate_5xx": _rate(error_5xx, request_count),
        "rate_error": _rate(error_4xx + error_5xx, request_count),
        "avg_ms": avg_ms,
        "p50_ms": _round_ms(core_metrics.histogram_quantile(0.5, counts)),
        "p95_ms": _round_ms(core_metrics.histogram_quantile(0.95, counts)),
        "p99_ms": _round_ms(core_metrics.histogram_quantile(0.99, counts)),
        "slow_count": int(stats.get("slow") or 0),
        "slow_threshold_ms": int(slow_threshold_ms),
    }


def _performance_status(payload: Mapping[str, Any]) -> str:
    """按默认阈值给出页面提示级别（不创建节点告警，节点告警由采集侧负责）。"""
    if int(payload.get("request_count") or 0) < int(settings.alert_request_min_requests or 0):
        return "unknown"
    p95 = payload.get("p95_ms")
    rate_5xx = float(payload.get("rate_5xx") or 0.0)
    if rate_5xx >= float(settings.alert_request_5xx_rate) * 2:
        return "critical"
    if p95 is not None and float(p95) >= float(settings.alert_request_p95_ms) * 2:
        return "critical"
    if rate_5xx >= float(settings.alert_request_5xx_rate):
        return "warning"
    if p95 is not None and float(p95) >= float(settings.alert_request_p95_ms):
        return "warning"
    return "ok"


def _build_overview(
    *,
    window_seconds: int,
    route: str | None = None,
    method: str | None = None,
    business_only: bool = False,
) -> dict[str, Any]:
    window = _normalize_window(window_seconds)
    increments, degraded, degraded_reason = _load_window_increments(window)
    route_stats = _collect_route_stats(increments)
    route_filter = (route or "").strip()
    method_filter = (method or "").strip().upper()
    selected = [
        (key, value)
        for key, value in route_stats.items()
        if (not route_filter or key[1] == route_filter)
        and (not method_filter or key[0] == method_filter)
        and (not business_only or core_metrics.is_business_route(key[1]))
    ]
    aggregate = _stats_payload(
        _merge_stats(value for _, value in selected),
        slow_threshold_ms=settings.request_metrics_slow_ms,
    )
    routes = [
        {
            "route": key[1],
            "method": key[0],
            **_stats_payload(value, slow_threshold_ms=settings.request_metrics_slow_ms),
        }
        for key, value in selected
    ]
    routes.sort(key=lambda item: (-int(item["request_count"]), item["route"], item["method"]))
    route_count = len(routes)
    payload = {
        "window_seconds": window,
        "generated_at": _iso(_utc_now()),
        "enabled": bool(settings.request_metrics_enabled),
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        **aggregate,
        "status": _performance_status(aggregate),
        "thresholds": {
            "p95_ms": int(settings.alert_request_p95_ms),
            "rate_5xx": float(settings.alert_request_5xx_rate),
            "min_requests": int(settings.alert_request_min_requests),
        },
        "route_count": route_count,
        "routes": routes[:MAX_ROUTES_IN_PAYLOAD],
    }
    return payload


# --------------------------------------------------------------------------- 对外接口
def performance_overview(window_seconds: int | None = None) -> dict[str, Any]:
    """全部业务 API 的请求量 / 5xx / 延迟分位数（供 overview 与独立接口复用）。"""
    return _build_overview(window_seconds=window_seconds or DEFAULT_WINDOW_SECONDS)


def business_requests_overview(window_seconds: int | None = None) -> dict[str, Any]:
    """业务请求（排除平台管理 / Agent / 监控自身）的请求耗时；与公网探测耗时分开。"""
    return _build_overview(
        window_seconds=window_seconds or DEFAULT_WINDOW_SECONDS, business_only=True
    )


def http_metrics(
    *,
    window_seconds: int | None = None,
    route: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """``GET /platform/metrics/http`` 的数据体（支持按路由模板 / 方法过滤）。"""
    return _build_overview(
        window_seconds=window_seconds or settings.request_metrics_window_seconds,
        route=route,
        method=method,
    )


def monitoring_overview_parts(
    center: Any = None, window_seconds: int | None = None  # noqa: ARG001 - 契约占位，性能指标不读库
) -> dict[str, Any]:
    """供 ``/platform/monitoring/overview`` 汇总使用的 performance / business_requests。

    ``center`` 参数只为与其他 overview 数据源保持一致的调用方式，本函数不读库。
    """
    return {
        "performance": performance_overview(window_seconds),
        "business_requests": business_requests_overview(window_seconds),
    }


# --------------------------------------------------------------------------- 公网探测耗时
def _probe_payload(
    rows: Iterable[Any], *, domain: str, node_id: int, instance_id: int, hours: int
) -> dict[str, Any]:
    grouped: "OrderedDict[str, list[Any]]" = OrderedDict()
    for row in rows:
        grouped.setdefault(str(row.target or ""), []).append(row)
    now = _utc_now()
    targets: list[dict[str, Any]] = []
    series: list[dict[str, Any]] = []
    for target, items in grouped.items():
        items.sort(key=lambda item: item.checked_at or now)
        latencies = [float(item.latency_ms or 0) for item in items]
        ok_count = sum(1 for item in items if bool(item.ok))
        last = items[-1]
        targets.append(
            {
                "target": target,
                "url": str(last.url or ""),
                "samples": len(items),
                "ok_count": ok_count,
                "ok_rate": _rate(ok_count, len(items)),
                "avg_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
                "p95_ms": _round_ms(core_metrics.percentile(latencies, 0.95)),
                "min_ms": int(min(latencies)) if latencies else None,
                "max_ms": int(max(latencies)) if latencies else None,
                "last_checked_at": _iso(last.checked_at),
                "last_latency_ms": int(last.latency_ms or 0),
                "last_ok": bool(last.ok),
            }
        )
        for item in items[-MAX_PROBE_SERIES_POINTS:]:
            series.append(
                {
                    "checked_at": _iso(item.checked_at),
                    "target": target,
                    "ok": bool(item.ok),
                    "latency_ms": int(item.latency_ms or 0),
                }
            )
    series.sort(key=lambda item: item["checked_at"] or "")
    return {
        "node_id": str(node_id),
        "instance_id": str(instance_id),
        "domain": domain,
        "hours": int(hours),
        "generated_at": _iso(now),
        "targets": targets,
        "series": series[-MAX_PROBE_SERIES_POINTS:],
    }


def probe_latency_overview(center: Any, node_id: int, *, hours: int = 24) -> dict[str, Any]:
    """``GET /platform/nodes/{id}/probe-latency``：公网探测耗时历史（按 target 分组）。"""
    from app.modules.ops.models_center import NodeCheck, ServerNode, TenantInstance

    node = center.get(ServerNode, int(node_id))
    if node is None:
        raise AppError(ErrorCode.NOT_FOUND, "节点不存在")
    instance = center.get(TenantInstance, node.instance_id)
    domain = instance.domain if instance is not None else (node.configured_public_ip or "")
    instance_id = int(instance.id) if instance is not None else int(node.instance_id or 0)
    span = max(1, min(int(hours), 24 * 30))
    since = _utc_now() - timedelta(hours=span)
    rows = (
        center.execute(
            select(NodeCheck)
            .where(NodeCheck.node_id == node.id, NodeCheck.checked_at >= since)
            .order_by(NodeCheck.checked_at.asc())
            .limit(20000)
        )
        .scalars()
        .all()
    )
    return _probe_payload(
        rows, domain=domain, node_id=int(node.id), instance_id=instance_id, hours=span
    )


__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "business_requests_overview",
    "http_metrics",
    "monitoring_overview_parts",
    "performance_overview",
    "probe_latency_overview",
]
