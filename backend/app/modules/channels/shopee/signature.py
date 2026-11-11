"""Shopee Open Platform v2 签名算法（纯函数，无网络 / 无数据库）。

签名规则（参考 ventasync/extensions/shopee 与 Shopee 官方文档）：

* 授权与公共接口：``base_string = partner_id + path + timestamp``
* 店铺接口：``base_string = partner_id + path + timestamp + access_token + shop_id``
* 刷新令牌：``base_string = partner_id + path + timestamp + refresh_token``
* 推送验签：``HMAC-SHA256(secret, callback_url + "|" + raw_body)``（十六进制小写）

所有签名使用 HMAC-SHA256 输出十六进制小写字符串；时间戳为秒级 Unix 时间。
本模块只放纯逻辑，便于单元测试（见 ``tests/unit/test_shopee.py``）。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def hmac_sha256_hex(secret: str, message: str) -> str:
    """HMAC-SHA256 十六进制小写摘要。"""
    if not secret:
        raise ValueError("签名密钥不能为空")
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- 请求签名
def build_auth_base_string(partner_id: int | str, path: str, timestamp: int) -> str:
    """授权 / 公共接口签名的 base string。"""
    return f"{int(partner_id)}{path}{int(timestamp)}"


def sign_auth(partner_id: int | str, partner_key: str, path: str, timestamp: int) -> str:
    """授权 / 公共接口签名。"""
    return hmac_sha256_hex(partner_key, build_auth_base_string(partner_id, path, timestamp))


def build_shop_base_string(
    partner_id: int | str,
    path: str,
    timestamp: int,
    access_token: str,
    shop_id: int | str,
) -> str:
    """店铺接口签名的 base string。"""
    return f"{int(partner_id)}{path}{int(timestamp)}{access_token}{int(shop_id)}"


def sign_shop(
    partner_id: int | str,
    partner_key: str,
    path: str,
    timestamp: int,
    access_token: str,
    shop_id: int | str,
) -> str:
    """店铺接口签名。"""
    return hmac_sha256_hex(
        partner_key,
        build_shop_base_string(partner_id, path, timestamp, access_token, shop_id),
    )


def build_refresh_base_string(
    partner_id: int | str, path: str, timestamp: int, refresh_token: str
) -> str:
    """刷新令牌签名的 base string。"""
    return f"{int(partner_id)}{path}{int(timestamp)}{refresh_token}"


def sign_refresh(
    partner_id: int | str,
    partner_key: str,
    path: str,
    timestamp: int,
    refresh_token: str,
) -> str:
    """刷新令牌签名。"""
    return hmac_sha256_hex(
        partner_key, build_refresh_base_string(partner_id, path, timestamp, refresh_token)
    )


# --------------------------------------------------------------------------- 推送验签
def webhook_signature(secret: str, callback_url: str, raw_body: bytes | str) -> str:
    """Shopee 推送签名：``HMAC-SHA256(secret, url + "|" + body)``。"""
    if isinstance(raw_body, str):
        body_bytes = raw_body.encode("utf-8")
    else:
        body_bytes = raw_body
    message = callback_url.encode("utf-8") + b"|" + body_bytes
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    secret: str,
    callback_urls: Iterable[str],
    raw_body: bytes | str,
    received_signature: str,
) -> bool:
    """常量时间校验推送签名；支持同一回调地址的 http/https 等多种写法。"""
    if not secret or not received_signature:
        return False
    candidate = received_signature.strip().lower()
    for url in callback_urls:
        if not url:
            continue
        expected = webhook_signature(secret, url, raw_body)
        if hmac.compare_digest(expected, candidate):
            return True
    return False


# --------------------------------------------------------------------------- 授权链接
def build_authorize_url(
    *,
    auth_base: str,
    partner_id: int | str,
    partner_key: str,
    redirect_url: str,
    timestamp: int | None = None,
) -> str:
    """拼接店铺授权地址（``/api/v2/shop/auth_partner``）。

    ``redirect_url`` 由调用方带上一次性 ``state``（Shopee 回调会原样带回，
    并附加 ``code`` / ``shop_id``）。
    """
    if not partner_key:
        raise ValueError("partner_key 不能为空")
    ts = int(timestamp if timestamp is not None else time.time())
    path = "/api/v2/shop/auth_partner"
    sign = sign_auth(partner_id, partner_key, path, ts)
    query = urlencode(
        {
            "partner_id": int(partner_id),
            "timestamp": ts,
            "sign": sign,
            "redirect": redirect_url,
        }
    )
    return f"{auth_base.rstrip('/')}{path}?{query}"


def append_query_params(url: str, params: dict[str, object]) -> str:
    """向 URL 追加查询参数（保留原有参数，重复键以新值为准）。"""
    parts = urlsplit(url)
    existing = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    merged = [(key, value) for key, value in existing if key not in params]
    for key, value in params.items():
        if value is None:
            continue
        merged.append((key, str(value)))
    query = urlencode(merged)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def safe_redirect_path(path: str | None, fallback: str = "/") -> str:
    """校验站内跳转路径，禁止开放重定向（不得含协议 / 主机 / 协议相对地址）。"""
    candidate = (path or "").strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    if "://" in candidate or "\r" in candidate or "\n" in candidate or "\\" in candidate:
        return fallback
    return candidate


def is_expired(expires_at, now=None, skew_seconds: int = 0) -> bool:
    """判断令牌是否已过期（可提前 ``skew_seconds`` 视为过期，便于刷新）。"""
    if expires_at is None:
        return True
    from datetime import datetime, timedelta, timezone

    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    return expires_at <= now + timedelta(seconds=max(0, int(skew_seconds)))
