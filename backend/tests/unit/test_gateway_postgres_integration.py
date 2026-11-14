"""PostgreSQL HA integration tests for the gateway (opt-in; requires psycopg + PG).

Run only when ``GATEWAY_TEST_POSTGRES_URL`` points at a disposable database:

    GATEWAY_TEST_POSTGRES_URL=postgresql://gateway:secret@127.0.0.1:5432/gateway_test \
      python -m pytest tests/unit/test_gateway_postgres_integration.py -q

These tests intentionally do not skip the feature implementation when PostgreSQL is
absent from a developer machine; they validate it when the HA stack is available.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.modules.gateway.app import create_app
from app.modules.gateway.config import GatewayConfig
from app.modules.gateway.security import encrypt_secret
from app.modules.gateway.store import GatewayStore
from app.modules.gateway.webhook import tiktok_signature

PG_URL = os.environ.get("GATEWAY_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not PG_URL, reason="设置 GATEWAY_TEST_POSTGRES_URL 后运行 PostgreSQL 双实例集成测试"
)
ADMIN_TOKEN = "pg-admin"
SECRET_KEY = "pg-gateway-secret-key-32-bytes-minimum"
RELAY_SECRET = "pg-relay-secret"
TABLES = (
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
)


def _clean_pg() -> None:
    import psycopg

    GatewayStore(pg_config()).init()
    with psycopg.connect(PG_URL, autocommit=True) as connection:
        for table in TABLES:
            connection.execute(f"TRUNCATE {table} RESTART IDENTITY CASCADE")


def pg_config(**overrides) -> GatewayConfig:
    values = dict(
        secret_key=SECRET_KEY,
        admin_token=ADMIN_TOKEN,
        database_url=PG_URL,
        webhook_dispatch_enabled=False,
        webhook_relay_secret=RELAY_SECRET,
        webhook_retry_base_seconds=5,
        webhook_retry_jitter_percent=0,
    )
    values.update(overrides)
    return GatewayConfig(**values)


def admin_headers() -> dict[str, str]:
    return {"X-Gateway-Token": ADMIN_TOKEN}


def create_instance(client: TestClient, *, callback: str = "https://tenant.example.com/oauth/cb") -> dict:
    response = client.put(
        "/api/v1/gateway/channel-apps/tiktok/production",
        headers=admin_headers(),
        json={"app_key": "pg-app-key", "app_secret": "pg-app-secret"},
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/api/v1/gateway/admin/instances",
        headers=admin_headers(),
        json={
            "tenant_id": 3,
            "name": "pg-tenant",
            "channels": ["tiktok"],
            "environments": ["production"],
            "store_ids": [11],
            "callback_url": callback,
        },
    )
    assert response.status_code == 200, response.text
    instance = response.json()
    response = client.post(
        "/api/v1/gateway/instance/webhook-shops",
        headers={"Authorization": f"Bearer {instance['instance_token']}"},
        json={"store_id": 11, "platform_shop_id": "pg-shop-1"},
    )
    assert response.status_code == 200, response.text
    return instance


def signed_tiktok_body(notification_id: str) -> tuple[bytes, str]:
    body = json.dumps(
        {"tts_notification_id": notification_id, "type": 1, "shop_id": "pg-shop-1"}
    ).encode()
    signature = tiktok_signature(
        app_key="pg-app-key", app_secret="pg-app-secret", raw_body=body
    )
    return body, signature


def test_dual_gateway_ingress_deduplicates_and_failover(monkeypatch) -> None:
    import app.modules.gateway.app as gateway_app

    _clean_pg()
    config = pg_config()
    app_a = create_app(config)
    app_b = create_app(config)
    with TestClient(app_a) as client_a:
        instance = create_instance(client_a)
        body, signature = signed_tiktok_body("pg-notify-1")
        # 同一事件打到 A（模拟 LB 分发）。
        assert client_a.post(
            "/api/v1/gateway/webhooks/tiktok",
            content=body,
            headers={"X-TikTok-Signature": signature},
        ).status_code == 200
    # A 已停止；B 继续接收同一事件，然后领取/投递 A 写入的 pending。
    with TestClient(app_b) as client_b:
        assert client_b.post(
            "/api/v1/gateway/webhooks/tiktok",
            content=body,
            headers={"X-TikTok-Signature": signature},
        ).status_code == 200
        stats = client_b.get(
            "/api/v1/gateway/admin/webhook-stats", headers=admin_headers()
        ).json()["webhook"]
        assert stats["total"] == 1
        assert stats["pending"] == 1
        assert client_b.get("/healthz").json()["webhook"]["total"] == 1
        claimed = app_b.state.gateway_store.claim_webhook_events(
            limit=1, lease_seconds=30, tenant_limit=8, store_limit=1
        )
        assert len(claimed) == 1

        class FakeResponse:
            status = 200
            headers: dict = {}

            def read(self, size=-1):
                return json.dumps(
                    {"received": True, "duplicate": False, "event_id": "1"}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(gateway_app, "urlopen", lambda request, timeout=0: FakeResponse())
        app_b.state.webhook_deliver_event(claimed[0], claimed[0]["lease_token"])
        delivered = client_b.get(
            "/api/v1/gateway/admin/webhook-stats", headers=admin_headers()
        ).json()["webhook"]
        assert delivered["delivered"] == 1
        assert delivered["pending"] == 0
        assert instance["tenant_id"] == "3"
    _clean_pg()


def test_shared_oauth_state_and_refresh_lock_across_instances() -> None:
    _clean_pg()
    config = pg_config()
    store_a = GatewayStore(config)
    store_b = GatewayStore(config)
    store_a.init()
    store_b.init()
    instance = store_a.create_instance(
        instance_uuid="pg-instance-1",
        tenant_id=3,
        name="pg",
        channels=["tiktok"],
        environments=["production"],
        store_ids=[11],
        callback_url="https://tenant.example.com/oauth/cb",
        token_hash="hash",
    )
    instance_id = int(instance["id"])
    state = store_a.create_oauth_state(
        state_hash="pg-state-hash",
        instance_id=instance_id,
        tenant_id=3,
        store_id=11,
        channel="tiktok",
        environment="production",
        redirect_path="/cb",
        ttl_seconds=600,
        context={"link": 1},
    )
    consumed, reason = store_b.consume_oauth_state("pg-state-hash", channel="tiktok")
    assert reason == "" and consumed is not None
    resolved, reason = store_a.resolve_oauth_state("pg-state-hash", instance_id=instance_id)
    assert reason == "" and resolved is not None and resolved["context"] == {"link": 1}
    assert store_b.resolve_oauth_state("pg-state-hash", instance_id=instance_id)[1] == "replayed"

    lock_a, existing = store_a.acquire_refresh_lock(
        lock_key="pg-lock", instance_id=instance_id, owner="worker-a", ttl_seconds=60
    )
    assert lock_a is not None and existing is None
    lock_b, existing = store_b.acquire_refresh_lock(
        lock_key="pg-lock", instance_id=instance_id, owner="worker-b", ttl_seconds=60
    )
    assert lock_b is None and existing is not None and existing["owner"] == "worker-a"
    assert store_a.release_refresh_lock(lock_key="pg-lock", owner="worker-a") is True
    lock_b, existing = store_b.acquire_refresh_lock(
        lock_key="pg-lock", instance_id=instance_id, owner="worker-b", ttl_seconds=60
    )
    assert lock_b is not None and existing is None
    assert state["tenant_id"] == "3"
    _clean_pg()


def test_sqlite_to_postgres_migration_reconciles_pending(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    sqlite_path = tmp_path / "gateway-source.db"
    source_cfg = GatewayConfig(
        db_path=str(sqlite_path),
        secret_key=SECRET_KEY,
        admin_token=ADMIN_TOKEN,
        webhook_dispatch_enabled=False,
        webhook_relay_secret=RELAY_SECRET,
    )
    source = GatewayStore(source_cfg)
    source.init()
    source.create_instance(
        instance_uuid="migration-instance",
        tenant_id=3,
        name="migrate",
        channels=["tiktok"],
        environments=["production"],
        store_ids=[11],
        callback_url="https://tenant.example.com/oauth/cb",
        token_hash="hash",
    )
    source.upsert_webhook_shop(
        channel="tiktok",
        platform_shop_id="pg-shop-1",
        instance_id=1,
        tenant_id=3,
        store_id=11,
    )
    source.create_webhook_event(
        channel="tiktok",
        notification_id="migrate-notify-1",
        event_type_code=1,
        event_type_name="ORDER_STATUS_CHANGE",
        platform_shop_id="pg-shop-1",
        instance_id=1,
        tenant_id=3,
        store_id=11,
        payload_cipher=encrypt_secret(source_cfg, "{}"),
        payload_sha256="sha",
    )
    _clean_pg()
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "gateway_ha_migrate.py"),
            "--sqlite",
            str(sqlite_path),
            "--postgres",
            PG_URL,
            "--apply",
            "--confirm",
            "STOP-OLD-GATEWAY",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(repo_root / "backend")},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert '"ok": true' in result.stdout
    target = GatewayStore(pg_config())
    target.init()
    stats = target.webhook_stats()
    assert stats["total"] == 1 and stats["pending"] == 1
    _clean_pg()
