"""邮件服务 provider 适配测试（不访问真实云厂商）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.modules.email import providers
from app.modules.email.providers import (
    EmailConfig,
    EmailMessage,
    EmailProviderError,
    send_email,
)


def _config(provider: str) -> EmailConfig:
    return EmailConfig(
        provider=provider,
        region="cn-hangzhou" if provider == "aliyun_directmail" else "ap-guangzhou",
        from_address="noreply@example.com",
        from_name="ERP",
        access_key_id="test-key-id",
        secret_access_key="test-secret",
    )


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def test_aliyun_directmail_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get(url: str, *, params: dict[str, Any], timeout: float) -> _FakeResponse:
        captured.update(url=url, params=params, timeout=timeout)
        return _FakeResponse(200, {"RequestId": "req-aliyun"})

    monkeypatch.setattr(providers.httpx, "get", fake_get)
    result = send_email(
        _config("aliyun_directmail"),
        EmailMessage(to=["to@example.com"], subject="测试", html="<p>hi</p>"),
    )
    assert result.ok
    assert result.request_id == "req-aliyun"
    assert captured["url"] == "https://dm.aliyuncs.com/"
    assert captured["params"]["Action"] == "SingleSendMail"
    assert captured["params"]["ToAddress"] == "to@example.com"
    assert captured["params"]["Signature"]
    assert captured["params"]["RegionId"] == "cn-hangzhou"


def test_tencent_ses_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, *, headers: dict[str, str], content: str, timeout: float) -> _FakeResponse:
        captured.update(url=url, headers=headers, content=content, timeout=timeout)
        return _FakeResponse(200, {"Response": {"RequestId": "req-tencent", "MessageId": "msg-1"}})

    monkeypatch.setattr(providers.httpx, "post", fake_post)
    result = send_email(
        _config("tencent_ses"),
        EmailMessage(to=["to@example.com"], subject="测试", text="hi"),
    )
    assert result.ok
    assert result.request_id == "req-tencent"
    assert captured["url"] == "https://ses.tencentcloudapi.com/"
    assert captured["headers"]["X-TC-Action"] == "SendEmail"
    assert captured["headers"]["X-TC-Region"] == "ap-guangzhou"
    assert captured["headers"]["Authorization"].startswith("TC3-HMAC-SHA256 ")
    payload = json.loads(captured["content"])
    assert payload["Destination"] == ["to@example.com"]
    assert payload["Simple"] == {"Text": "hi"}


def test_provider_error_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, *, params: dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse(400, {"Code": "InvalidAccessKeyId.NotFound", "Message": "bad key"})

    monkeypatch.setattr(providers.httpx, "get", fake_get)
    with pytest.raises(EmailProviderError) as exc:
        send_email(_config("aliyun_directmail"), EmailMessage(to=["to@example.com"], subject="x"))
    assert exc.value.code == "AliyunSendFailed"
    assert "bad key" in exc.value.message
