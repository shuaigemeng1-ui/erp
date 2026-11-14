"""Gateway storage backend abstraction tests (SQLite/PostgreSQL selection)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.gateway.config import GatewayConfig
from app.modules.gateway.database import (
    POSTGRES_SCHEMA,
    SQLiteBackend,
    create_backend,
)
from app.modules.gateway.store import GatewayStore


def make_config(**overrides) -> GatewayConfig:
    values = dict(
        secret_key="test-secret",
        admin_token="test-admin",
        webhook_dispatch_enabled=False,
        webhook_relay_secret="test-relay",
    )
    values.update(overrides)
    return GatewayConfig(**values)


def test_default_backend_is_sqlite(tmp_path) -> None:
    backend = create_backend(make_config(db_path=str(tmp_path / "gateway.db")))
    assert isinstance(backend, SQLiteBackend)
    assert backend.is_postgres is False


def test_sqlite_url_selects_sqlite_backend(tmp_path) -> None:
    db_path = tmp_path / "gateway-url.db"
    backend = create_backend(make_config(database_url=f"sqlite:///{db_path.as_posix()}"))
    assert isinstance(backend, SQLiteBackend)
    assert Path(backend.config.db_path).name == "gateway-url.db" or backend.config.db_path.endswith(
        "gateway-url.db"
    )


def test_postgres_url_selects_postgres_backend_or_requires_driver() -> None:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="psycopg"):
            create_backend(make_config(database_url="postgresql://u:p@pg:5432/gateway"))
        return
    backend = create_backend(make_config(database_url="postgresql://u:p@pg:5432/gateway"))
    assert backend.is_postgres is True
    assert backend.name == "postgresql"


def test_postgres_schema_contains_all_tables() -> None:
    expected = {
        "gateway_instances",
        "gateway_channel_apps",
        "gateway_oauth_states",
        "gateway_refresh_locks",
        "gateway_deliveries",
        "gateway_rate_limits",
        "gateway_settings",
        "gateway_audit_logs",
        "gateway_webhook_shops",
        "gateway_webhook_events",
    }
    for table in expected:
        assert table in POSTGRES_SCHEMA
    assert "lease_token" in POSTGRES_SCHEMA
    assert "failure_class" in POSTGRES_SCHEMA


def test_two_store_instances_share_sqlite_event_state(tmp_path) -> None:
    """无 PG 时的双连接冒烟：同库事件唯一、租约互斥；PG 集成测试见 test_gateway_postgres_integration。"""
    cfg = make_config(db_path=str(tmp_path / "shared.db"))
    store_a = GatewayStore(cfg)
    store_b = GatewayStore(cfg)
    store_a.init()
    store_b.init()
    event_id, duplicate = store_a.create_webhook_event(
        channel="tiktok",
        notification_id="shared-notify-1",
        event_type_code=1,
        event_type_name="ORDER_STATUS_CHANGE",
        platform_shop_id="shop-1",
        instance_id=1,
        tenant_id=3,
        store_id=11,
        payload_cipher="cipher",
        payload_sha256="sha",
    )
    duplicate_id, duplicate = store_b.create_webhook_event(
        channel="tiktok",
        notification_id="shared-notify-1",
        event_type_code=1,
        event_type_name="ORDER_STATUS_CHANGE",
        platform_shop_id="shop-1",
        instance_id=1,
        tenant_id=3,
        store_id=11,
        payload_cipher="cipher",
        payload_sha256="sha",
    )
    assert duplicate is True and duplicate_id == event_id
    claimed = store_a.claim_webhook_events(
        limit=1, lease_seconds=30, tenant_limit=8, store_limit=1
    )
    assert len(claimed) == 1
    assert store_b.claim_webhook_events(
        limit=1, lease_seconds=30, tenant_limit=8, store_limit=1
    ) == []
