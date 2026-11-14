"""Tenant-side helpers for verified TikTok webhook events relayed by gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

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

ORDER_EVENT_NAMES = frozenset(
    {
        "ORDER_STATUS_CHANGE",
        "REVERSE_STATUS_UPDATE",
        "RECIPIENT_ADDRESS_UPDATE",
        "PACKAGE_UPDATE",
        "CANCELLATION_STATUS_CHANGE",
        "RETURN_STATUS_CHANGE",
    }
)


def verify_relay_signature(
    *,
    relay_secret: str,
    timestamp: str,
    raw_body: bytes,
    received_signature: str,
) -> bool:
    if not relay_secret or not timestamp or not received_signature:
        return False
    message = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(
        relay_secret.encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received_signature.strip())


def parse_event(raw_body: bytes) -> tuple[str, int, str, str, dict[str, Any]]:
    """Return ``(notification_id, type_code, type_name, shop_id, payload)``."""
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    notification_id = str(
        payload.get("tts_notification_id") or payload.get("notification_id") or ""
    ).strip()
    if not notification_id:
        raise ValueError("notification id is missing")
    try:
        type_code = int(payload.get("type") or 0)
    except (TypeError, ValueError):
        type_code = 0
    shop_id = str(payload.get("shop_id") or "").strip()
    if not shop_id:
        raise ValueError("shop_id is missing")
    return (
        notification_id[:128],
        type_code,
        EVENT_TYPE_NAMES.get(type_code, f"UNKNOWN_{type_code}"),
        shop_id[:64],
        payload,
    )


def extract_order_ids(data: Any) -> list[str]:
    values: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()[:64]
        if text and text not in values:
            values.append(text)

    if not isinstance(data, dict):
        return values
    for key in ("order_id", "order_ids", "main_order_id", "main_order_ids"):
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
        else:
            add(value)
    for key in ("orders", "packages", "line_items"):
        rows = data.get(key)
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    for id_key in ("order_id", "main_order_id"):
                        add(row.get(id_key))
    return values[:100]


def sanitized_payload(
    *, notification_id: str, type_code: int, type_name: str, shop_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Only keep routing metadata in the tenant inbox; full PII stays in raw order data."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {
        "notification_id": notification_id,
        "type": type_code,
        "type_name": type_name,
        "shop_id": shop_id,
        "timestamp": payload.get("timestamp"),
        "update_time": data.get("update_time") or data.get("updateTime"),
        "order_ids": extract_order_ids(data),
    }
