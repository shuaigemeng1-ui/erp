"""Shopee Open Platform v2 HTTP 客户端（HTTPX，同步）。

可靠性约定（plan.txt §4「库存与任务可靠性」）：

* 超时与连接错误、HTTP 429/5xx、平台 ``error_server`` 类错误才做有限重试；
  参数错误、签名错误、凭据失效等 4xx 业务错误不重试，直接进入可处理失败状态。
* 每个请求重新计算时间戳与签名，签名规则见 ``shopee/signature.py``。
* Token 刷新使用 refresh token 单独签名；刷新失败的判定由 Service 处理
  （标记 ``stores.status=reauth_required`` 并暂停依赖任务）。
* 客户端不接触数据库：调用方传入已解密凭据，避免明文落日志。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.config import settings
from app.core.security import utc_now
from app.modules.channels.shopee.signature import (
    hmac_sha256_hex,
    sign_auth,
    sign_refresh,
    sign_shop,
)

logger = logging.getLogger("ventasync.shopee")

# --------------------------------------------------------------------------- 接口路径
AUTH_PARTNER_PATH = "/api/v2/shop/auth_partner"
AUTH_TOKEN_GET_PATH = "/api/v2/auth/token/get"
AUTH_ACCESS_TOKEN_GET_PATH = "/api/v2/auth/access_token/get"
SHOP_INFO_PATH = "/api/v2/shop/get_shop_info"
ITEM_LIST_PATH = "/api/v2/product/get_item_list"
ITEM_BASE_INFO_PATH = "/api/v2/product/get_item_base_info"
MODEL_LIST_PATH = "/api/v2/product/get_model_list"
UPDATE_STOCK_PATH = "/api/v2/product/update_stock"
ORDER_LIST_PATH = "/api/v2/order/get_order_list"
ORDER_DETAIL_PATH = "/api/v2/order/get_order_detail"

SANDBOX_BASE_URL = "https://openplatform.sandbox.test-stable.shopee.sg"
PRODUCTION_BASE_URL = "https://partner.shopeemobile.com"

# 仅这些 HTTP 状态视为暂时性错误，可有限重试
TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
# 平台侧返回的暂时性错误码
TRANSIENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "error_server",
        "error_system_busy",
        "error_rate_limit",
        "error_too_many_request",
        "error_try_again",
        "error_timeout",
    }
)
# 凭据失效类错误：由 Service 标记需要重新授权，不重试
AUTH_INVALID_ERROR_CODES: frozenset[str] = frozenset(
    {"error_auth", "error_token", "error_invalid_access_token", "error_invalid_refresh_token"}
)

ITEM_BATCH_SIZE = 50
ORDER_BATCH_SIZE = 50


def resolve_base_url(environment: str) -> str:
    """按环境返回平台地址：production 用生产域名，其余用沙箱。"""
    if (environment or "").lower() == "production":
        return (settings.shopee_api_base or PRODUCTION_BASE_URL).rstrip("/")
    return SANDBOX_BASE_URL


def is_transient_status(status_code: int) -> bool:
    """HTTP 状态是否可重试。"""
    return int(status_code) in TRANSIENT_STATUS_CODES


def is_transient_error_code(error_code: str) -> bool:
    """平台错误码是否可重试。"""
    return (error_code or "").lower() in TRANSIENT_ERROR_CODES


def is_auth_invalid_error_code(error_code: str) -> bool:
    """平台错误码是否表示授权失效（需要重新授权）。"""
    return (error_code or "").lower() in AUTH_INVALID_ERROR_CODES


def retry_delay_seconds(attempt: int, base: float = 0.5, cap: float = 5.0) -> float:
    """指数退避：0.5s、1s、2s、4s… 最大 5s。"""
    return min(cap, base * (2 ** max(0, int(attempt))))


def token_expiry(now: datetime, expire_in: int | float | str | None) -> datetime | None:
    """把 ``expire_in``（秒）换成 UTC 过期时间；非法值返回 ``None``。"""
    try:
        seconds = int(float(expire_in))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return now + timedelta(seconds=seconds)


class ShopeeApiError(Exception):
    """Shopee 接口错误。``transient=True`` 时调用方可以有限重试。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        status_code: int | None = None,
        transient: bool = False,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.transient = transient
        self.payload = dict(payload or {})

    def __str__(self) -> str:  # pragma: no cover - 仅日志展示
        parts = [self.message]
        if self.error_code:
            parts.append(f"error_code={self.error_code}")
        if self.status_code is not None:
            parts.append(f"http_status={self.status_code}")
        return " ".join(parts)


class ShopeeClient:
    """Shopee v2 同步客户端。

    用法::

        client = ShopeeClient(partner_id=..., partner_key=..., access_token=..., shop_id=...)
        data = client.shop_get(ITEM_LIST_PATH, {"offset": 0})
    """

    def __init__(
        self,
        *,
        partner_id: int,
        partner_key: str,
        access_token: str = "",
        shop_id: int | str = 0,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        http_client: httpx.Client | None = None,
        sleep_func=time.sleep,
    ) -> None:
        if not partner_id:
            raise ValueError("partner_id 不能为空")
        if not partner_key:
            raise ValueError("partner_key 不能为空")
        self.partner_id = int(partner_id)
        self.partner_key = partner_key
        self.access_token = access_token or ""
        self.shop_id = int(shop_id or 0)
        self.base_url = (base_url or PRODUCTION_BASE_URL).rstrip("/")
        self.timeout = float(
            timeout if timeout is not None else settings.shopee_http_timeout_seconds
        )
        self.max_retries = int(
            max_retries if max_retries is not None else settings.shopee_http_max_retries
        )
        self._sleep = sleep_func
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(self.timeout),
            headers={"Accept": "application/json"},
        )
        self._owns_client = http_client is None

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ShopeeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ 签名
    def _signed_query(self, path: str, timestamp: int, *, kind: str = "shop") -> dict[str, Any]:
        query: dict[str, Any] = {"partner_id": self.partner_id, "timestamp": timestamp}
        if kind == "shop":
            query["access_token"] = self.access_token
            query["shop_id"] = self.shop_id
            query["sign"] = sign_shop(
                self.partner_id,
                self.partner_key,
                path,
                timestamp,
                self.access_token,
                self.shop_id,
            )
        else:
            query["sign"] = sign_auth(self.partner_id, self.partner_key, path, timestamp)
        return query

    # ------------------------------------------------------------------ 请求
    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        kind: str = "shop",
    ) -> dict[str, Any]:
        """签名 + 发送请求；仅对暂时性错误做有限重试。"""

        def build_params(_attempt: int) -> dict[str, Any]:
            ts = int(time.time())
            params = self._signed_query(path, ts, kind=kind)
            if query:
                params.update({key: value for key, value in query.items() if value is not None})
            return params

        return self._send(method, path, build_params, json_body)

    def _send(
        self,
        method: str,
        path: str,
        build_params: Callable[[int], Mapping[str, Any]],
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts = max(0, self.max_retries) + 1
        last_error: ShopeeApiError | None = None
        for attempt in range(attempts):
            try:
                response = self._client.request(
                    method,
                    f"{self.base_url}{path}",
                    params=dict(build_params(attempt)),
                    json=dict(json_body) if json_body is not None else None,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = ShopeeApiError(
                    f"请求 Shopee 超时或网络错误：{exc.__class__.__name__}", transient=True
                )
                if attempt + 1 < attempts:
                    self._sleep(retry_delay_seconds(attempt))
                    continue
                raise last_error from exc
            if response.status_code in TRANSIENT_STATUS_CODES and attempt + 1 < attempts:
                self._sleep(retry_delay_seconds(attempt))
                continue
            return self._parse_response(response)
        raise last_error or ShopeeApiError("请求 Shopee 失败", transient=True)

    @staticmethod
    def _parse_response(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code >= 400:
            if isinstance(data, dict):
                message = str(data.get("message") or data.get("error") or response.text[:200])
                error_code = str(data.get("error") or data.get("error_code") or "")
            else:
                message = response.text[:200] or f"HTTP {response.status_code}"
                error_code = ""
            raise ShopeeApiError(
                message,
                error_code=error_code,
                status_code=response.status_code,
                transient=is_transient_status(response.status_code)
                or is_transient_error_code(error_code),
                payload=data if isinstance(data, dict) else None,
            )
        if not isinstance(data, dict):
            raise ShopeeApiError("Shopee 返回了非 JSON 对象", status_code=response.status_code)
        error_code = str(data.get("error") or data.get("error_code") or "")
        if error_code:
            raise ShopeeApiError(
                str(data.get("message") or error_code),
                error_code=error_code,
                status_code=response.status_code,
                transient=is_transient_error_code(error_code),
                payload=data,
            )
        return data

    # ------------------------------------------------------------------ 公共 / 授权接口
    def get_public(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, query=params, kind="auth")

    def post_public(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", path, query=params, json_body=body, kind="auth")

    def request_access_token(self, code: str, shop_id: int | str) -> dict[str, Any]:
        """用授权码换取 access / refresh token。

        Shopee 文档的签名只含 partner_id + path + timestamp；个别网关账号需要额外
        附带 shop_id，因此仅在返回 ``error_sign`` 时用变体重试一次。
        """
        shop = int(shop_id)
        body = {"code": str(code).strip(), "shop_id": shop, "partner_id": self.partner_id}
        try:
            return self.post_public(AUTH_TOKEN_GET_PATH, body)
        except ShopeeApiError as exc:
            if exc.error_code != "error_sign":
                raise
        logger.warning("Shopee token/get 主签名被拒，尝试带 shop_id 的签名变体")

        def build_params(_attempt: int) -> dict[str, Any]:
            ts = int(time.time())
            sign = hmac_sha256_hex(
                self.partner_key, f"{self.partner_id}{AUTH_TOKEN_GET_PATH}{ts}{shop}"
            )
            return {"partner_id": self.partner_id, "timestamp": ts, "sign": sign}

        return self._send("POST", AUTH_TOKEN_GET_PATH, build_params, body)

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        """用 refresh token 换取新令牌（签名 base string 含 refresh_token）。"""
        if not refresh_token:
            raise ShopeeApiError("缺少 refresh_token，需要重新授权", error_code="missing_refresh_token")
        body = {
            "refresh_token": refresh_token,
            "shop_id": self.shop_id,
            "partner_id": self.partner_id,
        }
        timestamp = int(time.time())
        params = {
            "partner_id": self.partner_id,
            "timestamp": timestamp,
            "sign": sign_refresh(
                self.partner_id,
                self.partner_key,
                AUTH_ACCESS_TOKEN_GET_PATH,
                timestamp,
                refresh_token,
            ),
        }
        try:
            response = self._client.request(
                "POST",
                f"{self.base_url}{AUTH_ACCESS_TOKEN_GET_PATH}",
                params=params,
                json=body,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ShopeeApiError("刷新 Shopee 令牌超时或网络错误", transient=True) from exc
        return self._parse_response(response)

    # ------------------------------------------------------------------ 店铺接口
    def shop_get(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._request("GET", path, query=params, kind="shop")

    def shop_post(
        self,
        path: str,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", path, query=params, json_body=body, kind="shop")

    # ------------------------------------------------------------------ 业务封装
    @staticmethod
    def _response(data: Mapping[str, Any]) -> Mapping[str, Any]:
        response = data.get("response")
        return response if isinstance(response, Mapping) else data

    def get_shop_info(self) -> dict[str, Any]:
        return dict(self._response(self.shop_get(SHOP_INFO_PATH)))

    def get_item_list(
        self,
        *,
        offset: int = 0,
        page_size: int = ITEM_BATCH_SIZE,
        item_status: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "offset": max(0, int(offset)),
            "page_size": max(1, min(int(page_size), ITEM_BATCH_SIZE)),
        }
        if item_status:
            params["item_status"] = item_status
        return dict(self._response(self.shop_get(ITEM_LIST_PATH, params)))

    def get_item_base_info(self, item_ids: Iterable[int | str]) -> list[dict[str, Any]]:
        ids = [int(item) for item in item_ids]
        if not ids:
            return []
        data = self.shop_get(
            ITEM_BASE_INFO_PATH,
            {"item_id_list": ",".join(str(item) for item in ids[:ITEM_BATCH_SIZE])},
        )
        response = self._response(data)
        items = response.get("item_list") or []
        return [dict(item) for item in items if isinstance(item, Mapping)]

    def get_model_list(self, item_id: int | str) -> list[dict[str, Any]]:
        data = self.shop_get(MODEL_LIST_PATH, {"item_id": int(item_id)})
        response = self._response(data)
        models = response.get("model") or response.get("model_list") or []
        return [dict(model) for model in models if isinstance(model, Mapping)]

    def update_stock(
        self, item_id: int | str, stock_list: list[dict[str, Any]]
    ) -> dict[str, Any]:
        body = {"item_id": int(item_id), "stock_list": stock_list}
        return dict(self._response(self.shop_post(UPDATE_STOCK_PATH, body)))

    def get_order_list(
        self,
        *,
        time_from: int,
        time_to: int,
        page_size: int = ORDER_BATCH_SIZE,
        cursor: str = "",
        time_range_field: str = "update_time",
        response_optional_fields: str = "order_status",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "time_range_field": time_range_field,
            "time_from": int(time_from),
            "time_to": int(time_to),
            "page_size": max(1, min(int(page_size), ORDER_BATCH_SIZE)),
            "response_optional_fields": response_optional_fields,
        }
        if cursor:
            params["cursor"] = cursor
        return dict(self._response(self.shop_get(ORDER_LIST_PATH, params)))

    def get_order_detail(self, order_sn_list: Iterable[str]) -> list[dict[str, Any]]:
        numbers = [str(item) for item in order_sn_list if str(item)]
        if not numbers:
            return []
        data = self.shop_get(
            ORDER_DETAIL_PATH,
            {
                "order_sn_list": ",".join(numbers[:ORDER_BATCH_SIZE]),
                "response_optional_fields": "buyer_username,item_list,recipient_address",
            },
        )
        response = self._response(data)
        orders = response.get("order_list") or []
        return [dict(order) for order in orders if isinstance(order, Mapping)]


def build_client_for_environment(
    *,
    environment: str,
    partner_id: int,
    partner_key: str,
    access_token: str = "",
    shop_id: int | str = 0,
) -> ShopeeClient:
    """按环境构造客户端（生产 / 沙箱地址不同）。"""
    return ShopeeClient(
        partner_id=partner_id,
        partner_key=partner_key,
        access_token=access_token,
        shop_id=shop_id,
        base_url=resolve_base_url(environment),
    )


def now_utc() -> datetime:
    """当前 UTC 时间（naive，入库统一 UTC）。"""
    return utc_now()
