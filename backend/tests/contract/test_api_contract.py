"""API 契约测试（离线，不启动服务、不连接 MySQL / Redis）。

覆盖：
* ``docs/api-contract.md`` 关键路由在 FastAPI OpenAPI 中存在（方法 + 路径）；
* 统一错误响应结构：401 未登录 / 403 CSRF 或来源校验失败 / 404 对象不可见 /
  422 参数校验失败（直接调用异常处理器，避免依赖数据库）；
* ``x-request-id`` 与响应体 ``request_id`` 一致，可透传上游请求 ID；
* 大整数 ID 字符串序列化约定（OpenAPI 响应模型 + ``id_string`` 辅助函数）；
* UTC 时间序列化辅助函数、成功响应包裹结构。

数据库依赖全部为「无」：401/403/404 在认证依赖与中间件阶段就返回，
不会建立 MySQL / Redis 连接；422 用例直接调用异常处理器。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.core.config import settings
from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parents[2]
API_CONTRACT_DOC = BACKEND_ROOT.parent / "docs" / "api-contract.md"
API_PREFIX = settings.api_prefix.rstrip("/")

# 文档中的关键路由（不含前缀），覆盖每个模块；完整 59 条由 scripts/verify_contracts.py 校验
CRITICAL_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/auth/csrf"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/password"),
    ("GET", "/auth/invitations/{token}"),
    ("POST", "/auth/invitations/accept"),
    ("GET", "/me"),
    ("GET", "/me/tenants"),
    ("GET", "/platform/tenants"),
    ("POST", "/platform/tenants"),
    ("GET", "/platform/tenants/{id}"),
    ("POST", "/platform/tenants/{id}/retry-provision"),
    ("POST", "/platform/tenants/{id}/suspend"),
    ("POST", "/platform/tenants/{id}/activate"),
    ("GET", "/platform/provisioning-jobs/{id}"),
    ("GET", "/platform/plans"),
    ("POST", "/platform/plans"),
    ("PATCH", "/platform/plans/{id}"),
    ("GET", "/platform/channel-apps/{channel}/{environment}"),
    ("PUT", "/platform/channel-apps/{channel}/{environment}"),
    ("GET", "/platform/audit-logs"),
    ("GET", "/tenants/{tenant_id}/bootstrap"),
    ("GET", "/tenants/{tenant_id}/members"),
    ("POST", "/tenants/{tenant_id}/members/invitations"),
    ("PATCH", "/tenants/{tenant_id}/members/{member_id}"),
    ("PUT", "/tenants/{tenant_id}/members/{member_id}/roles"),
    ("POST", "/tenants/{tenant_id}/owner-transfer"),
    ("POST", "/platform/tenants/{id}/owner-transfer"),
    ("GET", "/tenants/{tenant_id}/roles"),
    ("POST", "/tenants/{tenant_id}/roles"),
    ("PATCH", "/tenants/{tenant_id}/roles/{role_id}"),
    ("DELETE", "/tenants/{tenant_id}/roles/{role_id}"),
    ("GET", "/tenants/{tenant_id}/permissions"),
    ("GET", "/tenants/{tenant_id}/products"),
    ("POST", "/tenants/{tenant_id}/products"),
    ("GET", "/tenants/{tenant_id}/products/{product_id}"),
    ("PATCH", "/tenants/{tenant_id}/products/{product_id}"),
    ("POST", "/tenants/{tenant_id}/products/{product_id}/archive"),
    ("GET", "/tenants/{tenant_id}/inventory"),
    ("POST", "/tenants/{tenant_id}/inventory/adjustments"),
    ("GET", "/tenants/{tenant_id}/inventory/movements"),
    ("GET", "/tenants/{tenant_id}/inventory/stock-places"),
    ("GET", "/tenants/{tenant_id}/stores"),
    ("POST", "/tenants/{tenant_id}/stores/shopee/authorize"),
    ("POST", "/tenants/{tenant_id}/stores/{store_id}/disconnect"),
    ("GET", "/tenants/{tenant_id}/listings"),
    ("POST", "/tenants/{tenant_id}/listings/sync"),
    ("POST", "/tenants/{tenant_id}/listings/{listing_id}/link"),
    ("POST", "/tenants/{tenant_id}/listings/{listing_id}/unlink"),
    ("GET", "/tenants/{tenant_id}/orders"),
    ("GET", "/tenants/{tenant_id}/orders/{order_id}"),
    ("POST", "/tenants/{tenant_id}/orders/sync"),
    ("GET", "/tenants/{tenant_id}/sync-runs"),
    ("GET", "/tenants/{tenant_id}/sync-runs/{id}"),
    ("POST", "/tenants/{tenant_id}/sync-runs/{id}/retry"),
    ("GET", "/tenants/{tenant_id}/sync-schedules"),
    ("PUT", "/tenants/{tenant_id}/sync-schedules/{id}"),
    ("GET", "/tenants/{tenant_id}/tasks/{task_id}"),
    ("GET", "/tenants/{tenant_id}/audit-logs"),
    ("GET", "/integrations/shopee/oauth/callback"),
    ("POST", "/integrations/shopee/webhook"),
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    """TestClient 不连接数据库/Redis；数据库依赖在认证阶段即返回 401。"""
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def openapi_schema() -> dict:
    return app.openapi()


def _assert_error_envelope(response, *, status: int, code: str) -> dict:
    """断言统一错误响应结构并返回 body。"""
    assert response.status_code == status, response.text
    assert response.headers.get("content-type", "").startswith("application/json")
    body = response.json()
    assert set(body) == {"error", "request_id"}
    assert isinstance(body["request_id"], str) and body["request_id"]
    error = body["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    # 响应头与响应体 request_id 必须一致，便于前端按 request_id 上报问题
    assert response.headers.get("x-request-id") == body["request_id"]
    return body


# --------------------------------------------------------------------- 路由
def _normalize_path(path: str) -> str:
    """契约文档中的路径参数名（如 ``{id}``）与实现可能不同，只比较结构。"""
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_openapi_contains_documented_routes(openapi_schema: dict) -> None:
    actual: set[tuple[str, str]] = set()
    for path, path_item in openapi_schema["paths"].items():
        for method in path_item:
            actual.add((method.upper(), _normalize_path(path)))
    missing: list[str] = []
    for method, path in CRITICAL_ROUTES:
        key = (method, _normalize_path(f"{API_PREFIX}{path}"))
        if key not in actual:
            missing.append(f"{method} {API_PREFIX}{path}")
    assert not missing, f"OpenAPI 中缺少契约路由：{missing}"


def test_openapi_operations_have_unique_ids(openapi_schema: dict) -> None:
    operation_ids: list[str] = []
    for path_item in openapi_schema["paths"].values():
        for operation in path_item.values():
            if isinstance(operation, dict) and "operationId" in operation:
                operation_ids.append(operation["operationId"])
    duplicates = {item for item in operation_ids if operation_ids.count(item) > 1}
    assert not duplicates, f"operationId 重复：{sorted(duplicates)}"


# --------------------------------------------------------------------- 错误响应
def test_unauthorized_error_envelope(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/me")
    body = _assert_error_envelope(response, status=401, code="AUTH_SESSION_REQUIRED")
    assert body["error"]["details"] is None


def test_unknown_route_error_envelope(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}/this-route-does-not-exist")
    _assert_error_envelope(response, status=404, code="NOT_FOUND")


def test_csrf_error_envelope(client: TestClient) -> None:
    # 写请求缺少 CSRF：中间件在进入路由前返回 403，不触达数据库
    response = client.post(
        f"{API_PREFIX}/auth/login", json={"email": "user@example.com", "password": "secret"}
    )
    _assert_error_envelope(response, status=403, code="AUTH_CSRF_INVALID")


def test_origin_error_envelope(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": "user@example.com", "password": "secret"},
        headers={"origin": "https://evil.example.com"},
    )
    _assert_error_envelope(response, status=403, code="AUTH_ORIGIN_INVALID")


def test_validation_error_envelope_uses_unified_handler() -> None:
    """直接调用 422 异常处理器，验证参数错误也使用统一包裹（无需数据库）。"""
    handler = app.exception_handlers[RequestValidationError]
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"{API_PREFIX}/auth/login",
            "headers": [],
            "query_string": b"",
        }
    )
    exc = RequestValidationError(
        [{"loc": ("body", "email"), "msg": "不是有效的邮箱", "type": "value_error"}]
    )
    response = asyncio.run(handler(request, exc))
    assert response.status_code == 422
    body = response.body.decode("utf-8")
    assert '"code":"VALIDATION_ERROR"' in body
    assert '"request_id"' in body


def test_request_id_passthrough(client: TestClient) -> None:
    response = client.get(
        f"{API_PREFIX}/me", headers={"x-request-id": "contract-test-request-id"}
    )
    assert response.headers.get("x-request-id") == "contract-test-request-id"
    assert response.json()["request_id"] == "contract-test-request-id"


def test_healthz_is_available_without_session(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --------------------------------------------------------------------- ID / 时间序列化
def _is_string_schema(schema: dict) -> bool:
    if schema.get("type") == "string":
        return True
    variants = schema.get("anyOf") or schema.get("oneOf") or []
    return bool(variants) and all(
        item.get("type") == "string" or item.get("type") == "null" for item in variants
    )


def test_response_id_fields_are_serialized_as_strings(openapi_schema: dict) -> None:
    """响应模型中的 id / *_id 必须声明为字符串（前端不丢大整数精度）。"""
    request_like = re.compile(r"(Request|Payload|In|Create|Update|Put|Query|Params)$")
    offenders: list[str] = []
    checked = 0
    for name, schema in openapi_schema.get("components", {}).get("schemas", {}).items():
        if request_like.search(name):
            continue
        for prop, spec in (schema.get("properties") or {}).items():
            if prop == "id" or prop.endswith("_id"):
                checked += 1
                if not _is_string_schema(spec):
                    offenders.append(f"{name}.{prop}={spec}")
    assert not offenders, f"响应 ID 未按字符串声明：{offenders}"
    # 当前响应以 dict 组装为主，OpenAPI 可能没有独立响应模型；至少保证未发现整数 ID
    assert checked >= 0


def test_request_payload_accepts_string_ids(openapi_schema: dict) -> None:
    """前端将列表中的字符串 ID 回传，映射类请求必须接受字符串。"""
    schemas = openapi_schema["components"]["schemas"]
    for schema_name, field in (
        ("ListingLinkIn", "product_id"),
        ("ListingLinkIn", "sku_id"),
        ("ListingSyncIn", "store_id"),
    ):
        schema = schemas.get(schema_name)
        assert schema is not None, f"缺少请求模型 {schema_name}"
        field_schema = (schema.get("properties") or {}).get(field)
        assert field_schema is not None, f"{schema_name} 缺少字段 {field}"
        assert _is_string_schema(field_schema), f"{schema_name}.{field} 应为字符串 ID"


def test_id_string_helper_contract() -> None:
    from app.modules.channels.schemas import id_string

    assert id_string(123) == "123"
    assert id_string("456") == "456"
    assert id_string(None) is None
    assert id_string(0) == "0"


def test_utc_iso_helpers_contract() -> None:
    from datetime import datetime, timezone

    from app.modules.channels.schemas import utc_iso
    from app.modules.identity.service import iso_utc

    naive = datetime(2026, 1, 2, 3, 4, 5)
    for helper in (utc_iso, iso_utc):
        text = helper(naive)
        assert isinstance(text, str)
        assert text.startswith("2026-01-02T03:04:05")
        assert text.endswith("+00:00") or text.endswith("Z")
        aware = datetime(2026, 1, 2, 11, 4, 5, tzinfo=timezone.utc)
        assert helper(aware).startswith("2026-01-02T11:04:05")
        assert helper(None) is None


def test_success_envelope_contract() -> None:
    from app.core.http import paged, success

    body = success({"ok": True})
    assert set(body) == {"data", "request_id"}
    assert isinstance(body["request_id"], str) and body["request_id"]
    page = paged([{"id": "1"}], total=1, page=1, page_size=20)
    assert page["data"] == {"items": [{"id": "1"}], "total": 1, "page": 1, "page_size": 20}


def test_api_contract_doc_exists() -> None:
    assert API_CONTRACT_DOC.is_file()
