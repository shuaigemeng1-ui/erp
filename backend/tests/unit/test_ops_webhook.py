"""task-42：TikTok Webhook 指标与告警条件测试（纯函数，不连 MySQL/Redis/网关）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.modules.ops import webhook_metrics


def _utc_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _queue_row(**overrides):
    base = {
        "store_id": 1,
        "created_at": _utc_naive() - timedelta(minutes=4),
        "updated_at": _utc_naive() - timedelta(minutes=1),
        "status": "idle",
        "generation": 1,
        "processed_generation": 1,
        "attempts": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_summarize_queue_pending_written_dead_and_p95():
    now = _utc_naive()
    pending = [
        _queue_row(
            status="pending",
            created_at=now - timedelta(minutes=5),
            updated_at=now - timedelta(minutes=3),
        ),
        _queue_row(
            status="retry_wait",
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(seconds=130),
        ),
    ]
    window = [
        _queue_row(
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(seconds=20),
        ),
        _queue_row(
            created_at=now - timedelta(minutes=3),
            updated_at=now - timedelta(seconds=60),
        ),
        _queue_row(status="dead", created_at=now - timedelta(minutes=1)),
        # 旧 generation 未处理完，不能算刷新完成
        _queue_row(
            generation=2,
            processed_generation=1,
            created_at=now - timedelta(minutes=1),
            updated_at=now,
        ),
    ]
    summary = webhook_metrics._summarize_queue(pending, window, now=now)

    assert summary["orders_pending"] == 2
    assert summary["oldest_pending_age_seconds"] == 180
    assert summary["refresh_completed"] == 2
    assert summary["orders_dead"] == 1
    assert summary["refresh_success_rate"] == round(2 / 3, 4)
    assert summary["end_to_end_samples"] == 2
    assert summary["end_to_end_p95_ms"] is not None
    assert summary["end_to_end_p95_ms"] > 0


def test_summarize_queue_without_samples_is_null_safe():
    summary = webhook_metrics._summarize_queue([], [], now=_utc_naive())
    assert summary["orders_pending"] == 0
    assert summary["oldest_pending_age_seconds"] is None
    assert summary["refresh_success_rate"] is None
    assert summary["end_to_end_p95_ms"] is None


def _payload(**overrides):
    payload = {
        "gateway": {"available": True, "unroutable": 0},
        "redis": {"available": True, "publish_failed": 0},
        "tenants": [
            {
                "tenant_id": "1",
                "instance_id": "1",
                "events_received": 3,
                "orders_pending": 0,
                "oldest_pending_age_seconds": 0,
                "orders_dead": 0,
                "outbox_failed": 0,
                "end_to_end_p95_ms": 500,
                "end_to_end_samples": 10,
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_evaluate_alert_conditions_healthy_payload():
    assert webhook_metrics.evaluate_alert_conditions(_payload()) == []


def test_evaluate_alert_conditions_p95_pending_dead_and_redis():
    payload = _payload(
        gateway={"available": True, "unroutable": 2, "unroutable_recent": 2, "unavailable_fields": []},
        redis={"available": False, "publish_failed": 3},
        tenants=[
            {
                "tenant_id": "1",
                "instance_id": "1",
                "events_received": 4,
                "orders_pending": 2,
                "oldest_pending_age_seconds": 300,
                "orders_dead": 1,
                "outbox_failed": 2,
                "end_to_end_p95_ms": 125000,
                "end_to_end_samples": 6,
            }
        ],
    )
    conditions = webhook_metrics.evaluate_alert_conditions(payload)
    types = {item["alert_type"] for item in conditions}
    assert webhook_metrics.ALERT_WEBHOOK_E2E_P95 in types
    assert webhook_metrics.ALERT_WEBHOOK_PENDING_STALE in types
    assert webhook_metrics.ALERT_WEBHOOK_DEAD in types
    assert webhook_metrics.ALERT_WEBHOOK_UNROUTABLE in types
    assert webhook_metrics.ALERT_WEBHOOK_REDIS in types


def test_low_sample_p95_does_not_alert():
    payload = _payload(
        tenants=[
            {
                "tenant_id": "1",
                "instance_id": "1",
                "events_received": 100,
                "orders_pending": 0,
                "oldest_pending_age_seconds": 0,
                "orders_dead": 0,
                "outbox_failed": 0,
                "end_to_end_p95_ms": 125000,
                "end_to_end_samples": 2,
            }
        ]
    )
    assert webhook_metrics.evaluate_alert_conditions(payload) == []


def test_high_traffic_pending_stale_not_alerted():
    payload = _payload(
        tenants=[
            {
                "tenant_id": "1",
                "instance_id": "1",
                "events_received": 100,
                "orders_pending": 2,
                "oldest_pending_age_seconds": 300,
                "orders_dead": 0,
                "outbox_failed": 0,
                "end_to_end_p95_ms": 100,
                "end_to_end_samples": 10,
            }
        ]
    )
    assert webhook_metrics.evaluate_alert_conditions(payload) == []


def test_normalize_window_bounds():
    assert webhook_metrics._normalize_window(1) == webhook_metrics.MIN_WINDOW_SECONDS
    assert webhook_metrics._normalize_window(86_400 * 2) == webhook_metrics.MAX_WINDOW_SECONDS
    assert webhook_metrics._normalize_window("bad") == webhook_metrics.DEFAULT_WINDOW_SECONDS


def test_gateway_metrics_unconfigured(monkeypatch):
    monkeypatch.setattr(webhook_metrics.settings, "gateway_base_url", "", raising=False)
    payload = webhook_metrics._gateway_metrics(300)
    assert payload["available"] is False
    assert payload["source"] == "unconfigured"
    assert "signature_failed" in payload["unavailable_fields"]
    assert payload["received_total"] is None


def test_webhook_metrics_empty_center(monkeypatch):
    monkeypatch.setattr(webhook_metrics, "_instance_rows", lambda center, tenant_id=None: [])
    monkeypatch.setattr(
        webhook_metrics,
        "_gateway_metrics",
        lambda window_seconds: {"available": False, "unavailable_fields": []},
    )
    monkeypatch.setattr(
        webhook_metrics,
        "_redis_metrics",
        lambda: {
            "available": False,
            "publish_failed": 0,
            "rate_limited": None,
            "rate_limited_reason": "tests",
        },
    )
    payload = webhook_metrics.webhook_metrics(SimpleNamespace(), window_seconds=300)
    assert payload["window_seconds"] == 300
    assert payload["tenants"] == []
    assert payload["totals"]["events_received"] == 0
    assert payload["totals"]["end_to_end_p95_ms"] is None
    assert payload["notes"]
