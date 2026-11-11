"""渠道 / Shopee 纯逻辑单元测试（无数据库、无 Redis、无真实平台调用）。

覆盖：

* v2 签名算法（授权、店铺接口、刷新令牌、推送验签）；
* HTTPX 客户端超时 / 暂时性错误有限重试与签名参数；
* 一次性授权会话 state 解析与校验（Redis 键必须含 tenant_id）；
* Shopee 推送类型识别、防重键与验签地址变体；
* 平台商品解析、规格推导与 SKU 自动匹配唯一性规则；
* 库存推送数量下限 0 与请求体构造；
* 渠道数据范围过滤（店铺范围）与安全跳转路径。

实现负责人：backend-channels。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import AppError, ErrorCode
from app.core.permissions import Permission
from app.modules.channels import service
from app.modules.channels.constants import (
    ENV_PRODUCTION,
    ENV_TEST,
    LINK_STATUS_AMBIGUOUS,
    LINK_STATUS_MAPPED,
    LINK_STATUS_PENDING,
    STORE_STATUS_CONNECTED,
)
from app.modules.channels.shopee import client as shopee_client
from app.modules.channels.shopee import oauth
from app.modules.channels.shopee.client import (
    AUTH_ACCESS_TOKEN_GET_PATH,
    AUTH_TOKEN_GET_PATH,
    ShopeeApiError,
    ShopeeClient,
    is_auth_invalid_error_code,
    is_transient_error_code,
    is_transient_status,
    resolve_base_url,
    retry_delay_seconds,
    token_expiry,
)
from app.modules.channels.shopee.signature import (
    append_query_params,
    build_authorize_url,
    hmac_sha256_hex,
    is_expired,
    safe_redirect_path,
    sign_auth,
    sign_refresh,
    sign_shop,
    verify_webhook_signature,
    webhook_signature,
)

# --------------------------------------------------------------------------- 签名算法
PARTNER_ID = 123456
PARTNER_KEY = "partner-key-for-test"
ACCESS_TOKEN = "access-token-abc"
SHOP_ID = "998877"
PATH = "/api/v2/product/get_item_list"
TIMESTAMP = 1_700_000_000


def _hmac(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def test_sign_auth_matches_documented_base_string() -> None:
    expected = _hmac(PARTNER_KEY, f"{PARTNER_ID}{PATH}{TIMESTAMP}")
    assert sign_auth(PARTNER_ID, PARTNER_KEY, PATH, TIMESTAMP) == expected


def test_sign_shop_base_string_order_is_partner_path_timestamp_token_shop() -> None:
    expected = _hmac(PARTNER_KEY, f"{PARTNER_ID}{PATH}{TIMESTAMP}{ACCESS_TOKEN}{SHOP_ID}")
    assert (
        sign_shop(PARTNER_ID, PARTNER_KEY, PATH, TIMESTAMP, ACCESS_TOKEN, SHOP_ID) == expected
    )
    # 顺序敏感：交换 access_token / shop_id 必须得到不同签名
    swapped = _hmac(PARTNER_KEY, f"{PARTNER_ID}{PATH}{TIMESTAMP}{SHOP_ID}{ACCESS_TOKEN}")
    assert swapped != sign_shop(
        PARTNER_ID, PARTNER_KEY, PATH, TIMESTAMP, ACCESS_TOKEN, SHOP_ID
    )


def test_sign_refresh_uses_refresh_token_in_base_string() -> None:
    refresh = "refresh-token-xyz"
    expected = _hmac(PARTNER_KEY, f"{PARTNER_ID}{PATH}{TIMESTAMP}{refresh}")
    assert sign_refresh(PARTNER_ID, PARTNER_KEY, PATH, TIMESTAMP, refresh) == expected


def test_webhook_signature_and_verification() -> None:
    body = json.dumps({"code": 3, "shop_id": 998877, "data": {"ordersn": "A1"}}).encode()
    url = "https://erp.example.com/api/v1/integrations/shopee/webhook"
    signature = webhook_signature(PARTNER_KEY, url, body)
    assert signature == _hmac(PARTNER_KEY, f"{url}|{body.decode()}")
    assert verify_webhook_signature(PARTNER_KEY, [url], body, signature) is True
    # 调用方负责传入 http / https 等候选地址（service.webhook_candidate_urls）
    http_url = url.replace("https://", "http://")
    assert (
        verify_webhook_signature(PARTNER_KEY, [url, http_url], body, signature) is True
    )
    assert verify_webhook_signature(PARTNER_KEY, [url], body, "0" * 64) is False
    assert verify_webhook_signature("", [url], body, signature) is False


def test_build_authorize_url_contains_signed_query_and_redirect() -> None:
    redirect = "https://erp.example.com/api/v1/integrations/shopee/oauth/callback?state=t1.abc"
    url = build_authorize_url(
        auth_base="https://partner.shopeemobile.com/",
        partner_id=PARTNER_ID,
        partner_key=PARTNER_KEY,
        redirect_url=redirect,
        timestamp=TIMESTAMP,
    )
    assert url.startswith("https://partner.shopeemobile.com/api/v2/shop/auth_partner?")
    assert f"partner_id={PARTNER_ID}" in url
    assert f"timestamp={TIMESTAMP}" in url
    expected_sign = sign_auth(PARTNER_ID, PARTNER_KEY, "/api/v2/shop/auth_partner", TIMESTAMP)
    assert f"sign={expected_sign}" in url
    assert "redirect=" in url


def test_append_query_params_merges_without_dropping_existing() -> None:
    url = append_query_params("https://x.test/cb?tenant=1", {"state": "t1.abc"})
    assert "tenant=1" in url
    assert "state=t1.abc" in url


def test_safe_redirect_path_blocks_open_redirect() -> None:
    assert safe_redirect_path("/tenants/1/stores") == "/tenants/1/stores"
    assert safe_redirect_path("https://evil.example.com") == "/"
    assert safe_redirect_path("//evil.example.com") == "/"
    assert safe_redirect_path("javascript:alert(1)") == "/"
    assert safe_redirect_path("/ok?x=1#f") == "/ok?x=1#f"
    assert safe_redirect_path(None, "/fallback") == "/fallback"


# --------------------------------------------------------------------------- 客户端
def _make_client(handler, **kwargs) -> ShopeeClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return ShopeeClient(
        partner_id=PARTNER_ID,
        partner_key=PARTNER_KEY,
        access_token=ACCESS_TOKEN,
        shop_id=SHOP_ID,
        base_url="https://partner.shopeemobile.com",
        max_retries=kwargs.pop("max_retries", 2),
        http_client=http_client,
        sleep_func=lambda _seconds: None,
        **kwargs,
    )


def test_client_shop_get_signs_with_access_token_and_shop_id() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"error": "", "response": {"item": []}})

    client = _make_client(handler)
    result = client.shop_get(PATH, {"offset": 0})
    assert result["response"] == {"item": []}
    request = captured[0]
    params = dict(request.url.params)
    timestamp = int(params["timestamp"])
    assert params["partner_id"] == str(PARTNER_ID)
    assert params["access_token"] == ACCESS_TOKEN
    assert params["shop_id"] == SHOP_ID
    assert params["offset"] == "0"
    assert params["sign"] == sign_shop(
        PARTNER_ID, PARTNER_KEY, PATH, timestamp, ACCESS_TOKEN, SHOP_ID
    )
    client.close()


def test_client_retries_only_transient_status_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"error": "", "response": {"ok": True}})

    client = _make_client(handler, max_retries=2)
    assert client.shop_get(PATH)["response"] == {"ok": True}
    assert calls["n"] == 3
    client.close()


def test_client_does_not_retry_business_error() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"error": "error_param", "message": "invalid parameter"}
        )

    client = _make_client(handler, max_retries=3)
    with pytest.raises(ShopeeApiError) as exc_info:
        client.shop_get(PATH)
    assert exc_info.value.error_code == "error_param"
    assert exc_info.value.transient is False
    assert calls["n"] == 1
    client.close()


def test_client_retries_network_error_then_raises_transient() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    client = _make_client(handler, max_retries=1)
    with pytest.raises(ShopeeApiError) as exc_info:
        client.shop_get(PATH)
    assert exc_info.value.transient is True
    assert calls["n"] == 2
    client.close()


def test_refresh_access_token_signs_with_refresh_token() -> None:
    captured: list[httpx.Request] = []
    refresh = "refresh-token-xyz"

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expire_in": 7200},
        )

    client = _make_client(handler)
    data = client.refresh_access_token(refresh)
    assert data["access_token"] == "new-access"
    request = captured[0]
    params = dict(request.url.params)
    timestamp = int(params["timestamp"])
    assert request.url.path == AUTH_ACCESS_TOKEN_GET_PATH
    assert params["sign"] == sign_refresh(
        PARTNER_ID, PARTNER_KEY, AUTH_ACCESS_TOKEN_GET_PATH, timestamp, refresh
    )
    assert json.loads(request.content)["refresh_token"] == refresh
    client.close()


def test_request_access_token_falls_back_to_shop_id_signature_variant() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            return httpx.Response(200, json={"error": "error_sign", "message": "bad sign"})
        return httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expire_in": 3600}
        )

    client = _make_client(handler)
    data = client.request_access_token("code-1", SHOP_ID)
    assert data["access_token"] == "a"
    assert len(captured) == 2
    second = dict(captured[1].url.params)
    timestamp = int(second["timestamp"])
    assert second["sign"] == hmac_sha256_hex(
        PARTNER_KEY, f"{PARTNER_ID}{AUTH_TOKEN_GET_PATH}{timestamp}{SHOP_ID}"
    )
    client.close()


def test_resolve_base_url_and_transient_helpers() -> None:
    assert resolve_base_url(ENV_PRODUCTION) == "https://partner.shopeemobile.com"
    assert "sandbox" in resolve_base_url(ENV_TEST)
    assert is_transient_status(429) is True
    assert is_transient_status(400) is False
    assert is_transient_error_code("error_server") is True
    assert is_transient_error_code("error_param") is False
    assert is_auth_invalid_error_code("error_invalid_refresh_token") is True
    assert retry_delay_seconds(0) == 0.5
    assert retry_delay_seconds(1) == 1.0
    assert retry_delay_seconds(10) == 5.0


def test_token_expiry_and_is_expired() -> None:
    now = datetime(2026, 1, 1, 0, 0, 0)
    assert token_expiry(now, 3600) == now + timedelta(hours=1)
    assert token_expiry(now, "invalid") is None
    assert token_expiry(now, 0) is None
    assert is_expired(now + timedelta(seconds=10), now, skew_seconds=60) is True
    assert is_expired(now + timedelta(seconds=600), now, skew_seconds=60) is False
    assert is_expired(None, now) is True


# --------------------------------------------------------------------------- 授权会话
def test_state_embeds_tenant_id_and_parses_back() -> None:
    state = oauth.build_state(42)
    tenant_id, nonce = oauth.parse_state(state)
    assert tenant_id == 42
    assert len(nonce) >= 16
    assert state != oauth.build_state(42)  # 每次生成不同随机段


def test_authorization_session_key_contains_tenant_id() -> None:
    key = oauth.authorization_session_key(42, "nonce-abc")
    assert "42" in key
    assert key.startswith(oauth.OAUTH_SESSION_PREFIX)


@pytest.mark.parametrize("bad_state", [None, "", "abc", "t0.nonce", "t42.", "42.nonce"])
def test_parse_state_rejects_invalid_values(bad_state: str | None) -> None:
    with pytest.raises(AppError) as exc_info:
        oauth.parse_state(bad_state)
    assert exc_info.value.code == ErrorCode.CHANNEL_CALLBACK_UNKNOWN


def test_validate_authorization_session_checks_tenant_environment_and_expiry() -> None:
    now = datetime(2026, 1, 1, 0, 0, 0)
    payload = {
        "tenant_id": 7,
        "member_id": 9,
        "environment": ENV_TEST,
        "redirect_path": "/tenants/7/stores",
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
    }
    result = oauth.validate_authorization_session(
        payload, expected_tenant_id=7, expected_environment=ENV_TEST, now=now
    )
    assert result["tenant_id"] == 7
    assert result["environment"] == ENV_TEST

    with pytest.raises(AppError):
        oauth.validate_authorization_session(payload, expected_tenant_id=8, now=now)
    with pytest.raises(AppError):
        oauth.validate_authorization_session(
            payload, expected_environment=ENV_PRODUCTION, now=now
        )
    with pytest.raises(AppError):
        oauth.validate_authorization_session(
            payload, now=now + timedelta(minutes=10)
        )
    sanitized = oauth.validate_authorization_session(
        {**payload, "redirect_path": "https://evil.example.com"}, now=now
    )
    assert sanitized["redirect_path"] == "/tenants/7/stores"


def test_validate_authorization_session_rejects_incomplete_payload() -> None:
    with pytest.raises(AppError):
        oauth.validate_authorization_session({"tenant_id": 0, "member_id": 0})


def test_normalize_token_payload_and_expiry_times() -> None:
    token = oauth.normalize_token_payload(
        {"response": {"access_token": " a ", "refresh_token": " r ", "expire_in": "7200"}}
    )
    assert token == {
        "access_token": "a",
        "refresh_token": "r",
        "expire_in": 7200,
        "refresh_token_expire_in": 0,
    }
    now = datetime(2026, 1, 1)
    access_expires, refresh_expires = oauth.token_expiry_times(token, now=now)
    assert access_expires == now + timedelta(seconds=7200)
    assert refresh_expires == now + timedelta(days=30)


def test_validate_callback_params_and_display_name() -> None:
    assert oauth.validate_callback_params(" code ", " 123 ") == ("code", "123")
    with pytest.raises(AppError):
        oauth.validate_callback_params("", "123")
    with pytest.raises(AppError):
        oauth.validate_callback_params("code", "not-a-number")
    assert oauth.build_store_display_name("1", {"shop_name": " My Shop "}) == "My Shop"
    assert oauth.build_store_display_name("1", {}) == "Shopee 店铺 1"
    assert oauth.build_store_region({"region": "sg"}) == "SG"


def test_resolve_environment_defaults_to_test_outside_production() -> None:
    from app.modules.channels.constants import resolve_environment

    assert resolve_environment("production") == ENV_PRODUCTION
    assert resolve_environment("development") == ENV_TEST
    assert resolve_environment("test") == ENV_TEST


# --------------------------------------------------------------------------- 商品解析 / 匹配
def test_build_listing_specs_from_tier_variation_and_tier_index() -> None:
    item = {
        "tier_variation": [
            {"name": "颜色", "option_list": [{"option": "红"}, {"option": "蓝"}]},
            {"name": "尺码", "option_list": [{"option": "M"}, {"option": "L"}]},
        ]
    }
    model = {"tier_index": [1, 0], "model_sku": "SKU-BLUE-M"}
    specs = service.build_listing_specs(item, model)
    assert specs == {"颜色": "蓝", "尺码": "M"}
    # 无法推导时返回空，不猜规格
    assert service.build_listing_specs(item, {"tier_index": [9, 0]}) == {}
    assert service.build_listing_specs(None, model) == {}
    assert service.build_listing_specs(item, {}) == {}


def _candidates() -> list[service.SkuCandidate]:
    return [
        service.SkuCandidate(sku_id=11, product_id=101, sku_code="SKU-A", specs={}),
        service.SkuCandidate(
            sku_id=12, product_id=102, sku_code="SKU-B", specs={"颜色": "红", "尺码": "M"}
        ),
        service.SkuCandidate(
            sku_id=13, product_id=103, sku_code="SKU-C", specs={"颜色": "红", "尺码": "M"}
        ),
        service.SkuCandidate(sku_id=14, product_id=104, sku_code="SKU-D", specs={}),
        service.SkuCandidate(sku_id=15, product_id=104, sku_code="SKU-D", specs={}),
    ]


def test_match_unique_sku_code_maps() -> None:
    match = service.match_listing_to_skus(
        external_sku_id="SKU-A", listing_specs={}, candidates=_candidates()
    )
    assert match.status == LINK_STATUS_MAPPED
    assert match.sku_id == 11
    assert match.product_id == 101


def test_match_duplicate_sku_code_is_ambiguous() -> None:
    match = service.match_listing_to_skus(
        external_sku_id="SKU-D", listing_specs={}, candidates=_candidates()
    )
    assert match.status == LINK_STATUS_AMBIGUOUS
    assert match.sku_id is None


def test_match_specs_only_when_sku_code_missing_or_unknown() -> None:
    unique_but_two_candidates = service.match_listing_to_skus(
        external_sku_id="", listing_specs={"颜色": "红", "尺码": "M"}, candidates=_candidates()
    )
    assert unique_but_two_candidates.status == LINK_STATUS_AMBIGUOUS
    single = service.match_listing_to_skus(
        external_sku_id="",
        listing_specs={"颜色": "蓝", "尺码": "L"},
        candidates=[
            service.SkuCandidate(
                sku_id=21, product_id=201, sku_code="X", specs={"颜色": "蓝", "尺码": "L"}
            )
        ],
    )
    assert single.status == LINK_STATUS_MAPPED
    assert single.sku_id == 21


def test_match_without_any_hit_is_pending_not_guessed() -> None:
    match = service.match_listing_to_skus(
        external_sku_id="NOT-EXIST", listing_specs={}, candidates=_candidates()
    )
    assert match.status == LINK_STATUS_PENDING
    assert match.sku_id is None
    assert match.product_id is None


def test_normalize_specs_handles_json_string_and_list() -> None:
    assert service.normalize_specs('{"Color": " Red ", "Size": "M"}') == {
        "color": "red",
        "size": "m",
    }
    assert service.normalize_specs([{"name": "Color", "value": "Red"}]) == {"color": "red"}
    assert service.normalize_specs(None) == {}
    assert service.normalize_specs("not-json") == {}


def test_listing_unique_key_uses_empty_string_for_no_sku() -> None:
    assert service.listing_unique_key(5, " 100 ", None) == (5, "100", "")
    assert service.listing_unique_key(5, 100, " SKU-1 ") == (5, "100", "SKU-1")


def test_attach_item_statuses_fills_missing_from_list_response() -> None:
    item_infos = [{"item_id": 1}, {"item_id": "2", "item_status": "NORMAL"}]
    merged = service.attach_item_statuses(item_infos, {"1": "BANNED"})
    assert merged[0]["item_status"] == "BANNED"
    assert merged[1]["item_status"] == "NORMAL"
    # 不修改入参（避免污染调用方数据）
    assert "item_status" not in item_infos[0]


def test_parse_remote_listings_handles_variation_and_single_item() -> None:
    item_with_models = {
        "item_id": 1001,
        "item_name": "T 恤",
        "item_status": "NORMAL",
        "currency": "SGD",
        "tier_variation": [{"name": "颜色", "option_list": [{"option": "红"}]}],
        "price_info": [{"current_price": 19.9}],
        "stock_info_v2": {"summary_info": {"total_available_stock": 8}},
    }
    model = {
        "model_id": 55,
        "model_sku": "TS-RED",
        "tier_index": [0],
        "price_info": [{"current_price": 21.5}],
        "stock_info_v2": {"summary_info": {"total_available_stock": 3}},
    }
    single_item = {
        "item_id": "1002",
        "item_name": "杯子",
        "item_status": "UNLIST",
        "item_sku": "CUP-1",
        "price_info": [{"current_price": 5}],
        "stock_info_v2": {"seller_stock": [{"stock": 6}]},
    }
    rows = service.parse_remote_listings(
        [item_with_models, single_item], {"1001": [model]}
    )
    assert len(rows) == 2
    variation = rows[0]
    assert variation.external_item_id == "1001"
    assert variation.external_sku_id == "TS-RED"
    assert variation.price == Decimal("21.5")
    assert variation.remote_stock == 3
    assert variation.status == "active"
    assert variation.specs == {"颜色": "红"}
    single = rows[1]
    assert single.external_item_id == "1002"
    assert single.external_sku_id == "CUP-1"
    assert single.status == "inactive"
    assert single.remote_stock == 6
    assert single.price == Decimal("5")


# --------------------------------------------------------------------------- 库存推送
def test_clamp_stock_quantity_never_negative() -> None:
    assert service.clamp_stock_quantity(-5) == 0
    assert service.clamp_stock_quantity("7") == 7
    assert service.clamp_stock_quantity(None) == 0
    assert service.clamp_stock_quantity("abc") == 0


def test_extract_model_id_and_build_stock_update_payload() -> None:
    assert service.extract_listing_model_id({"model": {"model_id": 77}}) == 77
    assert service.extract_listing_model_id({"model_id": "88"}) == 88
    assert service.extract_listing_model_id({"model": {"model_sku": "x"}}) == 0
    assert service.extract_listing_model_id(None) == 0

    variation_payload = service.build_stock_update_payload("1001", 77, 5)
    assert variation_payload == {
        "item_id": 1001,
        "stock_list": [{"seller_stock": [{"stock": 5}], "model_id": 77}],
    }
    single_payload = service.build_stock_update_payload(1002, 0, -3)
    assert single_payload["stock_list"] == [{"seller_stock": [{"stock": 0}]}]
    assert "model_id" not in single_payload["stock_list"][0]


# --------------------------------------------------------------------------- 推送类型 / 防重
def test_webhook_event_type_detection() -> None:
    assert service.webhook_event_type(
        {"code": 3, "data": {"ordersn": "A1", "status": "READY_TO_SHIP"}}
    ) == "order_status"
    assert service.webhook_event_type(
        {"code": 4, "data": {"ordersn": "A1", "status": "SHIPPED"}}
    ) == "order_tracking"
    assert service.webhook_event_type({"code": 29, "data": {"return_sn": "R1"}}) == "return_update"
    assert service.webhook_event_type(
        {"code": 5, "data": {"item_id": 1, "item_status": "NORMAL"}}
    ) == "item_status"
    assert service.webhook_event_type({"code": 12, "data": {}}) == "code_12"
    assert service.webhook_event_type({}) == "unknown"


def test_webhook_dedupe_key_is_stable_and_status_sensitive() -> None:
    first = {"code": 3, "shop_id": 9, "timestamp": 100, "data": {"ordersn": "A1", "status": "X"}}
    second = {"data": {"status": "X", "ordersn": "A1"}, "shop_id": 9, "code": 3, "timestamp": 999}
    changed = {"code": 3, "shop_id": 9, "data": {"ordersn": "A1", "status": "Y"}}
    assert service.webhook_dedupe_key(first) == service.webhook_dedupe_key(second)
    assert service.webhook_dedupe_key(first) != service.webhook_dedupe_key(changed)
    assert len(service.webhook_dedupe_key(first)) == 64


def test_webhook_candidate_urls_include_variants() -> None:
    urls = service.webhook_candidate_urls(
        "https://erp.example.com/api/v1/integrations/shopee/webhook?x=1"
    )
    assert "https://erp.example.com/api/v1/integrations/shopee/webhook?x=1" in urls
    assert "http://erp.example.com/api/v1/integrations/shopee/webhook?x=1" in urls
    assert "https://erp.example.com/api/v1/integrations/shopee/webhook" in urls


def test_verify_shopee_webhook_prefers_partner_secret_then_key() -> None:
    config = service.ChannelAppConfig(
        channel="shopee",
        environment=ENV_TEST,
        partner_id=PARTNER_ID,
        partner_key=PARTNER_KEY,
        partner_secret="push-secret",
        redirect_url="",
        status="active",
    )
    url = "https://erp.example.com/api/v1/integrations/shopee/webhook"
    body = b'{"code": 3, "shop_id": 9}'
    push_signature = webhook_signature("push-secret", url, body)
    assert service.verify_shopee_webhook(
        config, request_url=url, raw_body=body, received_signature=push_signature
    ) is True
    key_signature = webhook_signature(PARTNER_KEY, url, body)
    assert service.verify_shopee_webhook(
        config, request_url=url, raw_body=body, received_signature=key_signature
    ) is True
    assert service.verify_shopee_webhook(
        config, request_url=url, raw_body=body, received_signature="deadbeef"
    ) is False


# --------------------------------------------------------------------------- 数据范围
def test_allowed_store_ids_respects_single_grant_scope() -> None:
    effective = EffectivePermissions(
        grants=(
            ScopeGrant(
                role_binding_id=1,
                role_id=1,
                scope_type="stores",
                scope_ids=frozenset({3}),
                permissions=frozenset({Permission.STORE_READ.value}),
            ),
        )
    )
    assert service.allowed_store_ids(effective, Permission.STORE_READ.value) == frozenset({3})
    assert service.store_in_scope(3, frozenset({3})) is True
    assert service.store_in_scope(4, frozenset({3})) is False
    assert service.store_in_scope(4, None) is True
    assert service.store_in_scope(4, frozenset()) is False


def test_allowed_store_ids_returns_none_for_all_scope_and_empty_for_no_grant() -> None:
    all_scope = EffectivePermissions(
        grants=(
            ScopeGrant(
                role_binding_id=1,
                role_id=1,
                scope_type="all",
                permissions=frozenset({Permission.STORE_READ.value}),
            ),
        )
    )
    assert service.allowed_store_ids(all_scope, Permission.STORE_READ.value) is None
    assert service.allowed_store_ids(EffectivePermissions(), Permission.STORE_READ.value) == frozenset()


# --------------------------------------------------------------------------- 请求模型
def test_request_schemas_accept_numeric_ids_and_validate_redirect_path() -> None:
    from pydantic import ValidationError

    from app.modules.channels import schemas

    sync_in = schemas.ListingSyncIn(store_id=10)
    assert sync_in.store_id == "10" and sync_in.store_id_int == 10
    link_in = schemas.ListingLinkIn(product_id="5", sku_id=6)
    assert link_in.product_id_int == 5 and link_in.sku_id_int == 6
    with pytest.raises(ValidationError):
        schemas.ListingSyncIn(store_id="-1")
    with pytest.raises(ValidationError):
        schemas.ListingLinkIn(product_id="0", sku_id="1")
    authorize_in = schemas.ShopeeAuthorizeIn(
        environment="test", redirect_path="/tenants/1/stores"
    )
    assert authorize_in.redirect_path == "/tenants/1/stores"
    with pytest.raises(ValidationError):
        schemas.ShopeeAuthorizeIn(redirect_path="https://evil.example.com")
    with pytest.raises(ValidationError):
        schemas.ShopeeAuthorizeIn(environment="staging")


# --------------------------------------------------------------------------- Webhook 补偿 / stale
def test_webhook_event_due_boundary_and_timezone() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert service.webhook_event_due(now - timedelta(seconds=121), now=now) is True
    assert service.webhook_event_due(now - timedelta(seconds=119), now=now) is False
    assert service.webhook_event_due(now - timedelta(seconds=120), now=now) is True
    assert service.webhook_event_due(None, now=now) is False
    aware = (now - timedelta(seconds=130)).replace(tzinfo=timezone.utc)
    assert service.webhook_event_due(aware, now=now) is True


def test_plan_compensation_attempt_progresses_to_failed_at_limit() -> None:
    first = service.plan_compensation_attempt(0)
    assert first["attempts"] == 1 and first["failed"] is False
    assert first["status"] == "received"

    last = service.plan_compensation_attempt(4, "订单同步失败", max_attempts=5)
    assert last["attempts"] == 5 and last["failed"] is True
    assert last["status"] == "failed"
    assert last["last_error"] == "订单同步失败"

    malformed = service.plan_compensation_attempt("bad", "x", max_attempts=3)
    assert malformed["attempts"] == 1


def test_merge_compensation_meta_preserves_business_payload() -> None:
    original = {"code": 3, "shop_id": 9, "data": {"ordersn": "A1"}}
    now = datetime(2026, 1, 1, 12, 0, 0)
    merged = service.merge_compensation_meta(
        original, attempts=2, last_error="boom", attempted_at=now, last_result="received"
    )
    assert merged["code"] == 3 and merged["data"] == {"ordersn": "A1"}
    meta = merged[service.WEBHOOK_COMPENSATION_META_KEY]
    assert meta["attempts"] == 2
    assert meta["last_error"] == "boom"
    assert meta["last_result"] == "received"
    assert meta["last_attempt_at"].startswith("2026-01-01T12:00:00")
    # 原 payload 不被修改，重复读取返回同一元数据
    assert service.WEBHOOK_COMPENSATION_META_KEY not in original
    assert service.compensation_meta(merged)["attempts"] == 2
    assert service.compensation_meta(None) == {}


def test_find_stale_listings_keeps_absent_remote_rows() -> None:
    from types import SimpleNamespace

    existing = [
        SimpleNamespace(store_id=1, external_item_id="100", external_sku_id="A"),
        SimpleNamespace(store_id=1, external_item_id="101", external_sku_id=""),
    ]
    remote_keys = {(1, "100", "A")}
    stale = service.find_stale_listings(existing, remote_keys)
    assert [row.external_item_id for row in stale] == ["101"]
    assert service.find_stale_listings(existing, {(1, "100", "A"), (1, "101", "")}) == []


def test_compensation_only_covers_order_events_and_task_registered() -> None:
    from app.modules.channels import tasks as channel_tasks

    assert service.COMPENSATION_ORDER_EVENT_TYPES == frozenset(
        {"order_status", "order_tracking", "return_update"}
    )
    assert (
        channel_tasks.compensate_webhook_events.name
        == "app.modules.channels.tasks.compensate_webhook_events"
    )
    assert callable(channel_tasks.compensate_tenant_webhook_events)


# --------------------------------------------------------------------------- 常量一致性
def test_store_status_constants_used_by_services() -> None:
    assert STORE_STATUS_CONNECTED == "connected"
    assert shopee_client.SANDBOX_BASE_URL.startswith("https://")
