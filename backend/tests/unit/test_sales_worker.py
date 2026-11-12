"""通过实际 Worker 与订单服务验证渠道分发、固定窗口和失败恢复，无外部连接。"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, MetaData, select

from app.core.errors import AppError
from app.core.access import ScopeGrant
from app.db.base import TenantBase
from app.modules.access.models_tenant import TenantSettings
from app.modules.channels.models_tenant import Store
from app.modules.channels import service as channel_service
from app.modules.sales import tasks, service
from app.modules.sales.models_tenant import Order, OrderSyncCheckpoint
from app.modules.tasks.models_tenant import SyncRun, OutboxEvent
from tests.unit.test_sales import session, stock_stub, _mapping_for, _order_payload  # noqa: F401
from tests.unit.test_tiktok_phase2 import ORDER


@pytest.fixture()
def worker(session, stock_stub, monkeypatch):
    metadata = MetaData()
    for name in ("stores", "tenant_settings", "order_sync_checkpoints"):
        table = TenantBase.metadata.tables[name].to_metadata(metadata)
        table.c.id.type = Integer()
    metadata.create_all(session.get_bind())
    session.add(TenantSettings(tenant_id=1, order_sync_enabled=True, stock_push_enabled=False))
    session.add_all([
        Store(id=1, tenant_id=1, channel="tiktok", environment="production", name="TikTok",
              store_code="TK1", external_store_id="remote1", status="connected"),
        Store(id=2, tenant_id=1, channel="shopee", environment="production", name="Shopee",
              store_code="SH2", external_store_id="remote2", status="connected"),
    ])
    session.commit()
    context = SimpleNamespace(db_route=object(), is_automation=True)

    @contextmanager
    def fake_session(route):
        yield session

    @contextmanager
    def fake_job(*args, **kwargs):
        yield context, session

    monkeypatch.setattr(tasks, "tenant_session", fake_session)
    monkeypatch.setattr(tasks, "job_tenant_session", fake_job)
    monkeypatch.setattr(tasks, "require_tenant_context", lambda: context)
    monkeypatch.setattr(tasks, "acquire_lock", lambda *args, **kwargs: "offline-lock")
    monkeypatch.setattr(tasks, "release_lock", lambda *args: None)
    monkeypatch.setattr(service, "_resolve_sku_mapping", _mapping_for(("SKU-1", 501)))
    return session, stock_stub


def test_tiktok_worker_uses_tiktok_parser_and_real_order_service(worker, monkeypatch):
    db, stock = worker
    monkeypatch.setattr(channel_service, "fetch_orders", lambda *args, **kwargs: {
        "orders": [ORDER], "has_more": False,
    })
    result = tasks.run_order_sync(1, store_id=1, triggered_by="schedule")
    order = db.execute(select(Order)).scalar_one()
    assert result["status"] == "succeeded"
    assert order.channel == "tiktok" and order.status == "paid"
    assert stock["calls"][0]["quantity_change"] == -2
    tasks.run_order_sync(1, store_id=1, triggered_by="schedule")
    assert len(stock["calls"]) == 1


def test_store_checkpoint_does_not_reuse_other_store_window(worker, monkeypatch):
    db, _ = worker
    now = datetime(2026, 11, 12, 9)
    monkeypatch.setattr(tasks, "utcnow", lambda: now)
    db.add(OrderSyncCheckpoint(tenant_id=1, store_id=1, channel="tiktok", environment="production",
                               watermark=now - timedelta(minutes=10), cursor=""))
    db.commit()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return {"orders": [], "has_more": False}

    monkeypatch.setattr(channel_service, "fetch_orders", fetch)
    tasks.run_order_sync(1, triggered_by="schedule")
    by_store = {call["store_id"]: call for call in calls}
    assert by_store[1]["since"] == now - timedelta(hours=2, minutes=10)
    assert by_store[2]["since"] == now - timedelta(days=14)
    assert by_store[1]["until"] == by_store[2]["until"] == now


def test_page_cap_preserves_window_and_enqueues_continuation(worker, monkeypatch):
    db, _ = worker
    monkeypatch.setattr(tasks, "MAX_PAGES_PER_RUN", 1)
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return {"orders": [], "has_more": len(calls) == 1, "next_cursor": "page2" if len(calls) == 1 else None}

    monkeypatch.setattr(channel_service, "fetch_orders", fetch)
    first = tasks.run_order_sync(1, store_id=1, triggered_by="schedule")
    point = db.execute(select(OrderSyncCheckpoint)).scalar_one()
    assert first["status"] == "partial" and point.watermark is None
    start, end = point.window_start, point.window_end
    assert point.cursor == "page2"
    assert db.execute(select(SyncRun)).scalar_one().store_id == 1
    assert db.execute(select(OutboxEvent)).scalar_one().event_type == "sync_run.created"
    second = tasks.run_order_sync(1, store_id=1, triggered_by="schedule")
    db.refresh(point)
    assert second["status"] == "succeeded" and point.watermark == end
    assert calls[1]["cursor"] == "page2" and calls[1]["since"] == start and calls[1]["until"] == end


def test_failed_normalization_does_not_advance_cursor(worker, monkeypatch):
    db, _ = worker
    monkeypatch.setattr(channel_service, "fetch_orders", lambda *args, **kwargs: {
        "orders": [{"id": "broken", "line_items": []}], "has_more": True, "next_cursor": "skip-me",
    })
    monkeypatch.setattr(service, "ingest_platform_orders", lambda *args, **kwargs: {
        "total": 1, "errors": 1, "messages": ["normalization failed"],
    })
    result = tasks.run_order_sync(1, store_id=1, triggered_by="schedule")
    point = db.execute(select(OrderSyncCheckpoint)).scalar_one()
    assert result["status"] == "partial"
    assert point.watermark is None and point.cursor == ""


def test_order_sync_switch_rechecked_between_pages(worker, monkeypatch):
    db, _ = worker
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        setting = db.execute(select(TenantSettings)).scalar_one()
        setting.order_sync_enabled = False
        db.commit()
        return {"orders": [], "has_more": True, "next_cursor": "page2"}

    monkeypatch.setattr(channel_service, "fetch_orders", fetch)
    result = tasks.run_order_sync(1, store_id=1, triggered_by="schedule")
    assert result["status"] == "failed" and len(calls) == 1
    assert db.execute(select(OrderSyncCheckpoint)).scalar_one().watermark is None


def test_older_platform_snapshot_does_not_restore_newer_stock(worker, monkeypatch):
    db, stock = worker
    new = _order_payload(update_time=200, order_status="READY_TO_SHIP")
    old = _order_payload(update_time=100, order_status="CANCELLED")
    service.ingest_platform_orders(db, tenant_id=1, store_id=2, channel="shopee", payloads=[new])
    service.ingest_platform_orders(db, tenant_id=1, store_id=2, channel="shopee", payloads=[old])
    order = db.execute(select(Order)).scalar_one()
    assert order.status == "paid" and len(stock["calls"]) == 1


def test_manual_sync_rechecks_store_scope_before_each_page(worker, monkeypatch):
    db, _ = worker
    visits = []
    calls = []

    @contextmanager
    def fresh_job(*args, **kwargs):
        visits.append(True)
        # 起始及第一页允许店铺 1，第二页前权限已改为仅店铺 2。
        store_id = 1 if len(visits) <= 2 else 2
        grant = ScopeGrant(role_binding_id=1, role_id=1, scope_type="stores",
                           scope_ids=frozenset({store_id}), permissions=frozenset({"order.sync"}))
        yield SimpleNamespace(db_route=object(), is_automation=False, scope_grants=(grant,)), db

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return {"orders": [], "has_more": True, "next_cursor": "page2"}

    monkeypatch.setattr(tasks, "job_tenant_session", fresh_job)
    monkeypatch.setattr(tasks, "guard_manual_permission", lambda *args: None)
    monkeypatch.setattr(channel_service, "fetch_orders", fetch)
    result = tasks.run_order_sync(1, store_id=1, requested_by=22, triggered_by="manual")
    assert result["status"] == "failed" and len(calls) == 1
    assert db.execute(select(OrderSyncCheckpoint)).scalar_one().cursor == "page2"


def test_manual_sync_without_actor_never_falls_back_to_owner():
    with pytest.raises(AppError):
        tasks.run_order_sync(1, store_id=1, triggered_by="manual")


def test_network_failure_after_committed_page_preserves_counts_and_checkpoint(worker, monkeypatch):
    db, stock = worker
    pages = []

    def fetch(*args, **kwargs):
        pages.append(kwargs)
        if len(pages) == 1:
            return {"orders": [ORDER], "has_more": True, "next_cursor": "remaining-page"}
        raise TimeoutError("offline timeout")

    monkeypatch.setattr(channel_service, "fetch_orders", fetch)
    result = tasks.run_order_sync(1, store_id=1, triggered_by="schedule")
    assert result["status"] == "partial"
    assert result["total_processed"] == 1 and result["orders_created"] == 1
    assert result["errors"] == 1 and len(stock["calls"]) == 1
    point = db.execute(select(OrderSyncCheckpoint)).scalar_one()
    assert point.cursor == "remaining-page" and point.watermark is None
    assert db.execute(select(Order)).scalar_one().channel == "tiktok"
