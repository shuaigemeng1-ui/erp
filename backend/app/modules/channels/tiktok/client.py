"""TikTok Shop Open API 客户端（OAuth + HMAC-SHA256 签名）。

按旧 `ventasync` 项目 `extensions/tiktok/Services/TikTok/TikTokClient.php` 的
协议实现，供新 ERP 复用：

* 授权：``https://auth.tiktok-shops.com/oauth/authorize``
* 换取/刷新 Token：``/api/v2/token/get``、``/api/v2/token/refresh``
* Open API：``https://open-api.tiktokglobalshop.com``
* 签名：``HMAC-SHA256(app_secret + path + 排序后的 k/v + body + app_secret)``
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger("ventasync.channels.tiktok")

TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


class TikTokApiError(Exception):
    """TikTok Shop 接口错误；``transient`` 时调用方可有限重试。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        status_code: int | None = None,
        transient: bool = False,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.transient = transient
        self.payload = dict(payload or {})

    def __str__(self) -> str:  # pragma: no cover - 仅日志
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.status_code is not None:
            parts.append(f"http_status={self.status_code}")
        return " ".join(parts)


def _to_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _expiry_seconds(value: Any, *, now_ts: int | None = None) -> int:
    """TikTok 的 expire 可能是绝对时间戳，也可能是剩余秒数。"""
    seconds = _to_int(value)
    if seconds <= 0:
        return 0
    current = int(now_ts if now_ts is not None else time.time())
    if seconds > 1_000_000_000:
        return max(0, seconds - current)
    return seconds


def normalize_token_payload(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 token 响应归一化为内部结构（兼容 ``{data: ...}`` 包装）。"""
    payload: Mapping[str, Any] = raw or {}
    data = payload.get("data")
    if isinstance(data, Mapping):
        payload = data
    return {
        "access_token": str(payload.get("access_token") or "").strip(),
        "refresh_token": str(payload.get("refresh_token") or "").strip(),
        "access_expires_in": _expiry_seconds(payload.get("access_token_expire_in")),
        "refresh_expires_in": _expiry_seconds(payload.get("refresh_token_expire_in")),
        "shop_id": str(payload.get("shop_id") or "").strip(),
        "shop_cipher": str(payload.get("shop_cipher") or "").strip(),
        "seller_name": str(payload.get("seller_name") or "").strip(),
        "region": str(payload.get("seller_base_region") or payload.get("region") or "").strip(),
    }


def token_expiry_times(
    token: Mapping[str, Any], now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    access_seconds = _to_int(token.get("access_expires_in"))
    refresh_seconds = _to_int(token.get("refresh_expires_in"))
    access = current + timedelta(seconds=access_seconds) if access_seconds else None
    refresh = current + timedelta(seconds=refresh_seconds) if refresh_seconds else None
    return access, refresh


class TikTokClient:
    """TikTok Shop Open API 客户端（单店铺上下文）。"""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        access_token: str = "",
        shop_cipher: str = "",
        auth_base: str | None = None,
        api_base: str | None = None,
        timeout: float | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not app_key:
            raise ValueError("app_key 不能为空")
        if not app_secret:
            raise ValueError("app_secret 不能为空")
        self.app_key = app_key
        self.app_secret = app_secret
        self.access_token = access_token or ""
        self.shop_cipher = shop_cipher or ""
        self.auth_base = (auth_base or settings.tiktok_auth_base).rstrip("/")
        self.api_base = (api_base or settings.tiktok_api_base).rstrip("/")
        self.timeout = float(
            timeout if timeout is not None else settings.tiktok_http_timeout_seconds
        )
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(self.timeout), follow_redirects=False
        )
        self._owns_client = http_client is None

    # ------------------------------------------------------------------ OAuth
    def auth_url(self, state: str) -> str:
        """生成授权地址（与旧实现一致：app_key + state）。"""
        query = httpx.QueryParams({"app_key": self.app_key, "state": state})
        return f"{self.auth_base}/oauth/authorize?{query}"

    def get_token(self, auth_code: str) -> dict[str, Any]:
        return self._token_request(
            "/api/v2/token/get",
            {
                "app_key": self.app_key,
                "app_secret": self.app_secret,
                "auth_code": auth_code,
                "grant_type": "authorized_code",
            },
        )

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        return self._token_request(
            "/api/v2/token/refresh",
            {
                "app_key": self.app_key,
                "app_secret": self.app_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

    def _token_request(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.get(f"{self.auth_base}{path}", params=dict(params))
        except httpx.HTTPError as exc:
            raise TikTokApiError("TikTok 授权服务连接失败", transient=True) from exc
        payload = self._parse_response(response, path)
        return payload

    # ------------------------------------------------------------------ 签名
    def generate_sign(self, path: str, params: Mapping[str, Any], body: str | None) -> str:
        filtered = {
            str(key): value
            for key, value in params.items()
            if key not in {"sign", "access_token"} and value is not None
        }
        sign_string = self.app_secret + path
        for key in sorted(filtered):
            sign_string += str(key) + str(filtered[key])
        if body:
            sign_string += body
        sign_string += self.app_secret
        return hmac.new(
            self.app_secret.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # ------------------------------------------------------------------ Open API
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        shop_cipher: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {
            **{str(key): value for key, value in (params or {}).items()},
            "app_key": self.app_key,
            "timestamp": int(time.time()),
        }
        cipher = shop_cipher if shop_cipher is not None else self.shop_cipher
        if cipher:
            query["shop_cipher"] = cipher
        body_text: str | None = None
        if body is not None:
            body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        query["sign"] = self.generate_sign(path, query, body_text)

        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["x-tts-access-token"] = self.access_token
        url = f"{self.api_base}{path}"
        try:
            response = self._client.request(
                method.upper(),
                url,
                params=query,
                headers=headers,
                content=body_text,
            )
        except httpx.HTTPError as exc:
            raise TikTokApiError("TikTok API 连接失败", transient=True) from exc
        return self._parse_response(response, path)

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.request("GET", path, params=params, **kwargs)

    def post(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return self.request("POST", path, params=params, body=body or {}, **kwargs)

    def get_shops(self) -> list[dict[str, Any]]:
        """读取授权店铺（需要 access_token；不含 shop_cipher）。"""
        payload = self.get("/authorization/202309/shops")
        data = payload.get("data") if isinstance(payload, Mapping) else None
        shops = data.get("shops") if isinstance(data, Mapping) else None
        if not isinstance(shops, list):
            return []
        return [dict(shop) for shop in shops if isinstance(shop, Mapping)]

    # ------------------------------------------------------------------ HTTP
    def _parse_response(self, response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:500]}
        if not isinstance(payload, dict):
            payload = {"data": payload}
        code = str(payload.get("code") or "0")
        message = str(payload.get("message") or payload.get("msg") or "")
        ok = response.status_code < 400 and code in {"0", "200", ""}
        if not ok:
            transient = response.status_code in TRANSIENT_STATUS or response.status_code >= 500
            logger.warning(
                "TikTok API 失败 path=%s http=%s code=%s message=%s",
                path,
                response.status_code,
                code,
                message[:200],
            )
            raise TikTokApiError(
                message or "TikTok API 返回错误",
                code=code,
                status_code=response.status_code,
                transient=transient,
                payload=payload,
            )
        return payload

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TikTokClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = [
    "TikTokApiError",
    "TikTokClient",
    "normalize_token_payload",
    "token_expiry_times",
]
