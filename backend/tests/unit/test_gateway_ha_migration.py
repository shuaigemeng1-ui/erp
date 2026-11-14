"""Migration tool preflight checks that do not require a PostgreSQL server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.modules.gateway.config import GatewayConfig
from app.modules.gateway.store import GatewayStore


def test_ha_migration_dry_run_reports_source_without_writing(tmp_path) -> None:
    sqlite_path = tmp_path / "gateway-source.db"
    cfg = GatewayConfig(
        db_path=str(sqlite_path),
        secret_key="migration-test-secret",
        admin_token="migration-test-admin",
        webhook_dispatch_enabled=False,
        webhook_relay_secret="migration-test-relay",
    )
    store = GatewayStore(cfg)
    store.init()
    store.create_webhook_event(
        channel="tiktok",
        notification_id="dry-run-notify-1",
        event_type_code=1,
        event_type_name="ORDER_STATUS_CHANGE",
        platform_shop_id="shop-1",
        instance_id=None,
        tenant_id=3,
        store_id=11,
        payload_cipher="cipher",
        payload_sha256="sha",
    )
    before = sqlite_path.read_bytes()
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "gateway_ha_migrate.py"),
            "--sqlite",
            str(sqlite_path),
            "--postgres",
            "postgresql://gateway:secret@pg-ha:5432/gateway",
        ],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(repo_root / "backend")},
    )
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout.split("\n[plan]", 1)[0])
    assert payload["apply"] is False
    assert payload["source_counts"]["gateway_webhook_events"] == 1
    assert payload["source_webhook_statuses"] == {"pending": 1}
    assert payload["source_pending"] == 1
    assert payload["target_initialized"] is False
    assert sqlite_path.read_bytes() == before
