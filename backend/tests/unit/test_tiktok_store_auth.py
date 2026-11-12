"""TikTok Shop 先建店铺后授权：Service ID 授权链接测试。"""

from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.modules.channels.tiktok.oauth import build_authorization_url


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
