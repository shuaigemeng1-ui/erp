"""邮件厂商适配：阿里云 DirectMail、腾讯云 SES。

只依赖 httpx 与标准库签名，避免引入云厂商 SDK 的大依赖。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

ALIYUN_DIRECTMAIL_ENDPOINT = "https://dm.aliyuncs.com/"
TENCENT_SES_ENDPOINT = "https://ses.tencentcloudapi.com/"
TENCENT_SES_VERSION = "2020-10-02"


class EmailProviderError(Exception):
    """厂商接口错误；``message`` 可直接展示给管理员。"""

    def __init__(self, message: str, *, code: str = "EmailSendFailed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class EmailConfig:
    provider: str
    region: str
    from_address: str
    from_name: str
    access_key_id: str
    secret_access_key: str


@dataclass(frozen=True)
class EmailMessage:
    to: list[str]
    subject: str
    text: str = ""
    html: str = ""


@dataclass(frozen=True)
class EmailSendResult:
    ok: bool
    message: str
    provider: str
    request_id: str = ""


def _safe_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        for key in ("Message", "message", "Msg", "msg"):
            value = payload.get(key)
            if value:
                return str(value)[:300]
        error = payload.get("Error")
        if isinstance(error, dict) and error.get("Message"):
            return str(error["Message"])[:300]
    return fallback


def _aliyun_encode(value: Any) -> str:
    return quote(str(value), safe="~")


def _aliyun_signature(params: dict[str, Any], secret: str) -> str:
    canonical = "&".join(
        f"{_aliyun_encode(key)}={_aliyun_encode(params[key])}" for key in sorted(params)
    )
    string_to_sign = f"GET&%2F&{_aliyun_encode(canonical)}"
    digest = hmac.new(
        f"{secret}&".encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _send_aliyun(config: EmailConfig, message: EmailMessage) -> EmailSendResult:
    params: dict[str, Any] = {
        "AccessKeyId": config.access_key_id,
        "Action": "SingleSendMail",
        "Format": "JSON",
        "RegionId": config.region or "cn-hangzhou",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureNonce": uuid.uuid4().hex,
        "SignatureVersion": "1.0",
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Version": "2015-11-23",
        "AccountName": config.from_address,
        "AddressType": "1",
        "ReplyToAddress": "false",
        "ToAddress": ",".join(message.to),
        "Subject": message.subject,
    }
    if config.from_name:
        params["FromAlias"] = config.from_name
    if message.html:
        params["HtmlBody"] = message.html
    else:
        params["TextBody"] = message.text or message.subject
    params["Signature"] = _aliyun_signature(params, config.secret_access_key)
    try:
        response = httpx.get(
            ALIYUN_DIRECTMAIL_ENDPOINT, params=params, timeout=30.0
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EmailProviderError(
            "阿里云 DirectMail 请求失败，请检查 Endpoint、网络和 AccessKey",
            code="AliyunRequestFailed",
        ) from exc
    if response.status_code >= 400 or (
        isinstance(payload, dict) and payload.get("Code") not in (None, "OK")
    ):
        raise EmailProviderError(
            _safe_message(payload, f"阿里云 DirectMail 返回 HTTP {response.status_code}"),
            code="AliyunSendFailed",
        )
    request_id = str(payload.get("RequestId") or payload.get("RequestID") or "") if isinstance(payload, dict) else ""
    return EmailSendResult(True, "阿里云 DirectMail 发送请求已提交", "aliyun_directmail", request_id)


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _send_tencent(config: EmailConfig, message: EmailMessage) -> EmailSendResult:
    body = {
        "FromEmailAddress": config.from_address,
        "Destination": list(message.to),
        "Subject": message.subject,
        "Simple": ({"Html": message.html} if message.html else {"Text": message.text or message.subject}),
    }
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    host = "ses.tencentcloudapi.com"
    content_type = "application/json; charset=utf-8"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        "x-tc-action:sendemail\n"
        f"x-tc-timestamp:{timestamp}\n"
        f"x-tc-version:{TENCENT_SES_VERSION}\n"
    )
    signed_headers = "content-type;host;x-tc-action;x-tc-timestamp;x-tc-version"
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{_sha256_hex(body_text.encode('utf-8'))}"
    )
    credential_scope = f"{date}/ses/tc3_request"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n"
        f"{_sha256_hex(canonical_request.encode('utf-8'))}"
    )
    secret_date = _hmac_sha256(f"TC3{config.secret_access_key}".encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, "ses")
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    authorization = (
        f"TC3-HMAC-SHA256 Credential={config.access_key_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": "SendEmail",
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": TENCENT_SES_VERSION,
        "X-TC-Region": config.region or "ap-guangzhou",
    }
    try:
        response = httpx.post(
            TENCENT_SES_ENDPOINT, headers=headers, content=body_text, timeout=30.0
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EmailProviderError(
            "腾讯云 SES 请求失败，请检查区域、网络和 SecretId/SecretKey",
            code="TencentRequestFailed",
        ) from exc
    result = payload.get("Response") if isinstance(payload, dict) else None
    if response.status_code >= 400 or not isinstance(result, dict):
        raise EmailProviderError(
            _safe_message(payload, f"腾讯云 SES 返回 HTTP {response.status_code}"),
            code="TencentSendFailed",
        )
    if result.get("Error"):
        raise EmailProviderError(
            _safe_message(result, "腾讯云 SES 发送失败"), code="TencentSendFailed"
        )
    request_id = str(result.get("RequestId") or "")
    message_id = str(result.get("MessageId") or "")
    return EmailSendResult(
        True,
        f"腾讯云 SES 发送请求已提交{f'（MessageId: {message_id}）' if message_id else ''}",
        "tencent_ses",
        request_id,
    )


SENDERS = {
    "aliyun_directmail": _send_aliyun,
    "tencent_ses": _send_tencent,
}


def send_email(config: EmailConfig, message: EmailMessage) -> EmailSendResult:
    sender = SENDERS.get(config.provider)
    if sender is None:
        raise EmailProviderError(f"不支持的邮件厂商：{config.provider}", code="InvalidProvider")
    if not message.to:
        raise EmailProviderError("收件人不能为空", code="InvalidRecipient")
    return sender(config, message)


__all__ = [
    "EmailConfig",
    "EmailMessage",
    "EmailProviderError",
    "EmailSendResult",
    "send_email",
]
