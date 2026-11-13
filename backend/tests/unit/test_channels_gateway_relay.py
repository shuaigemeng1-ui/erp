"""task-37：channels 接入网关 OAuth relay 与令牌刷新锁（纯 mock，不连网/DB/Redis）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.errors import AppError, ErrorCode
from app.modules.channels import authorization, gateway_relay, service
from app.modules.channels.shopee import oauth as shopee_oauth
from app.modules.channels.tiktok import oauth as tiktok_oauth
from app.modules.gateway.client import GatewayCallError


@pytest.fixture()
def gateway_settings(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "gateway_channel_config", True)
    monkeypatch.setattr(settings, "gateway_base_url", "http://gateway.internal:8100")
    monkeypatch.setattr(settings, "gateway_instance_id", "7")
    monkeypatch.setattr(settings, "gateway_instance_token", "inst-token-7")
    monkeypatch.setattr(settings, "gateway_timeout_seconds", 5.0)
    monkeypatch.setenv("GATEWAY_OAUTH_RELAY_ENABLED", "1")
    monkeypatch.setenv("GATEWAY_OAUTH_RELAY_LEGACY_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("GATEWAY_PUBLIC_URL", "https://gateway.example.com")
    return settings


def test_relay_and_callback_url_flags(gateway_settings, monkeypatch) -> None:
    assert gateway_relay.relay_enabled() is True
    assert gateway_relay.gateway_callback_url("shopee") == (
        "https://gateway.example.com/api/v1/gateway/oauth/shopee/callback"
    )
    monkeypatch.setenv("GATEWAY_OAUTH_RELAY_ENABLED", "0")
    assert gateway_relay.relay_enabled() is False


def test_create_gateway_state_binds_link_and_context(gateway_settings, monkeypatch) -> None:
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def create_oauth_state(self, **kwargs):
            calls.append(kwargs)
            return {"state": "gw-state-1", "expires_at": 123456}

        def close(self):
            pass

    monkeypatch.setattr(gateway_relay, "GatewayClient", FakeClient)
    row = SimpleNamespace(
        id=91,
        tenant_id=2,
        channel="shopee",
        store_id=11,
        context={
            "tenant_id": 2,
            "member_id": 5,
            "environment": "production",
            "redirect_path": "/tenants/2/stores",
            "store_id": 11,
            "store_code": "S1",
        },
    )
    response = gateway_relay.create_oauth_state(row)
    assert response["state"] == "gw-state-1"
    assert calls[0]["store_id"] == 11
    assert calls[0]["channel"] == "shopee"
    assert calls[0]["environment"] == "production"
    assert calls[0]["context"]["authorization_link_id"] == 91
    assert calls[0]["context"]["member_id"] == 5


def test_consume_state_gateway_then_replay_fallback(gateway_settings, monkeypatch) -> None:
    resolved = {
        "instance_id": "7",
        "tenant_id": "2",
        "store_id": "11",
        "channel": "shopee",
        "environment": "production",
        "redirect_path": "/tenants/2/stores",
        "context": {
            "authorization_link_id": 91,
            "tenant_id": 2,
            "member_id": 5,
            "environment": "production",
            "store_id": 11,
            "redirect_path": "/tenants/2/stores",
            "store_code": "S1",
        },
    }
    monkeypatch.setattr(gateway_relay, "resolve_oauth_state", lambda state: resolved)
    payload = authorization.consume_state("gw-state", "shopee")
    assert payload["authorization_link_id"] == 91
    assert payload["tenant_id"] == 2
    assert payload["store_code"] == "S1"

    # 渠道不匹配拒绝
    with pytest.raises(AppError):
        authorization.consume_state("gw-state", "tiktok")

    # 重放/失效：resolve 抛 410；legacy fallback 关闭时直接失败
    monkeypatch.setattr(
        gateway_relay,
        "resolve_oauth_state",
        lambda state: (_ for _ in ()).throw(
            GatewayCallError("replayed", status_code=410, code="gateway_oauth_state_replayed")
        ),
    )
    monkeypatch.setenv("GATEWAY_OAUTH_RELAY_LEGACY_FALLBACK_ENABLED", "0")
    with pytest.raises(AppError):
        authorization.consume_state("gw-state", "shopee")


def test_gateway_session_wrappers_do_not_require_tenant_signed_state(
    gateway_settings, monkeypatch
) -> None:
    monkeypatch.setattr(
        authorization,
        "consume_state",
        lambda state, channel: {
            "authorization_link_id": 91,
            "tenant_id": 2,
            "member_id": 5,
            "environment": "production",
            "redirect_path": "/tenants/2/stores",
            "store_id": 11,
            "store_code": "S1",
        },
    )
    shopee_session = shopee_oauth.consume_authorization_session("gw-opaque")
    assert shopee_session["authorization_link_id"] == 91
    assert shopee_session["store_id"] == 11
    tiktok_session = tiktok_oauth.consume_authorization_session("gw-opaque")
    assert tiktok_session["authorization_link_id"] == 91


def test_consume_state_falls_back_to_tenant_signed_state(gateway_settings, monkeypatch) -> None:
    from app.core.security import sha256_hex, utc_after, utc_now
    from app.modules.channels.state import build_signed_state

    payload = {
        "tenant_id": 2,
        "member_id": 5,
        "channel": "shopee",
        "environment": "production",
        "redirect_path": "/tenants/2/stores",
        "authorization_link_id": 91,
        "nonce": "nonce-fallback",
        "expires_at": utc_after(600).isoformat(),
        "created_at": utc_now().isoformat(),
    }
    state = build_signed_state(payload)

    class FakeRedis:
        def getdel(self, key):
            assert key == f"vs:oauth:nonce:{sha256_hex('nonce-fallback')}"
            return sha256_hex(state)

    monkeypatch.setattr(authorization, "cache_redis", lambda: FakeRedis())
    monkeypatch.setattr(
        gateway_relay,
        "resolve_oauth_state",
        lambda state: (_ for _ in ()).throw(GatewayCallError("gateway down", status_code=503)),
    )
    monkeypatch.setenv("GATEWAY_OAUTH_RELAY_LEGACY_FALLBACK_ENABLED", "1")
    result = authorization.consume_state(state, "shopee")
    assert result["authorization_link_id"] == 91
    assert result["nonce"] == "nonce-fallback"


def test_refresh_lock_acquire_busy_and_release(gateway_settings, monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_REFRESH_LOCK_ENABLED", "1")
    events: list[str] = []

    class FakeClient:
        mode = "ok"

        def __init__(self, *a, **k):
            pass

        def acquire_refresh_lock(self, **kwargs):
            events.append("acquire")
            if self.mode == "busy":
                raise GatewayCallError("busy", status_code=409, code="conflict")
            if self.mode == "down":
                raise GatewayCallError("down", status_code=503)
            return {"ok": True}

        def release_refresh_lock(self, **kwargs):
            events.append("release")

        def close(self):
            pass

    monkeypatch.setattr(gateway_relay, "GatewayClient", FakeClient)
    lock = gateway_relay.acquire_refresh_lock(
        tenant_id=2, store_id=11, channel="shopee", ttl_seconds=60
    )
    assert isinstance(lock, gateway_relay.GatewayRefreshLock)
    lock.release()
    assert events == ["acquire", "release"]

    FakeClient.mode = "busy"
    assert (
        gateway_relay.acquire_refresh_lock(
            tenant_id=2, store_id=11, channel="shopee", ttl_seconds=60
        )
        == "busy"
    )

    # 网关不可用：fallback 开启回退本地；关闭则 503
    FakeClient.mode = "down"
    assert (
        gateway_relay.acquire_refresh_lock(
            tenant_id=2, store_id=11, channel="shopee", ttl_seconds=60
        )
        is None
    )
    monkeypatch.setenv("GATEWAY_REFRESH_LOCK_LEGACY_FALLBACK_ENABLED", "0")
    with pytest.raises(AppError):
        gateway_relay.acquire_refresh_lock(
            tenant_id=2, store_id=11, channel="shopee", ttl_seconds=60
        )


def test_shopee_ensure_access_token_gateway_busy_rechecks_credential(
    monkeypatch,
) -> None:
    expired = SimpleNamespace(usable=False)
    fresh = SimpleNamespace(usable=True)
    calls = {"count": 0}

    def get_credential(db, tenant_id, store_id):
        calls["count"] += 1
        return expired if calls["count"] == 1 else fresh

    store = SimpleNamespace(id=11, status=service.STORE_STATUS_CONNECTED, environment="production")
    monkeypatch.setattr(service, "get_store_or_raise", lambda db, tenant_id, store_id: store)
    monkeypatch.setattr(service.repo, "get_credential", get_credential)
    monkeypatch.setattr(service, "_decrypt_tokens", lambda credential: ("tok", "refresh"))
    monkeypatch.setattr(
        service, "_token_is_usable", lambda credential, token: bool(getattr(credential, "usable", False))
    )
    monkeypatch.setattr(
        gateway_relay,
        "acquire_refresh_lock",
        lambda **kwargs: "busy",
    )
    assert service.ensure_access_token(None, None, tenant_id=2, store_id=11) == "tok"

    # busy 且重新读取仍不可用：任务进行中（不重复刷新）
    calls["count"] = 0
    monkeypatch.setattr(service.repo, "get_credential", lambda db, tenant_id, store_id: expired)
    with pytest.raises(AppError) as exc_info:
        service.ensure_access_token(None, None, tenant_id=2, store_id=11)
    assert exc_info.value.code == ErrorCode.JOB_IN_PROGRESS
