"""授权 state 的签名与解析（TikTok / Shopee 共用，无数据库依赖）。

state 自包含授权上下文，回调不依赖 Redis；签名使用应用 SECRET_KEY，
防止用户篡改 state 把授权结果写到其他店铺。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def build_signed_state(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = _b64url_encode(body)
    signature = hmac.new(
        settings.secret_key_bytes, encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64url_encode(signature)}"


def parse_signed_state(state: str | None) -> dict[str, Any]:
    token = (state or "").strip()
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as exc:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权 state 格式无效") from exc
    expected = hmac.new(
        settings.secret_key_bytes, encoded.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(signature)
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权 state 签名无效") from exc
    if not hmac.compare_digest(expected, provided):
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权 state 签名无效")
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权 state 内容无效") from exc
    if not isinstance(payload, dict):
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权 state 内容无效")
    return payload


__all__ = ["build_signed_state", "parse_signed_state"]
