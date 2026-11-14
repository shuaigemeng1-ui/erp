"""TikTok Shop webhook ingress helpers for the independent gateway.

The gateway is the only public receiver for an app.  It verifies the raw-body
signature, resolves ``shop_id`` to an instance, stores a durable inbox row and
lets the background dispatcher deliver verified events to the owning tenant.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

EVENT_TYPE_NAMES: dict[int, str] = {
    1: "ORDER_STATUS_CHANGE",
    2: "REVERSE_STATUS_UPDATE",
    3: "RECIPIENT_ADDRESS_UPDATE",
    4: "PACKAGE_UPDATE",
    6: "SELLER_DEAUTHORIZATION",
    7: "UPCOMING_AUTHORIZATION_EXPIRATION",
    11: "CANCELLATION_STATUS_CHANGE",
    12: "RETURN_STATUS_CHANGE",
    39: "ACTIVITY_STATUS_CHANGE",
    63: "ACTIVITY_CHANGE",
    64: "AFTERSALES_REQUEST_STATUS_UPDATE",
    65: "RMA_STATUS_UPDATE",
    67: "REFUND_SUCCESS",
}

WEBHOOK_PATH = "/api/v1/gateway/webhooks/tiktok"
TENANT_RELAY_PATH = "/api/v1/integrations/tiktok/webhook/relay"


@dataclass(frozen=True)
class WebhookNotification:
    notification_id: str
    event_type_code: int
    event_type_name: str
    platform_shop_id: str
    payload: dict[str, Any]


class WebhookPayloadError(ValueError):
    """The verified body is not a usable TikTok notification."""


def tiktok_signature(*, app_key: str, app_secret: str, raw_body: bytes) -> str:
    message = app_key.encode("utf-8") + raw_body
    return hmac.new(app_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_tiktok_signature(
    *,
    app_key: str,
    app_secret: str,
    raw_body: bytes,
    received_signature: str,
) -> bool:
    if not app_key or not app_secret or not received_signature:
        return False
    expected = tiktok_signature(app_key=app_key, app_secret=app_secret, raw_body=raw_body)
    return hmac.compare_digest(expected, received_signature.strip())


def parse_notification(raw_body: bytes) -> WebhookNotification:
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookPayloadError("body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise WebhookPayloadError("body must be a JSON object")
    notification_id = str(
        payload.get("tts_notification_id") or payload.get("notification_id") or ""
    ).strip()
    if not notification_id:
        raise WebhookPayloadError("notification id is missing")
    try:
        event_type_code = int(payload.get("type") or 0)
    except (TypeError, ValueError):
        event_type_code = 0
    platform_shop_id = str(payload.get("shop_id") or "").strip()
    if not platform_shop_id:
        raise WebhookPayloadError("shop_id is missing")
    return WebhookNotification(
        notification_id=notification_id[:128],
        event_type_code=event_type_code,
        event_type_name=EVENT_TYPE_NAMES.get(event_type_code, f"UNKNOWN_{event_type_code}"),
        platform_shop_id=platform_shop_id[:64],
        payload=payload,
    )


def relay_signature(*, relay_secret: str, timestamp: str, raw_body: bytes) -> str:
    message = timestamp.encode("utf-8") + b"." + raw_body
    return hmac.new(relay_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_relay_signature(
    *,
    relay_secret: str,
    timestamp: str,
    raw_body: bytes,
    received_signature: str,
) -> bool:
    if not relay_secret or not timestamp or not received_signature:
        return False
    expected = relay_signature(
        relay_secret=relay_secret, timestamp=timestamp, raw_body=raw_body
    )
    return hmac.compare_digest(expected, received_signature.strip())


def tenant_relay_url(callback_url: str) -> str:
    """Derive the tenant relay endpoint from the instance's OAuth callback URL."""
    parsed = urlsplit((callback_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{TENANT_RELAY_PATH}"


def webhook_idempotency_key(*, channel: str, notification_id: str) -> str:
    """租户侧幂等键：同一通知重放时保持不变（original notification ID）。"""
    return f"{(channel or 'tiktok').strip().lower()}:{str(notification_id).strip()}"
