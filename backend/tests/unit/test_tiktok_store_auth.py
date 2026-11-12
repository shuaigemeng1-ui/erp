"""TikTok Shop 先建店铺后授权：Service ID 授权链接测试。"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.core.security import utc_after, utc_now
from app.modules.channels.tiktok.oauth import (
    build_authorization_url,
    build_signed_state,
    consume_authorization_session,
    parse_signed_state,
)


def test_build_authorization_url_row() -> None:
    url = build_authorization_url(
        service_id="7675808952487053064", state="t1.nonce", market="ROW"
    )
    assert url.startswith("https://services.tiktokshop.com/open/authorize?")
    assert "service_id=7675808952487053064" in url
    assert "state=t1.nonce" in url


def test_build_authorization_url_us() -> None:
    url = build_authorization_url(
        service_id="7675808952487053064", state="abc", market="US"
    )
    assert url.startswith("https://services.us.tiktokshop.com/open/authorize?")
    assert "service_id=7675808952487053064" in url


def test_build_authorization_url_requires_service_id() -> None:
    with pytest.raises(AppError):
        build_authorization_url(service_id="", state="abc")


def _state_payload(*, store_id: int, store_code: str) -> dict:
    return {
        "tenant_id": 1,
        "member_id": 2,
        "environment": "production",
        "redirect_path": "/tenants/1/stores",
        "store_id": store_id,
        "store_code": store_code,
        "service_id": "7675808952487053064",
        "market": "ROW",
        "created_at": utc_now().isoformat(),
        "expires_at": utc_after(600).isoformat(),
        "nonce": f"nonce-{store_id}",
    }


def test_signed_state_binds_multiple_pending_stores() -> None:
    first = build_signed_state(_state_payload(store_id=11, store_code="TKAAAAAAAA"))
    second = build_signed_state(_state_payload(store_id=12, store_code="TKBBBBBBBB"))
    assert first != second
    assert parse_signed_state(first)["store_id"] == 11
    assert parse_signed_state(second)["store_id"] == 12
    callback_first = consume_authorization_session(first)
    callback_second = consume_authorization_session(second)
    assert callback_first["store_id"] == 11
    assert callback_second["store_id"] == 12
    assert callback_first["store_code"] == "TKAAAAAAAA"


def test_signed_state_rejects_tampered_payload() -> None:
    state = build_signed_state(_state_payload(store_id=11, store_code="TKAAAAAAAA"))
    payload_part, signature = state.split(".", 1)
    tampered = payload_part[:-1] + ("A" if payload_part[-1] != "A" else "B")
    with pytest.raises(AppError):
        parse_signed_state(f"{tampered}.{signature}")
