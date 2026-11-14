"""TikTok Webhook 平台侧可观测性聚合与告警（task-42）。

数据来源与边界
--------------
* **租户库**：``webhook_events``（网关转发后由租户落库的接收事件）、
  ``tiktok_order_refresh_queue``（Webhook 触发的订单定向刷新队列）、
  ``orders``（实际写入的订单）、``order_sync_checkpoints``（补拉水位）、
  ``outbox_events``（Redis 发布失败/积压的代理指标）。
  平台通过 ``load_tenant_db_route`` + ``tenant_session`` 只读聚合，不修改 channels 模块。
* **Gateway**：网关事件表在独立 SQLite（网关容器卷），中心库无法直接读取。
  网关已提供只读管理接口 ``/api/v1/gateway/admin/webhook-stats`` 与 ``/health``，
  本模块用中心配置的 ``GATEWAY_BASE_URL`` + ``GATEWAY_ADMIN_TOKEN`` 拉取
  pending/delivered/dead/unroutable/oldest 等；网关未暴露的验签失败、重复、
  P95 耗时返回 ``None`` 并标记 ``unavailable_fields``，页面按不可用展示。
* **Redis**：缓存 Redis/队列 Redis 的内存信息来自 ``INFO memory``；429 与
  Redis 发布失败在现有数据中没有独立计数，发布失败使用租户 ``outbox_events``
  failed 计数代理，429 明确标记不可用。

告警复用 ``service._upsert_alert`` / ``service._resolve_alerts`` 的合并与恢复机制；
P95 持续 5 分钟通过 Redis 记录首次命中时间，避免为告警额外建表。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select

from app.core import request_metrics as core_metrics
from app.core.config import settings

logger = logging.getLogger("ventasync.ops.webhook")

WEBHOOK_CHANNEL = "tiktok"
DEFAULT_WINDOW_SECONDS = 60 * 60
MIN_WINDOW_SECONDS = 60
MAX_WINDOW_SECONDS = 24 * 60 * 60
PENDING_STATUSES = ("pending", "retry_wait", "processing")
MAX_QUEUE_ROWS = 5000
MAX_EVENT_WINDOW_ROWS = 10000

# 告警阈值（与前端提示共用；任务未要求做成可配置项，先集中常量）
ALERT_WEBHOOK_E2E_P95 = "webhook_e2e_p95_high"
ALERT_WEBHOOK_PENDING_STALE = "webhook_pending_stale"
ALERT_WEBHOOK_DEAD = "webhook_refresh_dead"
ALERT_WEBHOOK_UNROUTABLE = "webhook_unroutable"
ALERT_WEBHOOK_REDIS = "webhook_redis_publish_failed"
WEBHOOK_ALERT_TYPES = (
    ALERT_WEBHOOK_E2E_P95,
    ALERT_WEBHOOK_PENDING_STALE,
    ALERT_WEBHOOK_DEAD,
    ALERT_WEBHOOK_UNROUTABLE,
    ALERT_WEBHOOK_REDIS,
)
E2E_P95_THRESHOLD_MS = 120_000
E2E_SUSTAIN_SECONDS = 300
E2E_MIN_SAMPLES = 5
PENDING_STALE_SECONDS = 120
LOW_TRAFFIC_MAX_EVENTS = 10
WEBHOOK_ALERT_WINDOW_SECONDS = 300
ALERT_CHECK_INTERVAL_SECONDS = 60
SUSTAIN_KEY_PREFIX = "vs:ops:webhook:sustain"


# --------------------------------------------------------------------------- 工具
def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + "Z"


def _normalize_window(value: int | None) -> int:
    try:
        parsed = int(value or DEFAULT_WINDOW_SECONDS)
    except (TypeError, ValueError):
        parsed = DEFAULT_WINDOW_SECONDS
    return max(MIN_WINDOW_SECONDS, min(parsed, MAX_WINDOW_SECONDS))


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(max(0.0, numerator) / denominator, 4)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return None


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _duration_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds() * 1000


def _summarize_queue(
    pending_rows: Sequence[Any],
    window_rows: Sequence[Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """汇总刷新队列：待刷新/最老年龄/刷新成功/端到端 P95（纯函数，便于单测）。"""
    moment = now or _utc_now()
    oldest_at: datetime | None = None
    for row in pending_rows:
        candidate = _as_datetime(_row_value(row, "updated_at")) or _as_datetime(
            _row_value(row, "created_at")
        )
        if candidate is not None and (oldest_at is None or candidate < oldest_at):
            oldest_at = candidate
    durations: list[float] = []
    completed = 0
    dead = 0
    for row in window_rows:
        status = str(_row_value(row, "status") or "").strip().lower()
        if status == "dead":
            dead += 1
            continue
        if status != "idle":
            continue
        generation = _safe_int(_row_value(row, "generation"))
        processed = _safe_int(_row_value(row, "processed_generation"))
        if processed < generation:
            continue
        completed += 1
        duration = _duration_ms(
            _as_datetime(_row_value(row, "created_at")),
            _as_datetime(_row_value(row, "updated_at")),
        )
        if duration is not None:
            durations.append(duration)
    terminal = completed + dead
    p95 = core_metrics.percentile(durations, 0.95) if durations else None
    return {
        "orders_pending": len(pending_rows),
        "oldest_pending_at": _iso(oldest_at),
        "oldest_pending_age_seconds": int(max(0.0, (moment - oldest_at).total_seconds()))
        if oldest_at is not None
        else None,
        "refresh_completed": completed,
        "orders_dead": dead,
        "refresh_success_rate": _rate(completed, terminal),
        "end_to_end_p95_ms": round(float(p95), 1) if p95 is not None else None,
        "end_to_end_samples": len(durations),
        "_e2e_durations_ms": durations,
    }


def _group_count_rows(rows: Iterable[tuple[Any, Any]]) -> dict[int, int]:
    result: dict[int, int] = {}
    for key, count in rows:
        store_id = _safe_int(key)
        result[store_id] = result.get(store_id, 0) + _safe_int(count)
    return result


def _group_queue_rows(rows: Iterable[Any]) -> dict[int, list[Any]]:
    grouped: dict[int, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[_safe_int(_row_value(row, "store_id"))].append(row)
    return grouped


def _latest_iso(values: Iterable[Any]) -> str | None:
    latest: datetime | None = None
    for value in values:
        parsed = _as_datetime(value)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return _iso(latest)


# --------------------------------------------------------------------------- Gateway
def _gateway_metrics(window_seconds: int) -> dict[str, Any]:
    """从网关现有只读管理接口取 Webhook 统计；接口缺失/令牌缺失时降级。"""
    base = str(getattr(settings, "gateway_base_url", "") or "").strip().rstrip("/")
    admin_token = str(getattr(settings, "gateway_admin_token", "") or "").strip()
    timeout = float(getattr(settings, "gateway_timeout_seconds", 5.0) or 5.0)
    payload: dict[str, Any] = {
        "available": False,
        "source": "unavailable",
        "window_seconds": int(window_seconds),
        "windowed": False,
        "reason": "",
        "unavailable_fields": [
            "signature_failed",
            "duplicate",
            "p95_ms",
        ],
        "unavailable_reason": (
            "网关 admin webhook-stats 未提供验签失败、重复事件与投递 P95；"
            "这些计数只存在于网关 SQLite，中心无读取权限"
        ),
        "instances": None,
        "received_total": None,
        "signature_failed": None,
        "duplicate": None,
        "pending": None,
        "oldest_pending_age_seconds": None,
        "delivered": None,
        "dead": None,
        "unroutable": None,
        "unroutable_recent": None,
        "retry": None,
        "leased": None,
        "due": None,
        "success_rate": None,
        "p95_ms": None,
        "last_delivery_latency_ms": None,
        "last_delivery_at": None,
        "last_status_code": None,
        "checked_at": _iso(_utc_now()),
    }
    if not base:
        payload["reason"] = "未配置 GATEWAY_BASE_URL，无法访问网关指标"
        payload["source"] = "unconfigured"
        return payload
    try:
        response = httpx.get(f"{base}/api/v1/gateway/health", timeout=timeout)
        response.raise_for_status()
        health = response.json() if response.content else {}
        if isinstance(health, dict):
            payload["instances"] = health.get("instances")
            payload["available"] = True
    except Exception as exc:  # noqa: BLE001 - 网关不可达时页面仍展示租户指标
        payload["reason"] = f"网关健康检查失败：{str(exc)[:160]}"
    if not admin_token:
        payload["reason"] = (payload["reason"] + "；" if payload["reason"] else "") + (
            "未配置 GATEWAY_ADMIN_TOKEN，无法读取 webhook-stats"
        )
        return payload
    try:
        response = httpx.get(
            f"{base}/api/v1/gateway/admin/webhook-stats",
            headers={"X-Gateway-Token": admin_token},
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.json() if response.content else {}
    except Exception as exc:  # noqa: BLE001
        payload["reason"] = (payload["reason"] + "；" if payload["reason"] else "") + (
            f"网关 webhook-stats 不可用：{str(exc)[:160]}"
        )
        return payload
    stats = body.get("webhook") if isinstance(body, dict) else None
    if not isinstance(stats, dict):
        payload["reason"] = (payload["reason"] + "；" if payload["reason"] else "") + (
            "网关 webhook-stats 响应缺少 webhook 字段"
        )
        return payload
    payload["available"] = True
    payload["source"] = "gateway_admin_webhook_stats"
    payload["received_total"] = _safe_int(
        stats.get("received_total", stats.get("received", stats.get("total")))
    )
    payload["signature_failed"] = (
        _safe_int(stats.get("signature_failed"))
        if stats.get("signature_failed") is not None
        else None
    )
    payload["duplicate"] = (
        _safe_int(stats.get("duplicate"))
        if stats.get("duplicate") is not None
        else None
    )
    payload["pending"] = _safe_int(stats.get("pending"))
    oldest_age = stats.get("oldest_pending_age_seconds", stats.get("oldest_pending"))
    payload["oldest_pending_age_seconds"] = (
        _safe_int(oldest_age) if oldest_age is not None else None
    )
    payload["delivered"] = _safe_int(stats.get("delivered"))
    payload["dead"] = _safe_int(stats.get("dead"))
    payload["unroutable"] = _safe_int(stats.get("unroutable"))
    payload["retry"] = _safe_int(stats.get("retry"))
    payload["leased"] = _safe_int(stats.get("leased"))
    payload["due"] = _safe_int(stats.get("due"))
    payload["oldest_pending_at"] = stats.get("oldest_pending_at")
    last_latency = stats.get(
        "last_delivery_latency_ms", stats.get("last_latency_ms", stats.get("latency_ms"))
    )
    payload["last_delivery_latency_ms"] = (
        _safe_int(last_latency) if last_latency is not None else None
    )
    payload["last_delivery_at"] = stats.get("last_delivery_at")
    payload["last_status_code"] = (
        _safe_int(stats.get("last_status_code"))
        if stats.get("last_status_code") is not None
        else None
    )
    payload["p95_ms"] = (
        _safe_int(stats.get("p95_ms")) if stats.get("p95_ms") is not None else None
    )
    terminal = payload["delivered"] + payload["dead"]
    payload["success_rate"] = (
        round(float(stats["success_rate"]), 4)
        if stats.get("success_rate") is not None
        else _rate(payload["delivered"], terminal)
    )
    payload["unavailable_fields"] = [
        field
        for field in ("signature_failed", "duplicate", "p95_ms")
        if payload.get(field) is None
    ]
    # 网关 audit-logs 只记录 unroutable 等少量动作；用它统计“窗口内首次 unroutable”，
    # 避免把累计 unroutable 当作持续告警条件（累计值可能永远不会回落）。
    try:
        audit_response = httpx.get(
            f"{base}/api/v1/gateway/admin/audit-logs",
            params={"limit": 500},
            headers={"X-Gateway-Token": admin_token},
            timeout=timeout,
        )
        audit_response.raise_for_status()
        audit_body = audit_response.json() if audit_response.content else {}
        audit_items = audit_body.get("items") if isinstance(audit_body, dict) else None
        if isinstance(audit_items, list):
            since = _utc_now() - timedelta(seconds=int(window_seconds))
            recent = 0
            for item in audit_items:
                if not isinstance(item, dict) or item.get("action") != "webhook.unroutable":
                    continue
                created_at = _as_datetime(item.get("created_at"))
                if created_at is not None and created_at >= since:
                    recent += 1
            payload["unroutable_recent"] = recent
            payload["unroutable_recent_source"] = "gateway_audit_logs"
    except Exception:  # noqa: BLE001 - 累计 unroutable 仍可展示，窗口值缺失
        payload["unroutable_recent"] = None
    return payload


# --------------------------------------------------------------------------- Redis
def _redis_memory(url: str, label: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "label": label,
        "used_memory_bytes": None,
        "used_memory_human": None,
        "maxmemory_bytes": None,
        "maxmemory_human": None,
        "connected_clients": None,
        "error": "",
    }
    try:
        import redis

        client = redis.Redis.from_url(
            url,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
            decode_responses=True,
        )
        info = client.info("memory")
        result.update(
            {
                "available": True,
                "used_memory_bytes": _safe_int(info.get("used_memory")),
                "used_memory_human": str(info.get("used_memory_human") or ""),
                "maxmemory_bytes": _safe_int(info.get("maxmemory")),
                "maxmemory_human": str(info.get("maxmemory_human") or ""),
            }
        )
    except Exception as exc:  # noqa: BLE001 - Redis 不可用不能阻塞监控页
        result["error"] = str(exc)[:200]
    return result


def _redis_metrics() -> dict[str, Any]:
    cache = _redis_memory(str(getattr(settings, "cache_redis_url", "") or ""), "cache")
    queue = _redis_memory(str(getattr(settings, "queue_redis_url", "") or ""), "queue")
    return {
        "available": bool(cache.get("available") or queue.get("available")),
        "cache": cache,
        "queue": queue,
        "publish_failed": 0,
        "rate_limited": None,
        "rate_limited_reason": "现有请求指标只按 1xx/2xx/3xx/4xx/5xx 聚合，未单独统计 429",
    }


# --------------------------------------------------------------------------- 租户库聚合
def _collect_tenant_metrics(
    center,
    *,
    tenant_id: int,
    tenant_name: str,
    instance_id: int,
    instance_name: str,
    instance_status: str,
    since: datetime,
    now: datetime,
    store_id: int | None = None,
) -> dict[str, Any]:
    from app.db.session import tenant_session
    from app.modules.channels.models_tenant import Store, TiktokOrderRefreshQueue
    from app.modules.identity.service import load_tenant_db_route
    from app.modules.sales.models_tenant import Order, OrderSyncCheckpoint
    from app.modules.tasks.models_tenant import OutboxEvent, WebhookEvent

    route = load_tenant_db_route(center, tenant_id)
    if route is None:
        raise RuntimeError("租户数据库路由不可用")
    event_filters = [
        WebhookEvent.tenant_id == tenant_id,
        WebhookEvent.channel == WEBHOOK_CHANNEL,
        WebhookEvent.received_at >= since,
    ]
    pending_event_filters = [
        WebhookEvent.tenant_id == tenant_id,
        WebhookEvent.channel == WEBHOOK_CHANNEL,
        WebhookEvent.status == "received",
    ]
    failed_event_filters = [
        WebhookEvent.tenant_id == tenant_id,
        WebhookEvent.channel == WEBHOOK_CHANNEL,
        WebhookEvent.status == "failed",
        WebhookEvent.received_at >= since,
    ]
    queue_filters = [TiktokOrderRefreshQueue.tenant_id == tenant_id]
    order_filters = [
        Order.tenant_id == tenant_id,
        Order.channel == WEBHOOK_CHANNEL,
        Order.updated_at >= since,
    ]
    store_filters = [Store.tenant_id == tenant_id, Store.channel == WEBHOOK_CHANNEL]
    checkpoint_filters = [
        OrderSyncCheckpoint.tenant_id == tenant_id,
        OrderSyncCheckpoint.channel == WEBHOOK_CHANNEL,
    ]
    outbox_filters = [
        OutboxEvent.tenant_id == tenant_id,
        OutboxEvent.status == "failed",
        OutboxEvent.updated_at >= since,
    ]
    if store_id is not None:
        event_filters.append(WebhookEvent.store_id == int(store_id))
        pending_event_filters.append(WebhookEvent.store_id == int(store_id))
        failed_event_filters.append(WebhookEvent.store_id == int(store_id))
        queue_filters.append(TiktokOrderRefreshQueue.store_id == int(store_id))
        order_filters.append(Order.store_id == int(store_id))
        store_filters.append(Store.id == int(store_id))
        checkpoint_filters.append(OrderSyncCheckpoint.store_id == int(store_id))
        if hasattr(OutboxEvent, "store_id"):
            outbox_filters.append(OutboxEvent.store_id == int(store_id))

    with tenant_session(route) as db:
        # 事件接收（按店铺分组）
        received_rows = db.execute(
            select(WebhookEvent.store_id, func.count())
            .where(*event_filters)
            .group_by(WebhookEvent.store_id)
        ).all()
        pending_event_rows = db.execute(
            select(WebhookEvent.store_id, func.count())
            .where(*pending_event_filters)
            .group_by(WebhookEvent.store_id)
        ).all()
        failed_event_rows = db.execute(
            select(WebhookEvent.store_id, func.count())
            .where(*failed_event_filters)
            .group_by(WebhookEvent.store_id)
        ).all()
        event_latest_rows = db.execute(
            select(WebhookEvent.store_id, func.max(WebhookEvent.received_at))
            .where(*event_filters)
            .group_by(WebhookEvent.store_id)
        ).all()
        latest_event_at = db.execute(
            select(func.max(WebhookEvent.received_at)).where(*event_filters)
        ).scalar()

        pending_queue = list(
            db.execute(
                select(TiktokOrderRefreshQueue)
                .where(
                    *queue_filters,
                    TiktokOrderRefreshQueue.status.in_(PENDING_STATUSES),
                    TiktokOrderRefreshQueue.generation
                    > TiktokOrderRefreshQueue.processed_generation,
                )
                .order_by(TiktokOrderRefreshQueue.updated_at.asc())
                .limit(MAX_QUEUE_ROWS)
            )
            .scalars()
            .all()
        )
        window_queue = list(
            db.execute(
                select(TiktokOrderRefreshQueue)
                .where(*queue_filters, TiktokOrderRefreshQueue.updated_at >= since)
                .order_by(TiktokOrderRefreshQueue.updated_at.asc())
                .limit(MAX_EVENT_WINDOW_ROWS)
            )
            .scalars()
            .all()
        )

        order_rows = db.execute(
            select(Order.store_id, func.count()).where(*order_filters).group_by(Order.store_id)
        ).all()
        store_rows = db.execute(
            select(Store.id, Store.name, Store.external_store_id).where(*store_filters)
        ).all()
        checkpoint_rows = db.execute(
            select(OrderSyncCheckpoint.store_id, OrderSyncCheckpoint.watermark).where(
                *checkpoint_filters
            )
        ).all()
        outbox_failed = _safe_int(
            db.execute(select(func.count()).select_from(OutboxEvent).where(*outbox_filters)).scalar_one()
        )

    received_by_store = _group_count_rows(received_rows)
    pending_events_by_store = _group_count_rows(pending_event_rows)
    failed_events_by_store = _group_count_rows(failed_event_rows)
    orders_by_store = _group_count_rows(order_rows)
    latest_event_by_store = {
        _safe_int(key): _iso(_as_datetime(value)) for key, value in event_latest_rows
    }
    pending_queue_by_store = _group_queue_rows(pending_queue)
    window_queue_by_store = _group_queue_rows(window_queue)
    store_meta = {
        _safe_int(row[0]): {
            "store_name": str(row[1] or ""),
            "external_store_id": str(row[2] or ""),
        }
        for row in store_rows
    }
    checkpoints = {
        _safe_int(row[0]): _iso(_as_datetime(row[1])) for row in checkpoint_rows if row[1]
    }
    store_ids = set(store_meta)
    store_ids.update(received_by_store)
    store_ids.update(pending_queue_by_store)
    store_ids.update(window_queue_by_store)
    store_ids.update(orders_by_store)
    store_ids.update(checkpoints)
    store_ids.discard(0)
    if store_id is not None:
        store_ids = {int(store_id)}

    store_payloads: list[dict[str, Any]] = []
    for current_store_id in sorted(store_ids):
        summary = _summarize_queue(
            pending_queue_by_store.get(current_store_id, []),
            window_queue_by_store.get(current_store_id, []),
            now=now,
        )
        meta = store_meta.get(current_store_id, {})
        store_payloads.append(
            {
                "store_id": str(current_store_id),
                "store_name": meta.get("store_name", ""),
                "external_store_id": meta.get("external_store_id", ""),
                "events_received": received_by_store.get(current_store_id, 0),
                "events_pending": pending_events_by_store.get(current_store_id, 0),
                "events_failed": failed_events_by_store.get(current_store_id, 0),
                "orders_pending": summary["orders_pending"],
                "oldest_pending_age_seconds": summary["oldest_pending_age_seconds"],
                "orders_written": orders_by_store.get(current_store_id, 0),
                "refresh_completed": summary["refresh_completed"],
                "orders_dead": summary["orders_dead"],
                "refresh_success_rate": summary["refresh_success_rate"],
                "end_to_end_p95_ms": summary["end_to_end_p95_ms"],
                "end_to_end_samples": summary["end_to_end_samples"],
                "backfill_watermark": checkpoints.get(current_store_id),
                "last_event_at": latest_event_by_store.get(current_store_id),
                "_e2e_durations_ms": summary["_e2e_durations_ms"],
            }
        )

    tenant_summary = _summarize_queue(pending_queue, window_queue, now=now)
    return {
        "tenant_id": str(tenant_id),
        "tenant_name": tenant_name,
        "instance_id": str(instance_id),
        "instance_name": instance_name,
        "instance_status": instance_status,
        "events_received": sum(received_by_store.values()),
        "events_pending": sum(pending_events_by_store.values()),
        "events_failed": sum(failed_events_by_store.values()),
        "orders_pending": tenant_summary["orders_pending"],
        "oldest_pending_age_seconds": tenant_summary["oldest_pending_age_seconds"],
        "orders_written": sum(orders_by_store.values()),
        "refresh_completed": tenant_summary["refresh_completed"],
        "orders_dead": tenant_summary["orders_dead"],
        "refresh_success_rate": tenant_summary["refresh_success_rate"],
        "end_to_end_p95_ms": tenant_summary["end_to_end_p95_ms"],
        "end_to_end_samples": tenant_summary["end_to_end_samples"],
        "backfill_watermark": _iso(
            max(
                (
                    _as_datetime(value)
                    for value in checkpoints.values()
                    if _as_datetime(value) is not None
                ),
                default=None,
            )
        ),
        "last_event_at": _iso(_as_datetime(latest_event_at)),
        "outbox_failed": outbox_failed,
        "stores": store_payloads,
        "_e2e_durations_ms": tenant_summary["_e2e_durations_ms"],
    }


def _instance_rows(center, tenant_id: int | None = None) -> list[tuple[Any, str]]:
    from app.modules.identity.models_center import Tenant
    from app.modules.ops.models_center import TenantInstance

    stmt = select(TenantInstance, Tenant.name).join(
        Tenant, Tenant.id == TenantInstance.tenant_id, isouter=True
    )
    if tenant_id is not None:
        stmt = stmt.where(TenantInstance.tenant_id == int(tenant_id))
    rows = center.execute(stmt.order_by(TenantInstance.id.asc())).all()
    return [(row[0], str(row[1] or "")) for row in rows]


# --------------------------------------------------------------------------- 对外聚合
def _empty_totals() -> dict[str, Any]:
    return {
        "events_received": 0,
        "events_pending": 0,
        "events_failed": 0,
        "orders_pending": 0,
        "orders_written": 0,
        "refresh_completed": 0,
        "orders_dead": 0,
        "outbox_failed": 0,
        "oldest_pending_age_seconds": None,
        "refresh_success_rate": None,
        "end_to_end_p95_ms": None,
        "end_to_end_samples": 0,
        "backfill_watermark": None,
    }


def webhook_metrics(
    center,
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    tenant_id: int | None = None,
    store_id: int | None = None,
) -> dict[str, Any]:
    """平台只读 Webhook 指标：租户库聚合 + Gateway 管理接口 + Redis 内存。"""
    window = _normalize_window(window_seconds)
    now = _utc_now()
    since = now - timedelta(seconds=window)
    tenants: list[dict[str, Any]] = []
    for instance, tenant_name in _instance_rows(center, tenant_id):
        try:
            tenant_payload = _collect_tenant_metrics(
                center,
                tenant_id=int(instance.tenant_id),
                tenant_name=tenant_name,
                instance_id=int(instance.id),
                instance_name=str(instance.name or ""),
                instance_status=str(instance.status or ""),
                since=since,
                now=now,
                store_id=store_id,
            )
            tenants.append(tenant_payload)
        except Exception as exc:  # noqa: BLE001 - 单租户库故障不影响其他租户
            logger.warning(
                "TikTok Webhook 指标聚合失败 tenant=%s: %s", instance.tenant_id, exc
            )
            tenants.append(
                {
                    "tenant_id": str(instance.tenant_id),
                    "tenant_name": tenant_name,
                    "instance_id": str(instance.id),
                    "instance_name": str(instance.name or ""),
                    "instance_status": str(instance.status or ""),
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "stores": [],
                }
            )

    totals = _empty_totals()
    durations: list[float] = []
    for tenant in tenants:
        if tenant.get("error"):
            continue
        for key in (
            "events_received",
            "events_pending",
            "events_failed",
            "orders_pending",
            "orders_written",
            "refresh_completed",
            "orders_dead",
            "outbox_failed",
        ):
            totals[key] += _safe_int(tenant.get(key))
        for key in ("oldest_pending_age_seconds",):
            value = tenant.get(key)
            if value is not None:
                totals[key] = max(totals[key] or 0, _safe_int(value))
        watermark = tenant.get("backfill_watermark")
        if watermark and (not totals["backfill_watermark"] or watermark > totals["backfill_watermark"]):
            totals["backfill_watermark"] = watermark
        durations.extend(tenant.get("_e2e_durations_ms") or [])
    totals["end_to_end_samples"] = len(durations)
    totals["end_to_end_p95_ms"] = (
        round(float(core_metrics.percentile(durations, 0.95)), 1) if durations else None
    )
    totals["refresh_success_rate"] = _rate(
        totals["refresh_completed"], totals["refresh_completed"] + totals["orders_dead"]
    )
    for tenant in tenants:
        tenant.pop("_e2e_durations_ms", None)
        for store in tenant.get("stores", []):
            store.pop("_e2e_durations_ms", None)

    gateway = _gateway_metrics(window)
    redis = _redis_metrics()
    redis["publish_failed"] = totals["outbox_failed"]
    redis["publish_failed_source"] = "tenant outbox_events.status=failed（代理指标）"
    return {
        "checked_at": _iso(now),
        "window_seconds": window,
        "filters": {"tenant_id": str(tenant_id) if tenant_id is not None else None,
                    "store_id": str(store_id) if store_id is not None else None},
        "gateway": gateway,
        "totals": totals,
        "tenants": tenants,
        "redis": redis,
        "notes": [
            "事件已接收取租户 webhook_events.received_at 窗口计数；订单待刷新取 tiktok_order_refresh_queue 当前积压；"
            "订单已写入取 orders 表窗口内更新时间计数。",
            "端到端 P95 取刷新队列 created_at→updated_at（事件落库与入队同事务）；最老待刷新按队列 updated_at 计算。",
            "Gateway 事件计数通过网关只读 admin webhook-stats 拉取；验签失败/重复/P95 网关未暴露时展示为不可用。",
            "Redis 429 未独立计数；发布失败使用租户 outbox_events failed 代理。",
        ],
    }


# --------------------------------------------------------------------------- 告警
def evaluate_alert_conditions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """根据指标生成告警条件（纯函数，供调度与单测）。"""
    conditions: list[dict[str, Any]] = []
    gateway = payload.get("gateway") or {}
    redis = payload.get("redis") or {}
    unroutable_recent = gateway.get("unroutable_recent")
    if unroutable_recent is not None and _safe_int(unroutable_recent) > 0:
        conditions.append(
            {
                "tenant_id": None,
                "instance_id": None,
                "alert_type": ALERT_WEBHOOK_UNROUTABLE,
                "severity": "critical",
                "message": f"TikTok Webhook 窗口内出现 unroutable 事件 {unroutable_recent} 条",
                "value": str(unroutable_recent),
                "threshold": "0",
            }
        )
    gateway_dead = _safe_int(gateway.get("dead"))
    if gateway_dead > 0:
        conditions.append(
            {
                "tenant_id": None,
                "instance_id": None,
                "alert_type": ALERT_WEBHOOK_DEAD,
                "severity": "critical",
                "message": f"Gateway TikTok Webhook dead 事件 {gateway_dead} 条",
                "value": str(gateway_dead),
                "threshold": "0",
            }
        )
    if not redis.get("available") or _safe_int(redis.get("publish_failed")) > 0:
        detail = "Redis 不可用" if not redis.get("available") else f"outbox failed {redis.get('publish_failed')} 条"
        conditions.append(
            {
                "tenant_id": None,
                "instance_id": None,
                "alert_type": ALERT_WEBHOOK_REDIS,
                "severity": "critical" if not redis.get("available") else "warning",
                "message": f"TikTok Webhook Redis 发布失败/不可用：{detail}",
                "value": str(redis.get("publish_failed") or 0),
                "threshold": "0",
            }
        )
    for tenant in payload.get("tenants") or []:
        if tenant.get("error"):
            continue
        tenant_id = _safe_int(tenant.get("tenant_id"))
        instance_id = _safe_int(tenant.get("instance_id"))
        p95 = tenant.get("end_to_end_p95_ms")
        samples = _safe_int(tenant.get("end_to_end_samples"))
        if (
            p95 is not None
            and float(p95) > E2E_P95_THRESHOLD_MS
            and samples >= E2E_MIN_SAMPLES
        ):
            conditions.append(
                {
                    "tenant_id": tenant_id,
                    "instance_id": instance_id,
                    "alert_type": ALERT_WEBHOOK_E2E_P95,
                    "severity": "warning",
                    "message": f"TikTok Webhook 端到端 P95 {round(float(p95) / 1000, 1)}s 持续超过 120s",
                    "value": f"{round(float(p95) / 1000, 1)}s",
                    "threshold": f"{int(E2E_P95_THRESHOLD_MS / 1000)}s",
                }
            )
        oldest = tenant.get("oldest_pending_age_seconds")
        received = _safe_int(tenant.get("events_received"))
        if (
            oldest is not None
            and float(oldest) > PENDING_STALE_SECONDS
            and received <= LOW_TRAFFIC_MAX_EVENTS
            and _safe_int(tenant.get("orders_pending")) > 0
        ):
            conditions.append(
                {
                    "tenant_id": tenant_id,
                    "instance_id": instance_id,
                    "alert_type": ALERT_WEBHOOK_PENDING_STALE,
                    "severity": "warning",
                    "message": f"低流量下 TikTok 订单最老待刷新 {int(float(oldest))}s（阈值 120s）",
                    "value": f"{int(float(oldest))}s",
                    "threshold": f"{PENDING_STALE_SECONDS}s",
                }
            )
        if _safe_int(tenant.get("orders_dead")) > 0:
            conditions.append(
                {
                    "tenant_id": tenant_id,
                    "instance_id": instance_id,
                    "alert_type": ALERT_WEBHOOK_DEAD,
                    "severity": "critical",
                    "message": f"TikTok 订单刷新进入 dead：{tenant.get('orders_dead')} 条",
                    "value": str(tenant.get("orders_dead")),
                    "threshold": "0",
                }
            )
        if _safe_int(tenant.get("outbox_failed")) > 0:
            conditions.append(
                {
                    "tenant_id": tenant_id,
                    "instance_id": instance_id,
                    "alert_type": ALERT_WEBHOOK_REDIS,
                    "severity": "warning",
                    "message": f"租户 outbox 发布失败：{tenant.get('outbox_failed')} 条",
                    "value": str(tenant.get("outbox_failed")),
                    "threshold": "0",
                }
            )
    return conditions


def _sustained(
    instance_id: int,
    alert_type: str,
    *,
    active: bool,
    now: datetime,
) -> bool:
    """用 Redis 首次命中时间判断持续时长；Redis 不可用时返回 False（不误报）。"""
    try:
        from app.core.redis import cache_redis

        client = cache_redis()
        key = f"{SUSTAIN_KEY_PREFIX}:{instance_id}:{alert_type}"
        if not active:
            client.delete(key)
            return False
        first = client.get(key)
        if first is None:
            client.set(key, int(now.timestamp()), ex=E2E_SUSTAIN_SECONDS * 3)
            return False
        return now.timestamp() - float(first) >= E2E_SUSTAIN_SECONDS
    except Exception:  # noqa: BLE001 - 状态存储不可用时宁可不告警
        return False


_last_alert_check_at: datetime | None = None


def check_webhook_alerts(center, *, force: bool = False, now: datetime | None = None) -> int:
    """调度进程周期调用：生成/恢复 TikTok Webhook 告警，复用既有节点告警合并。"""
    global _last_alert_check_at
    moment = now or _utc_now()
    if not force and _last_alert_check_at is not None:
        if (moment - _last_alert_check_at).total_seconds() < ALERT_CHECK_INTERVAL_SECONDS:
            return 0
    _last_alert_check_at = moment
    try:
        payload = webhook_metrics(center, window_seconds=WEBHOOK_ALERT_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001 - 告警评估失败不能影响调度轮次
        logger.exception("TikTok Webhook 指标聚合失败，跳过告警")
        return 0
    conditions = evaluate_alert_conditions(payload)
    try:
        from app.modules.ops.models_center import ServerNode
        from app.modules.ops.service import _resolve_alerts, _upsert_alert

        nodes = center.execute(select(ServerNode).order_by(ServerNode.id.asc())).scalars().all()
        first_node_by_instance: dict[int, ServerNode] = {}
        for node in nodes:
            first_node_by_instance.setdefault(int(node.instance_id), node)
        active_by_node: dict[int, set[str]] = defaultdict(set)
        created = 0
        first_global_node: ServerNode | None = nodes[0] if nodes else None
        for condition in conditions:
            instance_id = _safe_int(condition.get("instance_id"))
            node = first_node_by_instance.get(instance_id) if instance_id else first_global_node
            if node is None:
                continue
            alert_type = str(condition["alert_type"])
            active_by_node[int(node.id)].add(alert_type)
            if alert_type == ALERT_WEBHOOK_E2E_P95:
                if not _sustained(
                    instance_id or int(node.instance_id),
                    alert_type,
                    active=True,
                    now=moment,
                ):
                    continue
            _upsert_alert(
                center,
                node,
                alert_type,
                str(condition["severity"]),
                str(condition["message"]),
                str(condition["value"]),
                str(condition["threshold"]),
                moment,
            )
            created += 1
        # 条件未命中的节点恢复对应告警；P95 同时清理持续状态
        for node in nodes:
            active = active_by_node.get(int(node.id), set())
            for alert_type in WEBHOOK_ALERT_TYPES:
                if alert_type in active:
                    continue
                _resolve_alerts(center, node, (alert_type,), moment)
                if alert_type == ALERT_WEBHOOK_E2E_P95:
                    _sustained(int(node.instance_id), alert_type, active=False, now=moment)
        center.commit()
        return created
    except Exception:  # noqa: BLE001 - 告警写入失败不阻塞调度
        logger.exception("TikTok Webhook 告警评估失败")
        return 0


__all__ = [
    "ALERT_WEBHOOK_DEAD",
    "ALERT_WEBHOOK_E2E_P95",
    "ALERT_WEBHOOK_PENDING_STALE",
    "ALERT_WEBHOOK_REDIS",
    "ALERT_WEBHOOK_UNROUTABLE",
    "E2E_MIN_SAMPLES",
    "E2E_P95_THRESHOLD_MS",
    "E2E_SUSTAIN_SECONDS",
    "LOW_TRAFFIC_MAX_EVENTS",
    "PENDING_STALE_SECONDS",
    "WEBHOOK_ALERT_TYPES",
    "WEBHOOK_CHANNEL",
    "check_webhook_alerts",
    "evaluate_alert_conditions",
    "webhook_metrics",
]
