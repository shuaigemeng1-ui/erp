"""TikTok Shop 纯函数与签名单元测试（不联网、不连数据库）。"""

from __future__ import annotations

import hashlib
import hmac
import time

from app.modules.channels.tiktok import oauth
from app.modules.channels.tiktok.client import (
    TikTokClient,
    normalize_token_payload,
    token_expiry_times,
)


def _client() -> TikTokClient:
    return TikTokClient(
        app_key="test_app_key",
        app_secret="test_app_secret",
        access_token="test_access_token",
        shop_cipher="test_shop_cipher",
    )


def test_tiktok_auth_url_contains_app_key_and_state() -> None:
    client = _client()
    try:
        url = client.auth_url("t123.abc")
    finally:
        client.close()
    assert url.startswith("https://auth.tiktok-shops.com/oauth/authorize?")
    assert "app_key=test_app_key" in url
    assert "state=t123.abc" in url


def test_tiktok_signature_matches_manual_hmac() -> None:
    client = _client()
    try:
        path = "/authorization/202309/shops"
        params = {"app_key": "test_app_key", "timestamp": 1700000000}
        body = '{"a":1}'
        sign = client.generate_sign(path, params, body)
    finally:
        client.close()
    sign_string = (
        "test_app_secret"
        + path
        + "app_keytest_app_key"
        + "timestamp1700000000"
        + body
        + "test_app_secret"
    )
    expected = hmac.new(
        b"test_app_secret", sign_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    assert sign == expected


def test_tiktok_normalize_token_payload_supports_wrapped_and_absolute_expiry() -> None:
    expire_at = int(time.time()) + 3600
    payload = normalize_token_payload(
        {
            "code": 0,
            "data": {
                "access_token": " access ",
                "refresh_token": "refresh",
                "access_token_expire_in": expire_at,
                "refresh_token_expire_in": 7200,
                "seller_name": "Shop",
                "seller_base_region": "US",
            },
        }
    )
    assert payload["access_token"] == "access"
    assert payload["refresh_token"] == "refresh"
    assert 3500 <= payload["access_expires_in"] <= 3600
    assert payload["refresh_expires_in"] == 7200
    assert payload["seller_name"] == "Shop"
    assert payload["region"] == "US"


def test_tiktok_token_expiry_times() -> None:
    access, refresh = token_expiry_times(
        {"access_expires_in": 60, "refresh_expires_in": 120}
    )
    assert access is not None and refresh is not None
    assert (refresh - access).total_seconds() == 60


def test_tiktok_oauth_state_roundtrip() -> None:
    state = oauth.build_state(7)
    tenant_id, nonce = oauth.parse_state(state)
    assert tenant_id == 7
    assert nonce
    assert nonce in oauth.authorization_session_key(7, nonce)
