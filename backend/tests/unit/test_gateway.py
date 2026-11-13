"""task-35：独立集成网关与实例身份测试（纯本地 SQLite / TestClient，不连 MySQL/Redis/外网）。

覆盖：
* 网关模块不依赖管理中心 app.core/app.db/Redis/Celery（独立部署）；
* 实例身份：一次性令牌、双实例渠道/店铺/回调隔离、令牌轮换；
* OAuth 一次性 state：绑定实例/店铺/渠道，回调按实例转发，重放 410、过期 410、渠道不符 400；
* 刷新锁并发互斥、过期抢占；
* 统一限流（实例+路径，429 + Retry-After）与实例间隔离；
* 幂等交付：同 Idempotency-Key 同响应、请求体冲突 409、重试上限 failed；
* 迁移/回滚开关：全部实例验证后才允许撤销 legacy，回滚可恢复；
* SQLite 重启持久化、渠道密钥落库加密。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.modules.gateway.app import create_app
from app.modules.gateway.config import GatewayConfig
from app.modules.gateway.security import decrypt_secret

BACKEND_DIR = Path(__file__).resolve().parents[2]
ADMIN_TOKEN = "test-admin-token"
SECRET_KEY = "test-gateway-secret-key-32-bytes-minimum"
ADMIN_HEADERS = {"X-Gateway-Token": ADMIN_TOKEN}


def make_config(db_path: str, **overrides) -> GatewayConfig:
    values = dict(
        db_path=db_path,
        secret_key=SECRET_KEY,
        admin_token=ADMIN_TOKEN,
        channels=("shopee", "tiktok"),
        environments=("production", "test"),
        rate_limit_per_minute=1000,
        delivery_max_attempts=2,
        oauth_state_ttl_seconds=600,
        refresh_lock_ttl_seconds=60,
        legacy_default_callback="https://legacy.example.com/callback",
    )
    values.update(overrides)
    return GatewayConfig(**values)


@pytest.fixture()
def gateway(tmp_path):
    config = make_config(str(tmp_path / "gateway.db"))
    app = create_app(config)
    with TestClient(app) as client:
        yield client, app, config


def create_instance(
    client: TestClient,
    *,
    tenant_id: int,
    name: str,
    callback_url: str,
    store_ids: list[int],
    channels: list[str] | None = None,
    environments: list[str] | None = None,
) -> dict:
    response = client.post(
        "/api/v1/gateway/admin/instances",
        headers=ADMIN_HEADERS,
        json={
            "tenant_id": tenant_id,
            "name": name,
            "channels": channels or ["shopee", "tiktok"],
            "environments": environments or ["test"],
            "store_ids": store_ids,
            "callback_url": callback_url,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def instance_headers(instance: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {instance['instance_token']}"}


def put_channel_app(client: TestClient, channel: str = "shopee", environment: str = "test") -> None:
    response = client.put(
        f"/api/v1/gateway/channel-apps/{channel}/{environment}",
        headers=ADMIN_HEADERS,
        json={
            "app_key": f"{channel}-app-key",
            "app_secret": f"{channel}-super-secret",
            "region": "sg",
            "extra": {"app_id": "app-1", "redirect_url": "https://erp.example.com/cb"},
        },
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- 独立性 / 健康
def test_gateway_module_has_no_center_dependencies() -> None:
    code = (
        "import sys; import app.modules.gateway.app;"
        "bad=[m for m in sys.modules if m.startswith(('app.core','app.db','app.modules.identity','app.modules.sales','app.modules.tasks','celery')) or m in ('redis',)];"
        "print(','.join(bad))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_DIR)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_health_and_admin_auth(gateway) -> None:
    client, _app, _cfg = gateway
    assert client.get("/healthz").status_code == 200
    health = client.get("/api/v1/gateway/health").json()
    assert health["ok"] is True and health["service"] == "erp-gateway"
    assert health["flags"]["instance_auth_enabled"] is True
    assert client.get("/api/v1/gateway/admin/instances").status_code == 401
    assert (
        client.get(
            "/api/v1/gateway/admin/instances", headers={"X-Gateway-Token": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.get("/api/v1/gateway/admin/instances", headers=ADMIN_HEADERS).status_code
        == 200
    )


# --------------------------------------------------------------------------- 实例身份
def test_two_instances_scope_and_callback_isolation(gateway) -> None:
    client, _app, _cfg = gateway
    put_channel_app(client, "shopee", "test")
    put_channel_app(client, "tiktok", "test")
    a = create_instance(
        client,
        tenant_id=1,
        name="tenant-a",
        callback_url="https://tenant-a.example.com/oauth/cb",
        store_ids=[11, 12],
        channels=["shopee", "tiktok"],
    )
    b = create_instance(
        client,
        tenant_id=2,
        name="tenant-b",
        callback_url="https://tenant-b.example.com/oauth/cb",
        store_ids=[21],
        channels=["shopee"],
    )
    a_token = instance_headers(a)
    b_token = instance_headers(b)

    assert client.get("/api/v1/gateway/instance/me", headers=a_token).status_code == 200
    me_b = client.get("/api/v1/gateway/instance/me", headers=b_token).json()
    assert me_b["instance"]["tenant_id"] == "2"

    # 渠道白名单隔离
    assert (
        client.get(
            "/api/v1/gateway/instance/channel-apps/shopee/test", headers=a_token
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/gateway/instance/channel-apps/tiktok/test", headers=b_token
        ).status_code
        == 403
    )
    # 店铺白名单隔离：B 只能 21，A 只能 11/12
    assert (
        client.post(
            "/api/v1/gateway/instance/oauth/states",
            headers=b_token,
            json={"channel": "shopee", "environment": "test", "store_id": 21},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/gateway/instance/oauth/states",
            headers=a_token,
            json={"channel": "shopee", "environment": "test", "store_id": 21},
        ).status_code
        == 403
    )
    # 无令牌/伪造令牌
    assert client.get("/api/v1/gateway/instance/me").status_code == 401
    assert (
        client.get(
            "/api/v1/gateway/instance/me", headers={"Authorization": "Bearer fake"}
        ).status_code
        == 401
    )
    # 令牌轮换后旧令牌立即失效
    old_token = a["instance_token"]
    rotated = client.post(
        f"/api/v1/gateway/admin/instances/{a['instance']['id']}/rotate-token",
        headers=ADMIN_HEADERS,
    )
    assert rotated.status_code == 200
    new_token = rotated.json()["instance_token"]
    assert new_token != old_token
    assert (
        client.get(
            "/api/v1/gateway/instance/me",
            headers={"Authorization": f"Bearer {old_token}"},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/gateway/instance/me",
            headers={"Authorization": f"Bearer {new_token}"},
        ).status_code
        == 200
    )


def test_instance_without_channel_scope_cannot_fetch(gateway) -> None:
    client, _app, _cfg = gateway
    put_channel_app(client, "shopee", "test")
    instance = create_instance(
        client,
        tenant_id=3,
        name="tenant-c",
        callback_url="https://tenant-c.example.com/cb",
        store_ids=[31],
        channels=["shopee"],
        environments=["test"],
    )
    headers = instance_headers(instance)
    # 环境白名单隔离
    assert (
        client.get(
            "/api/v1/gateway/instance/channel-apps/shopee/production", headers=headers
        ).status_code
        in {400, 403, 404}
    )


# --------------------------------------------------------------------------- OAuth state
def test_oauth_state_one_time_and_instance_callback_isolation(gateway) -> None:
    client, _app, _cfg = gateway
    put_channel_app(client, "shopee", "test")
    a = create_instance(
        client,
        tenant_id=1,
        name="a",
        callback_url="https://tenant-a.example.com/cb",
        store_ids=[11],
        channels=["shopee"],
    )
    b = create_instance(
        client,
        tenant_id=2,
        name="b",
        callback_url="https://tenant-b.example.com/cb",
        store_ids=[21],
        channels=["shopee"],
    )
    state_a = client.post(
        "/api/v1/gateway/instance/oauth/states",
        headers=instance_headers(a),
        json={"channel": "shopee", "environment": "test", "store_id": 11, "redirect_path": "/stores"},
    ).json()
    state_b = client.post(
        "/api/v1/gateway/instance/oauth/states",
        headers=instance_headers(b),
        json={"channel": "shopee", "environment": "test", "store_id": 21},
    ).json()

    response_a = client.get(
        "/api/v1/gateway/oauth/shopee/callback",
        params={"state": state_a["state"], "code": "CODE-A", "shop_id": "1001"},
        follow_redirects=False,
    )
    assert response_a.status_code == 302
    assert response_a.headers["location"].startswith("https://tenant-a.example.com/cb")
    assert "code=CODE-A" in response_a.headers["location"]
    assert f"gateway_instance_id={a['instance']['id']}" in response_a.headers["location"]

    response_b = client.get(
        "/api/v1/gateway/oauth/shopee/callback",
        params={"state": state_b["state"], "code": "CODE-B"},
        follow_redirects=False,
    )
    assert response_b.status_code == 302
    assert response_b.headers["location"].startswith("https://tenant-b.example.com/cb")

    # state 重放：410
    replay = client.get(
        "/api/v1/gateway/oauth/shopee/callback",
        params={"state": state_a["state"], "code": "CODE-A"},
        follow_redirects=False,
    )
    assert replay.status_code == 410
    assert replay.json()["error"]["code"] == "gateway_oauth_state_used"
    # 未知 state / 渠道不匹配
    assert (
        client.get(
            "/api/v1/gateway/oauth/shopee/callback",
            params={"state": "not-a-state"},
            follow_redirects=False,
        ).status_code
        == 400
    )
    state_c = client.post(
        "/api/v1/gateway/instance/oauth/states",
        headers=instance_headers(a),
        json={"channel": "shopee", "environment": "test", "store_id": 11},
    ).json()
    assert (
        client.get(
            "/api/v1/gateway/oauth/tiktok/callback",
            params={"state": state_c["state"]},
            follow_redirects=False,
        ).status_code
        == 400
    )
    # 无 state 旧固定回调：legacy 开关开启时回退
    legacy = client.get("/api/v1/gateway/oauth/shopee/callback", follow_redirects=False)
    assert legacy.status_code == 307
    assert legacy.headers["location"].startswith("https://legacy.example.com/callback")


def test_oauth_state_expiry(gateway) -> None:
    client, app, _cfg = gateway
    put_channel_app(client, "shopee", "test")
    instance = create_instance(
        client,
        tenant_id=1,
        name="a",
        callback_url="https://tenant-a.example.com/cb",
        store_ids=[11],
        channels=["shopee"],
    )
    state = client.post(
        "/api/v1/gateway/instance/oauth/states",
        headers=instance_headers(instance),
        json={"channel": "shopee", "environment": "test", "store_id": 11},
    ).json()
    store = app.state.gateway_store
    from app.modules.gateway.security import oauth_state_hash

    state_hash = oauth_state_hash(_cfg, state["state"])
    with store._write() as connection:
        connection.execute(
            "UPDATE gateway_oauth_states SET expires_at = 1 WHERE state_hash = ?",
            (state_hash,),
        )
    expired = client.get(
        "/api/v1/gateway/oauth/shopee/callback",
        params={"state": state["state"]},
        follow_redirects=False,
    )
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "gateway_oauth_state_expired"


# --------------------------------------------------------------------------- 刷新锁
def test_refresh_lock_mutex_and_concurrency(gateway) -> None:
    client, app, _cfg = gateway
    instance = create_instance(
        client,
        tenant_id=1,
        name="a",
        callback_url="https://tenant-a.example.com/cb",
        store_ids=[11],
        channels=["shopee"],
    )
    headers = instance_headers(instance)
    first = client.post(
        "/api/v1/gateway/instance/refresh-locks",
        headers=headers,
        json={"lock_key": "store-11", "owner": "worker-1"},
    )
    assert first.status_code == 200
    conflict = client.post(
        "/api/v1/gateway/instance/refresh-locks",
        headers=headers,
        json={"lock_key": "store-11", "owner": "worker-2"},
    )
    assert conflict.status_code == 409
    assert "Retry-After" in conflict.headers
    wrong_owner = client.post(
        "/api/v1/gateway/instance/refresh-locks/store-11/release",
        headers=headers,
        json={"owner": "worker-2"},
    )
    assert wrong_owner.json()["released"] is False
    released = client.post(
        "/api/v1/gateway/instance/refresh-locks/store-11/release",
        headers=headers,
        json={"owner": "worker-1"},
    )
    assert released.json()["released"] is True
    second = client.post(
        "/api/v1/gateway/instance/refresh-locks",
        headers=headers,
        json={"lock_key": "store-11", "owner": "worker-2"},
    )
    assert second.status_code == 200

    # 并发抢占：只有一个成功
    store = app.state.gateway_store
    results: list[bool] = []

    def acquire(owner: str) -> None:
        acquired, _existing = store.acquire_refresh_lock(
            lock_key=f"{instance['instance']['id']}:concurrent-lock",
            instance_id=int(instance["instance"]["id"]),
            owner=owner,
            ttl_seconds=30,
        )
        results.append(acquired is not None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(acquire, f"w{i}") for i in range(2)]
        for future in futures:
            future.result()
    assert results.count(True) == 1


# --------------------------------------------------------------------------- 限流
def test_rate_limit_per_instance(gateway) -> None:
    client, _app, _cfg = gateway
    a = create_instance(
        client,
        tenant_id=1,
        name="a",
        callback_url="https://tenant-a.example.com/cb",
        store_ids=[11],
        channels=["shopee"],
    )
    b = create_instance(
        client,
        tenant_id=2,
        name="b",
        callback_url="https://tenant-b.example.com/cb",
        store_ids=[21],
        channels=["shopee"],
    )
    # 另建低限流 app（独立 SQLite），验证实例+路径限流与实例间隔离
    config = make_config(str(Path(_app.state.gateway_config.db_path).parent / "limited.db"), rate_limit_per_minute=2)
    limited_app = create_app(config)
    with TestClient(limited_app) as limited:
        a2 = create_instance(
            limited,
            tenant_id=1,
            name="a2",
            callback_url="https://tenant-a.example.com/cb",
            store_ids=[11],
            channels=["shopee"],
        )
        b2 = create_instance(
            limited,
            tenant_id=2,
            name="b2",
            callback_url="https://tenant-b.example.com/cb",
            store_ids=[21],
            channels=["shopee"],
        )
        assert limited.get("/api/v1/gateway/instance/me", headers=instance_headers(a2)).status_code == 200
        assert limited.get("/api/v1/gateway/instance/me", headers=instance_headers(a2)).status_code == 200
        blocked = limited.get("/api/v1/gateway/instance/me", headers=instance_headers(a2))
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        # 另一个实例独立计数
        assert limited.get("/api/v1/gateway/instance/me", headers=instance_headers(b2)).status_code == 200


# --------------------------------------------------------------------------- 幂等交付
def test_idempotent_delivery_and_retry_limit(gateway) -> None:
    client, _app, _cfg = gateway
    instance = create_instance(
        client,
        tenant_id=1,
        name="a",
        callback_url="https://tenant-a.example.com/cb",
        store_ids=[11],
        channels=["shopee"],
    )
    other = create_instance(
        client,
        tenant_id=2,
        name="b",
        callback_url="https://tenant-b.example.com/cb",
        store_ids=[21],
        channels=["shopee"],
    )
    headers = {**instance_headers(instance), "Idempotency-Key": "order-1"}
    body = {"kind": "order_sync", "target": "https://tenant-a.example.com/hook", "payload": {"order": "A1"}}
    first = client.post("/api/v1/gateway/instance/deliveries", headers=headers, json=body)
    assert first.status_code == 200, first.text
    delivery = first.json()["delivery"]
    assert first.json()["replayed"] is False
    assert delivery["status"] == "pending" and delivery["attempts"] == 0
    replay = client.post("/api/v1/gateway/instance/deliveries", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["delivery"]["id"] == delivery["id"]
    conflict = client.post(
        "/api/v1/gateway/instance/deliveries",
        headers=headers,
        json={**body, "payload": {"order": "A2"}},
    )
    assert conflict.status_code == 409
    assert (
        client.post(
            "/api/v1/gateway/instance/deliveries",
            headers=instance_headers(instance),
            json=body,
        ).status_code
        == 422
    )
    # 重试上限：test config delivery_max_attempts=2
    retry1 = client.post(
        f"/api/v1/gateway/instance/deliveries/{delivery['id']}/retry",
        headers=instance_headers(instance),
        json={"error": "timeout"},
    )
    assert retry1.json()["delivery"]["attempts"] == 1
    assert retry1.json()["delivery"]["status"] == "pending"
    retry2 = client.post(
        f"/api/v1/gateway/instance/deliveries/{delivery['id']}/retry",
        headers=instance_headers(instance),
        json={"error": "timeout"},
    )
    assert retry2.json()["delivery"]["attempts"] == 2
    assert retry2.json()["delivery"]["status"] == "failed"
    retry3 = client.post(
        f"/api/v1/gateway/instance/deliveries/{delivery['id']}/retry",
        headers=instance_headers(instance),
        json={},
    )
    assert retry3.json()["delivery"]["attempts"] == 2
    # 其他实例不可见
    assert (
        client.get(
            f"/api/v1/gateway/instance/deliveries/{delivery['id']}",
            headers=instance_headers(other),
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- 迁移 / 回滚
def test_migration_requires_verified_instances_and_rollback(gateway) -> None:
    client, _app, _cfg = gateway
    put_channel_app(client, "shopee", "test")
    a = create_instance(
        client,
        tenant_id=1,
        name="a",
        callback_url="https://tenant-a.example.com/cb",
        store_ids=[11],
        channels=["shopee"],
    )
    b = create_instance(
        client,
        tenant_id=2,
        name="b",
        callback_url="https://tenant-b.example.com/cb",
        store_ids=[21],
        channels=["shopee"],
    )
    # 实例真实调用新链后才能 verify
    for instance in (a, b):
        assert (
            client.get(
                "/api/v1/gateway/instance/me", headers=instance_headers(instance)
            ).status_code
            == 200
        )
    for instance in (a, b):
        verify = client.post(
            f"/api/v1/gateway/admin/instances/{instance['instance']['id']}/verify",
            headers=ADMIN_HEADERS,
            json={},
        )
        assert verify.status_code == 200, verify.text
    migration = client.get("/api/v1/gateway/admin/migration", headers=ADMIN_HEADERS).json()
    assert migration["instances"]["all_active_verified"] is True
    assert migration["legacy_tenant_admin_enabled"] is True
    # 撤销 legacy 前 legacy internal 可用
    assert (
        client.get(
            "/api/v1/gateway/channel-apps/shopee/test/internal", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    switched = client.post(
        "/api/v1/gateway/admin/migration/switch",
        headers=ADMIN_HEADERS,
        json={"revoke_legacy": True},
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["legacy_tenant_admin_enabled"] is False
    assert switched.json()["legacy_revoked_at"]
    # legacy 租户 admin token 被拒绝；实例新链仍可用
    assert (
        client.get(
            "/api/v1/gateway/channel-apps/shopee/test/internal", headers=ADMIN_HEADERS
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/gateway/instance/channel-apps/shopee/test",
            headers=instance_headers(a),
        ).status_code
        == 200
    )
    # 回滚开关恢复 legacy
    rollback = client.post(
        "/api/v1/gateway/admin/migration/switch",
        headers=ADMIN_HEADERS,
        json={"legacy_tenant_admin_enabled": True, "legacy_oauth_fallback_enabled": True},
    )
    assert rollback.status_code == 200
    assert rollback.json()["legacy_tenant_admin_enabled"] is True
    assert (
        client.get(
            "/api/v1/gateway/channel-apps/shopee/test/internal", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )


def test_migration_revoke_rejected_when_instance_unverified(gateway) -> None:
    client, _app, _cfg = gateway
    instance = create_instance(
        client,
        tenant_id=1,
        name="a",
        callback_url="https://tenant-a.example.com/cb",
        store_ids=[11],
        channels=["shopee"],
    )
    client.get("/api/v1/gateway/instance/me", headers=instance_headers(instance))
    response = client.post(
        "/api/v1/gateway/admin/migration/switch",
        headers=ADMIN_HEADERS,
        json={"revoke_legacy": True},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "gateway_migration_not_verified"


def test_legacy_disabled_blocks_internal_but_admin_api_works(gateway) -> None:
    client, _app, _cfg = gateway
    put_channel_app(client, "shopee", "test")
    client.post(
        "/api/v1/gateway/admin/migration/switch",
        headers=ADMIN_HEADERS,
        json={"legacy_tenant_admin_enabled": False, "legacy_oauth_fallback_enabled": False},
    )
    assert (
        client.get(
            "/api/v1/gateway/channel-apps/shopee/test/internal", headers=ADMIN_HEADERS
        ).status_code
        == 403
    )
    # 平台管理 API 保留
    assert client.get("/api/v1/gateway/channel-apps", headers=ADMIN_HEADERS).status_code == 200
    # 无 state 的旧固定回调被拒绝
    assert (
        client.get("/api/v1/gateway/oauth/shopee/callback", follow_redirects=False).status_code
        == 400
    )


# --------------------------------------------------------------------------- 租户侧客户端（新链 / legacy 回退）
def test_gateway_client_paths_and_headers() -> None:
    import httpx

    from app.modules.gateway.client import GatewayClient

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"app_key": "k", "app_secret": "s"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    client = GatewayClient("http://gateway.local", http_client=http_client)
    data = client.fetch_instance_channel_app(
        instance_id="7", instance_token="token-7", channel="shopee", environment="test"
    )
    assert data["app_key"] == "k"
    request = captured[0]
    assert request.url.path == "/api/v1/gateway/instance/channel-apps/shopee/test"
    assert request.headers["authorization"] == "Bearer token-7"
    assert request.headers["x-gateway-instance-id"] == "7"
    client.fetch_legacy_channel_app(
        admin_token="admin-token", channel="shopee", environment="test"
    )
    legacy_request = captured[1]
    assert legacy_request.url.path == "/api/v1/gateway/channel-apps/shopee/test/internal"
    assert legacy_request.headers["x-gateway-token"] == "admin-token"
    http_client.close()


def test_gateway_client_raises_on_http_error() -> None:
    import httpx

    from app.modules.gateway.client import GatewayCallError, GatewayClient

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403, json={"error": {"code": "gateway_instance_channel_denied", "message": "denied"}}
        )
    )
    client = GatewayClient("http://gateway.local", http_client=httpx.Client(transport=transport))
    with pytest.raises(GatewayCallError) as exc_info:
        client.fetch_instance_channel_app(
            instance_id="7", instance_token="token-7", channel="shopee", environment="test"
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "gateway_instance_channel_denied"


class _FakeGatewayClient:
    calls: list[str] = []
    instance_error: Exception | None = None
    legacy_error: Exception | None = None
    payload: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    def fetch_instance_channel_app(self, **kwargs):
        self.calls.append("instance")
        if self.instance_error:
            raise self.instance_error
        return dict(self.payload or {})

    def fetch_legacy_channel_app(self, **kwargs):
        self.calls.append("legacy")
        if self.legacy_error:
            raise self.legacy_error
        return dict(self.payload or {})

    def close(self) -> None:
        pass


def _gateway_channel_app_payload() -> dict:
    return {
        "app_key": "shopee-key",
        "app_secret": "shopee-secret",
        "region": "sg",
        "extra": json.dumps({"partner_id": 123, "redirect_url": "https://erp.example.com/cb"}),
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_channels_use_instance_token_first_and_fallback_legacy(monkeypatch) -> None:
    from app.core.config import settings
    from app.modules.channels import service as channels_service
    from app.modules.gateway import client as gateway_client_module
    from app.modules.gateway.client import GatewayCallError

    fake = _FakeGatewayClient()
    fake.calls = []
    fake.payload = _gateway_channel_app_payload()
    fake.instance_error = None
    fake.legacy_error = None
    monkeypatch.setattr(gateway_client_module, "GatewayClient", lambda *a, **k: fake)
    monkeypatch.setattr(settings, "gateway_channel_config", True)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.local")
    monkeypatch.setattr(settings, "gateway_instance_id", "7")
    monkeypatch.setattr(settings, "gateway_instance_token", "instance-token")
    monkeypatch.setattr(settings, "gateway_admin_token", "admin-token")
    monkeypatch.setattr(settings, "gateway_legacy_admin_enabled", True)

    config = channels_service._load_gateway_channel_app("shopee", "test")
    assert config is not None
    assert fake.calls == ["instance"]
    # Shopee 使用 partner_key/partner_secret 承载网关 app_key/app_secret
    assert config.partner_key == "shopee-key"
    assert config.partner_secret == "shopee-secret"
    assert config.partner_id == 123
    assert config.redirect_url == "https://erp.example.com/cb"

    # 新链失败 → legacy 回退（迁移期开关开启）
    fake.calls = []
    fake.instance_error = GatewayCallError("token revoked", status_code=401)
    legacy_config = channels_service._load_gateway_channel_app("shopee", "test")
    assert fake.calls == ["instance", "legacy"]
    assert legacy_config is not None and legacy_config.partner_key == "shopee-key"

    # legacy 回退关闭 → 新链失败直接返回 None（由调用方回退本地中心库配置）
    fake.calls = []
    monkeypatch.setattr(settings, "gateway_legacy_admin_enabled", False)
    assert channels_service._load_gateway_channel_app("shopee", "test") is None
    assert fake.calls == ["instance"]

    # 未配置实例令牌且 legacy 关闭：完全不调用网关
    fake.calls = []
    monkeypatch.setattr(settings, "gateway_instance_id", "")
    monkeypatch.setattr(settings, "gateway_instance_token", "")
    assert channels_service._load_gateway_channel_app("shopee", "test") is None
    assert fake.calls == []

    # 迁移期：仅 legacy admin token 也可工作
    fake.calls = []
    monkeypatch.setattr(settings, "gateway_legacy_admin_enabled", True)
    legacy_only = channels_service._load_gateway_channel_app("shopee", "test")
    assert legacy_only is not None
    assert fake.calls == ["legacy"]


# --------------------------------------------------------------------------- 持久化 / 加密 / 审计
def test_sqlite_persistence_and_secret_encryption(gateway) -> None:
    client, _app, cfg = gateway
    put_channel_app(client, "shopee", "test")
    instance = create_instance(
        client,
        tenant_id=1,
        name="a",
        callback_url="https://tenant-a.example.com/cb",
        store_ids=[11],
        channels=["shopee"],
    )
    # 密钥落库为密文
    with sqlite3.connect(cfg.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT secret_cipher FROM gateway_channel_apps WHERE channel='shopee' AND environment='test'"
        ).fetchone()
    assert row is not None
    cipher = row["secret_cipher"]
    assert "shopee-super-secret" not in cipher
    assert decrypt_secret(cfg, cipher) == "shopee-super-secret"
    # 重建 app（模拟网关重启），实例令牌/渠道配置仍在
    app2 = create_app(cfg)
    with TestClient(app2) as client2:
        assert (
            client2.get(
                "/api/v1/gateway/instance/channel-apps/shopee/test",
                headers=instance_headers(instance),
            ).status_code
            == 200
        )
        assert client2.get("/api/v1/gateway/channel-apps", headers=ADMIN_HEADERS).status_code == 200
    # 审计记录
    logs = client.get("/api/v1/gateway/admin/audit-logs", headers=ADMIN_HEADERS).json()
    actions = [item["action"] for item in logs["items"]]
    assert "instance.create" in actions
    assert "channel_app.update" in actions
