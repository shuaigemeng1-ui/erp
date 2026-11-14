"""TikTok Webhook 严格回执、generation/lease 队列与历史对账测试（不连 MySQL/Redis）。

对应 task-40 P0：

* relay 回执语义：received/duplicate → 200；mapping_missing / 持久化失败 → 503（绝不 200）；
* 刷新队列 generation 原子递增、processing 期间新事件回到 pending；
* Worker 领取写入 lease_token/generation，确认/失败只对当前版本生效（旧 Worker 不得覆盖）；
* 调度进程 15 秒扫描（本测试只验证 claim 条件与 reconciliation，不启动调度/Celery）；
* 历史 received 事件：订单已存在补 processed，缺结果重新入队且避免重复放大 generation。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, MetaData, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import TenantBase
from app.modules.channels import service
from app.modules.channels import tasks as channel_tasks
from app.modules.channels.models_tenant import TiktokOrderRefreshQueue
from app.modules.sales.models_tenant import Order
from app.modules.tasks.models_tenant import WebhookEvent

NOW = datetime(2026, 11, 14, 8, 0, 0)


def _session() -> Session:
    copy = MetaData()
    for name in ("tiktok_order_refresh_queue", "webhook_events", "orders"):
        TenantBase.metadata.tables[name].to_metadata(copy)
    for table in copy.tables.values():
        table.c.id.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    copy.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def _queue(
    db: Session,
    *,
    tenant_id: int = 1,
    store_id: int = 10,
    order_no: str = "ORDER-1",
    generation: int = 1,
    processed_generation: int = 0,
    status: str = "pending",
    attempts: int = 0,
    next_attempt_at: datetime | None = None,
    lease_token: str | None = None,
    lease_expires_at: datetime | None = None,
) -> TiktokOrderRefreshQueue:
    row = TiktokOrderRefreshQueue(
        tenant_id=tenant_id,
        store_id=store_id,
        external_order_no=order_no,
        generation=generation,
        processed_generation=processed_generation,
        status=status,
        attempts=attempts,
        next_attempt_at=next_attempt_at if next_attempt_at is not None else NOW - timedelta(seconds=1),
        lease_owner="worker" if lease_token else None,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
    )
    db.add(row)
    db.flush()
    return row


def _event(
    db: Session,
    *,
    tenant_id: int = 1,
    store_id: int | None = 10,
    external_event_id: str = "evt-1",
    order_ids: list[str] | None = None,
    status: str = "received",
) -> WebhookEvent:
    event = WebhookEvent(
        tenant_id=tenant_id,
        channel="tiktok",
        store_id=store_id,
        event_type="ORDER_STATUS_CHANGE",
        external_event_id=external_event_id,
        signature_ok=True,
        status=status,
        payload={"order_ids": order_ids or []},
        received_at=NOW - timedelta(minutes=5),
    )
    db.add(event)
    db.flush()
    return event


def _order(db: Session, *, tenant_id: int = 1, store_id: int = 10, order_no: str = "ORDER-1") -> Order:
    order = Order(
        tenant_id=tenant_id,
        store_id=store_id,
        channel="tiktok",
        external_order_no=order_no,
        status="paid",
    )
    db.add(order)
    db.flush()
    return order


# --------------------------------------------------------------------------- generation
def test_enqueue_creates_then_atomically_increments_generation() -> None:
    db = _session()
    assert service.enqueue_tiktok_order_refresh(db, 1, 10, "ORDER-1", reason="ORDER_STATUS_CHANGE")
    db.flush()
    row = db.execute(select(TiktokOrderRefreshQueue)).scalar_one()
    assert row.generation == 1 and row.status == "pending"
    assert service.enqueue_tiktok_order_refresh(db, 1, 10, "ORDER-1", reason="ORDER_STATUS_CHANGE")
    db.flush()
    db.refresh(row)
    assert row.generation == 2
    assert row.processed_generation == 0
    assert row.status == "pending"


def test_new_event_during_processing_resets_lease_and_generation() -> None:
    db = _session()
    row = _queue(
        db,
        generation=3,
        status="processing",
        lease_token="token-old",
        lease_expires_at=NOW + timedelta(minutes=2),
        attempts=4,
    )
    assert service.enqueue_tiktok_order_refresh(db, 1, 10, "ORDER-1", reason="RECIPIENT_ADDRESS_UPDATE")
    db.flush()
    db.refresh(row)
    assert row.generation == 4
    assert row.status == "pending"
    assert row.lease_token is None and row.lease_expires_at is None
    assert row.attempts == 0


# --------------------------------------------------------------------------- claim / confirm
def test_claim_leases_due_rows_and_skips_future_lease() -> None:
    db = _session()
    due = _queue(db, order_no="A", next_attempt_at=NOW - timedelta(seconds=5))
    retry = _queue(db, order_no="B", status="retry_wait", next_attempt_at=NOW - timedelta(seconds=5))
    expired = _queue(
        db,
        order_no="C",
        status="processing",
        lease_token="old",
        lease_expires_at=NOW - timedelta(seconds=1),
    )
    future = _queue(
        db,
        order_no="D",
        status="processing",
        lease_token="live",
        lease_expires_at=NOW + timedelta(minutes=5),
    )
    claims = channel_tasks.claim_tiktok_refresh_batch(db, 1, now=NOW, limit=10)
    claimed_orders = {claim["external_order_no"] for claim in claims}
    assert claimed_orders == {"A", "B", "C"}
    assert all(claim["lease_token"] for claim in claims)
    assert any(claim["generation"] == 1 for claim in claims)
    db.flush()
    for row in (due, retry, expired):
        db.refresh(row)
        assert row.status == "processing"
        assert row.lease_token
        assert row.attempts == 1
    db.refresh(future)
    assert future.status == "processing" and future.lease_token == "live"


def test_confirm_only_current_generation_and_token() -> None:
    db = _session()
    row = _queue(db, order_no="A")
    claim = channel_tasks.claim_tiktok_refresh_batch(db, 1, now=NOW, limit=1)[0]
    # 外呼期间新事件：generation 递增并清空租约
    service.enqueue_tiktok_order_refresh(db, 1, 10, "A", reason="ORDER_STATUS_CHANGE")
    db.flush()
    assert channel_tasks.confirm_tiktok_refresh_success(db, claim) is False
    db.refresh(row)
    assert row.status == "pending" and row.processed_generation == 0
    row.next_attempt_at = NOW - timedelta(seconds=1)  # enqueue 使用真实 UTC 时间，测试固定时钟下需回拨
    db.flush()

    claim2 = channel_tasks.claim_tiktok_refresh_batch(db, 1, now=NOW, limit=1)[0]
    assert claim2["generation"] == 2
    assert channel_tasks.confirm_tiktok_refresh_success(db, claim2, processed_order_ids=["A"]) is True
    db.flush()
    db.refresh(row)
    assert row.status == "idle"
    assert row.processed_generation == 2
    assert row.lease_token is None and row.lease_expires_at is None


def test_confirm_marks_related_webhook_event_processed() -> None:
    db = _session()
    event = _event(db, order_ids=["A"])
    _queue(db, order_no="A")
    claim = channel_tasks.claim_tiktok_refresh_batch(db, 1, now=NOW, limit=1)[0]
    assert channel_tasks.confirm_tiktok_refresh_success(db, claim, processed_order_ids=["A"])
    db.flush()
    db.refresh(event)
    assert event.status == "processed"
    assert event.processed_at is not None


def test_fail_records_failure_code_and_backoff_then_dead() -> None:
    db = _session()
    row = _queue(db, order_no="A", attempts=0)
    claim = channel_tasks.claim_tiktok_refresh_batch(db, 1, now=NOW, limit=1)[0]
    assert channel_tasks.fail_tiktok_refresh(
        db, claim, error="详情为空", failure_code="detail_missing", now=NOW
    )
    db.flush()
    db.refresh(row)
    assert row.status == "retry_wait"
    assert row.last_failure_code == "detail_missing"
    assert row.attempts == 1
    assert row.next_attempt_at == NOW + timedelta(seconds=30)

    row.status = "pending"
    row.next_attempt_at = NOW - timedelta(seconds=1)
    row.attempts = channel_tasks.TIKTOK_REFRESH_MAX_ATTEMPTS - 1
    db.flush()
    claim2 = channel_tasks.claim_tiktok_refresh_batch(db, 1, now=NOW, limit=1)[0]
    assert channel_tasks.fail_tiktok_refresh(
        db, claim2, error="still failing", failure_code="normalize_failed", now=NOW
    )
    db.flush()
    db.refresh(row)
    assert row.status == "dead"
    assert row.last_failure_code == "normalize_failed"


# --------------------------------------------------------------------------- 历史对账
def test_reconcile_marks_processed_when_business_order_exists() -> None:
    db = _session()
    _order(db, order_no="A")
    event = _event(db, order_ids=["A"])
    summary = channel_tasks.reconcile_tiktok_webhook_events_in_db(db, 1, limit=10)
    db.flush()
    db.refresh(event)
    assert summary["reconciled"] == 1
    assert event.status == "processed"
    assert db.execute(select(TiktokOrderRefreshQueue)).scalar_one_or_none() is None


def test_reconcile_requeues_missing_order_and_avoids_repeated_generation() -> None:
    db = _session()
    event = _event(db, order_ids=["MISSING"])
    summary = channel_tasks.reconcile_tiktok_webhook_events_in_db(db, 1, limit=10)
    db.flush()
    db.refresh(event)
    assert summary["requeued"] == 1
    assert event.status == "received"
    row = db.execute(select(TiktokOrderRefreshQueue)).scalar_one()
    assert row.generation == 1 and row.status == "pending"

    # 队列仍活跃：下一轮对账跳过，不重复放大 generation
    summary2 = channel_tasks.reconcile_tiktok_webhook_events_in_db(db, 1, limit=10)
    db.flush()
    db.refresh(row)
    assert summary2["skipped"] == 1
    assert row.generation == 1

    # Worker 处理失败进入 dead：对账重新入队
    row.status = "dead"
    row.next_attempt_at = None
    db.flush()
    summary3 = channel_tasks.reconcile_tiktok_webhook_events_in_db(db, 1, limit=10)
    db.flush()
    db.refresh(row)
    assert summary3["requeued"] == 1
    assert row.generation == 2
    assert row.status == "pending"


def test_reconcile_mapping_missing_event_not_touched() -> None:
    db = _session()
    event = _event(db, store_id=None, order_ids=["A"])
    summary = channel_tasks.reconcile_tiktok_webhook_events_in_db(db, 1, limit=10)
    db.flush()
    db.refresh(event)
    assert summary["mapping_missing"] == 1
    assert event.status == "received"


# --------------------------------------------------------------------------- 严格回执
def test_relay_receipt_success_and_duplicate_are_200() -> None:
    status, payload = service.tiktok_webhook_receipt(
        received=True,
        duplicate=False,
        mapping_missing=False,
        event_id="42",
        external_event_id="notif-1",
    )
    assert status == 200
    assert payload["received"] is True and payload["duplicate"] is False
    assert payload["mapping_missing"] is False
    assert payload["event_id"] == "42"
    assert payload["external_event_id"] == "notif-1"

    status, payload = service.tiktok_webhook_receipt(
        received=True, duplicate=True, mapping_missing=False, event_id=42, external_event_id="notif-1"
    )
    assert status == 200 and payload["duplicate"] is True and payload["status"] == "duplicate"


def test_relay_receipt_mapping_missing_and_persist_failure_are_not_200() -> None:
    status, payload = service.tiktok_webhook_receipt(
        received=False,
        duplicate=False,
        mapping_missing=True,
        event_id=None,
        external_event_id="notif-1",
    )
    assert status == 503
    assert payload["mapping_missing"] is True
    assert payload["received"] is False
    assert payload["event_id"] == "notif-1"

    status, payload = service.tiktok_webhook_receipt(
        received=False,
        duplicate=False,
        mapping_missing=False,
        event_id=None,
        external_event_id="notif-1",
        reason="persist_failed",
    )
    assert status == 503
    assert payload["received"] is False
    assert payload["reason"] == "persist_failed"


def test_ensure_rows_on_duplicate_does_not_bump_generation() -> None:
    db = _session()
    _queue(db, order_no="A", generation=5, processed_generation=5, status="idle")
    created = service.ensure_tiktok_order_refresh_rows(db, 1, 10, ["A", "B"])
    db.flush()
    assert created == 1
    rows = {row.external_order_no: row for row in db.execute(select(TiktokOrderRefreshQueue)).scalars()}
    assert rows["A"].generation == 5
    assert rows["B"].generation == 1


def test_refresh_failure_codes_are_classified_per_order() -> None:
    detail_code, _ = channel_tasks._classify_refresh_exception(
        channel_tasks.TiktokRefreshError("detail_missing", "empty")
    )
    mismatch_code, mismatch_msg = channel_tasks._classify_refresh_exception(
        channel_tasks.TiktokRefreshError("order_no_mismatch", "请求 A 返回 B")
    )
    normalize_code, _ = channel_tasks._classify_refresh_exception(
        channel_tasks.TiktokRefreshError("normalize_failed", "bad payload")
    )
    assert detail_code == "detail_missing"
    assert mismatch_code == "order_no_mismatch" and "请求 A" in mismatch_msg
    assert normalize_code == "normalize_failed"


# --------------------------------------------------------------------------- relay 端点严格回执
class _FakeSession:
    def __init__(self, *, fail_commit: bool = False) -> None:
        self.fail_commit = fail_commit

    def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("db commit failed")

    def rollback(self) -> None:
        return None

    def get(self, model: Any, primary_key: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(status="processed")


def _relay_request() -> Any:
    import json as _json
    from starlette.requests import Request

    body = _json.dumps({"event": "ORDER_STATUS_CHANGE"}).encode("utf-8")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/integrations/tiktok/webhook/relay",
        "headers": [
            (b"x-gateway-timestamp", str(int(__import__("time").time())).encode()),
            (b"x-gateway-tenant-id", b"1"),
        ],
        "query_string": b"",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _call_relay(monkeypatch, *, store: Any, record_result: Any, session: _FakeSession, dispatch_calls: list[dict[str, Any]]):
    import asyncio
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.modules.channels import router as channels_router
    from app.modules.channels.tiktok import webhook as tiktok_webhook

    @contextmanager
    def fake_center():
        yield SimpleNamespace()

    @contextmanager
    def fake_tenant(_route):
        yield session

    monkeypatch.setattr(channels_router, "center_session", fake_center)
    monkeypatch.setattr(channels_router, "tenant_session", fake_tenant)
    monkeypatch.setattr(channels_router, "_load_tenant_route", lambda center, tenant_id: object())
    monkeypatch.setattr(channels_router.service, "load_tenant_status", lambda center, tenant_id: "active")
    monkeypatch.setattr(channels_router.service, "find_tiktok_store_by_shop", lambda db, tenant_id, shop_id: store)
    monkeypatch.setattr(tiktok_webhook, "verify_relay_signature", lambda **kwargs: True)
    monkeypatch.setattr(
        tiktok_webhook,
        "parse_event",
        lambda raw_body: ("notif-1", 1, "ORDER_STATUS_CHANGE", "shop-1", {"order_ids": ["A"]}),
    )
    monkeypatch.setattr(
        tiktok_webhook,
        "sanitized_payload",
        lambda **kwargs: {"order_ids": ["A"], "notification_id": "notif-1"},
    )
    monkeypatch.setattr(channels_router.service, "record_tiktok_webhook_event", lambda *args, **kwargs: record_result)
    monkeypatch.setattr(channels_router.service, "ensure_tiktok_order_refresh_rows", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        channels_router,
        "_dispatch_task",
        lambda task_name, **kwargs: dispatch_calls.append({"task": task_name, **kwargs}) or "task-1",
    )
    response = asyncio.run(channels_router.tiktok_webhook_relay(_relay_request()))
    return response


def _json_body(response: Any) -> dict[str, Any]:
    import json as _json

    return _json.loads(response.body.decode("utf-8"))


def test_relay_mapping_missing_returns_503_with_event_id(monkeypatch) -> None:
    response = _call_relay(
        monkeypatch,
        store=None,
        record_result=(0, False),
        session=_FakeSession(),
        dispatch_calls=[],
    )
    payload = _json_body(response)
    assert response.status_code == 503
    assert payload["received"] is False
    assert payload["mapping_missing"] is True
    assert payload["event_id"] == "notif-1"


def test_relay_persist_failure_returns_503(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    response = _call_relay(
        monkeypatch,
        store=object(),
        record_result=(42, False),
        session=_FakeSession(fail_commit=True),
        dispatch_calls=calls,
    )
    payload = _json_body(response)
    assert response.status_code == 503
    assert payload["received"] is False
    assert payload["reason"] == "persist_failed"
    assert calls == []


def test_relay_success_returns_200_event_id_and_wakes_worker(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    response = _call_relay(
        monkeypatch,
        store=object(),
        record_result=(42, False),
        session=_FakeSession(),
        dispatch_calls=calls,
    )
    payload = _json_body(response)
    assert response.status_code == 200
    assert payload["received"] is True and payload["duplicate"] is False
    assert payload["event_id"] == "42"
    assert payload["external_event_id"] == "notif-1"
    assert calls and calls[0]["task"] == "process_tiktok_webhook_events"


def test_relay_duplicate_returns_200_duplicate_true(monkeypatch) -> None:
    response = _call_relay(
        monkeypatch,
        store=object(),
        record_result=(42, True),
        session=_FakeSession(),
        dispatch_calls=[],
    )
    payload = _json_body(response)
    assert response.status_code == 200
    assert payload["received"] is True
    assert payload["duplicate"] is True
    assert payload["status"] == "duplicate"
