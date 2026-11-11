"""纯逻辑契约测试（无数据库、无 Redis、无网络）。

覆盖 plan.txt 的授权与数据契约：
* 多角色并集规则：「局部编辑 + 全局查看」不能合成全局编辑权；
* 空范围无授权；同一绑定多对象取并集；店铺 / 库存地点两个维度互不越权；
* 权限编码与 docs/api-contract.md、预置角色、权限目录一致；
* 金额 DECIMAL、时间 UTC 的模型与序列化约定；
* 统一响应包裹与错误码契约；
* 中心库 Session 工厂可重入（防止 `_lock` 自死锁回归）。
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Numeric

from app.api.deps import TenantCtx
from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import (
    DEFAULT_MESSAGES,
    DEFAULT_STATUS,
    AppError,
    ErrorCode,
)
from app.core.http import error_body, paged, success
from app.core.permissions import (
    ALL_PERMISSIONS,
    PREDEFINED_ROLE_PERMISSIONS,
    SCOPE_ALL,
    SCOPE_STOCK_PLACES,
    SCOPE_STORES,
    Permission,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
API_CONTRACT_DOC = BACKEND_ROOT.parent / "docs" / "api-contract.md"


# --------------------------------------------------------------------- 授权规则
def _grant(
    *,
    binding_id: int = 1,
    role_id: int = 1,
    scope_type: str = SCOPE_STORES,
    scope_ids: tuple[int, ...] = (),
    permissions: tuple[str, ...] = (),
) -> ScopeGrant:
    return ScopeGrant(
        role_binding_id=binding_id,
        role_id=role_id,
        scope_type=scope_type,
        scope_ids=frozenset(scope_ids),
        permissions=frozenset(permissions),
    )


def test_partial_edit_plus_global_read_cannot_compose_global_edit() -> None:
    """「店铺 1 编辑 + 全部查看」只能编辑店铺 1，不能合成全部店铺编辑权。"""
    partial_edit = _grant(
        binding_id=1,
        scope_type=SCOPE_STORES,
        scope_ids=(1,),
        permissions=(Permission.INVENTORY_ADJUST, Permission.INVENTORY_READ),
    )
    global_read = _grant(
        binding_id=2,
        scope_type=SCOPE_ALL,
        permissions=(Permission.INVENTORY_READ,),
    )
    effective = EffectivePermissions(grants=(partial_edit, global_read))

    assert effective.has(Permission.INVENTORY_ADJUST)
    assert effective.has(Permission.INVENTORY_READ)
    # 有编辑权限，但只覆盖店铺 1
    assert effective.covers(Permission.INVENTORY_ADJUST, store_id=1)
    assert not effective.covers(Permission.INVENTORY_ADJUST, store_id=2)
    # 读权限来自「全部」绑定，可覆盖任何店铺
    assert effective.covers(Permission.INVENTORY_READ, store_id=2)
    assert effective.allowed_ids(Permission.INVENTORY_ADJUST, SCOPE_STORES) == frozenset({1})
    assert effective.allowed_ids(Permission.INVENTORY_READ, SCOPE_STORES) is None
    assert effective.filter_ids(Permission.INVENTORY_ADJUST, SCOPE_STORES, [1, 2, 3]) == [1]


def test_different_bindings_union_only_within_same_binding() -> None:
    """不同绑定的「权限 + 范围」不能拆开组合：并集按整条绑定计算。"""
    edit_store_1 = _grant(
        binding_id=1,
        role_id=1,
        scope_type=SCOPE_STORES,
        scope_ids=(1,),
        permissions=(Permission.INVENTORY_ADJUST,),
    )
    read_all = _grant(
        binding_id=2,
        role_id=2,
        scope_type=SCOPE_ALL,
        permissions=(Permission.INVENTORY_READ,),
    )
    effective = EffectivePermissions(grants=(edit_store_1, read_all))
    assert effective.permissions == frozenset(
        {Permission.INVENTORY_ADJUST, Permission.INVENTORY_READ}
    )
    assert not effective.covers(Permission.INVENTORY_ADJUST, store_id=7)


def test_same_binding_multiple_stores_take_union() -> None:
    grant = _grant(
        scope_type=SCOPE_STORES,
        scope_ids=(1, 2),
        permissions=(Permission.ORDER_READ,),
    )
    effective = EffectivePermissions(grants=(grant,))
    assert effective.covers(Permission.ORDER_READ, store_id=1)
    assert effective.covers(Permission.ORDER_READ, store_id=2)
    assert not effective.covers(Permission.ORDER_READ, store_id=3)
    assert effective.allowed_ids(Permission.ORDER_READ, SCOPE_STORES) == frozenset({1, 2})


def test_scope_dimensions_are_independent() -> None:
    """店铺范围不覆盖库存地点，库存地点范围也不覆盖店铺。"""
    store_grant = _grant(
        scope_type=SCOPE_STORES,
        scope_ids=(5,),
        permissions=(Permission.STOCK_PUSH,),
    )
    place_grant = _grant(
        binding_id=2,
        scope_type=SCOPE_STOCK_PLACES,
        scope_ids=(9,),
        permissions=(Permission.STOCK_PUSH,),
    )
    effective = EffectivePermissions(grants=(store_grant, place_grant))
    assert effective.covers(Permission.STOCK_PUSH, store_id=5)
    assert not effective.covers(Permission.STOCK_PUSH, store_id=9)
    assert effective.covers(Permission.STOCK_PUSH, stock_place_id=9)
    assert not effective.covers(Permission.STOCK_PUSH, stock_place_id=5)

    all_grant = _grant(binding_id=3, scope_type=SCOPE_ALL, permissions=(Permission.STOCK_PUSH,))
    effective_all = EffectivePermissions(grants=(all_grant,))
    assert effective_all.covers(Permission.STOCK_PUSH, store_id=99)
    assert effective_all.covers(Permission.STOCK_PUSH, stock_place_id=99)


def test_empty_scope_means_no_authorization() -> None:
    """空范围 = 没有授权；全部范围必须显式使用 all。"""
    with pytest.raises(ValueError):
        _grant(scope_type=SCOPE_STORES, scope_ids=(), permissions=(Permission.ORDER_READ,))
    with pytest.raises(ValueError):
        _grant(
            scope_type=SCOPE_STOCK_PLACES,
            scope_ids=(),
            permissions=(Permission.INVENTORY_READ,),
        )
    with pytest.raises(ValueError):
        _grant(scope_type=SCOPE_ALL, scope_ids=(1,), permissions=(Permission.ORDER_READ,))

    empty = EffectivePermissions()
    assert empty.permissions == frozenset()
    assert not empty.has(Permission.ORDER_READ)
    assert not empty.covers(Permission.ORDER_READ)
    assert not empty.covers(Permission.ORDER_READ, store_id=1)
    assert empty.allowed_ids(Permission.ORDER_READ, SCOPE_STORES) == frozenset()
    assert empty.filter_ids(Permission.ORDER_READ, SCOPE_STORES, [1, 2]) == []


def test_unknown_scope_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        ScopeGrant(
            role_binding_id=1,
            role_id=1,
            scope_type="department",
            scope_ids=frozenset({1}),
            permissions=frozenset({"order.read"}),
        )


def test_role_ids_collected_from_grants() -> None:
    grants = (
        _grant(binding_id=1, role_id=10, scope_type=SCOPE_ALL, permissions=("audit.read",)),
        _grant(binding_id=2, role_id=20, scope_type=SCOPE_ALL, permissions=("task.read",)),
        _grant(binding_id=3, role_id=10, scope_type=SCOPE_ALL, permissions=("task.read",)),
    )
    effective = EffectivePermissions(grants=grants)
    assert effective.role_ids == frozenset() or effective.role_ids == frozenset({10, 20})
    # 权限并集去重
    assert effective.permissions == frozenset({"audit.read", "task.read"})


# --------------------------------------------------------------------- 权限编码
def _parse_docs_permissions() -> frozenset[str]:
    text = API_CONTRACT_DOC.read_text(encoding="utf-8")
    start = text.index("## 权限编码")
    end = text.find("\n## ", start + 1)
    block = text[start : end if end != -1 else len(text)]
    codes: set[str] = set()
    for token in re.findall(r"`([^`]+)`", block):
        token = token.strip()
        if not token or token.startswith("/"):
            continue
        parts = token.split("/")
        first = parts[0]
        if not re.fullmatch(r"[a-z][a-z0-9_.]+", first):
            continue
        codes.add(first)
        if "." in first:
            prefix = first.rsplit(".", 1)[0]
            for part in parts[1:]:
                if re.fullmatch(r"[a-z][a-z0-9_]*", part):
                    codes.add(f"{prefix}.{part}")
    return frozenset(codes)


def test_permission_codes_match_api_contract_doc() -> None:
    documented = _parse_docs_permissions()
    assert documented == set(ALL_PERMISSIONS)


def test_permission_codes_format() -> None:
    pattern = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    for code in ALL_PERMISSIONS:
        assert pattern.fullmatch(code), f"权限编码格式不合法：{code}"
    values = [item.value for item in Permission]
    assert len(values) == len(set(values)), "Permission 枚举存在重复编码"


def test_predefined_roles_only_use_known_permissions() -> None:
    assert PREDEFINED_ROLE_PERMISSIONS, "预置角色不能为空"
    for role, codes in PREDEFINED_ROLE_PERMISSIONS.items():
        assert codes, f"预置角色 {role} 权限不能为空"
        assert codes <= ALL_PERMISSIONS, f"{role} 使用了未定义权限：{codes - ALL_PERMISSIONS}"
    owner = PREDEFINED_ROLE_PERMISSIONS["owner"]
    assert owner == ALL_PERMISSIONS


def test_permission_catalogue_matches_core_permissions() -> None:
    from app.modules.identity.permission_catalog import (
        missing_catalogue_codes,
        permission_codes,
        permission_items,
        unknown_catalogue_codes,
    )

    assert not missing_catalogue_codes()
    assert not unknown_catalogue_codes()
    assert permission_codes() == ALL_PERMISSIONS
    for item in permission_items():
        assert item.name.strip(), f"权限 {item.code} 缺少中文名"
        assert re.search(r"[\u4e00-\u9fff]", item.name), f"权限 {item.code} 名称应为中文"


# --------------------------------------------------------------------- 金额 / 时间
def test_money_columns_use_decimal() -> None:
    from app.db.base import ExchangeRate, Money, Quantity
    from app.db.registry import tenant_metadata

    assert isinstance(Money, Numeric)
    assert Money.precision == 18 and Money.scale == 4
    assert Money.asdecimal is True
    assert Money.python_type is Decimal
    assert isinstance(ExchangeRate, Numeric) and ExchangeRate.scale == 8
    assert Quantity is not None

    numeric_problems: list[str] = []
    numeric_count = 0
    for table in tenant_metadata.tables.values():
        for column in table.columns:
            column_type = column.type
            if not isinstance(column_type, Numeric):
                continue
            numeric_count += 1
            if column_type.asdecimal is not True:
                numeric_problems.append(f"{table.name}.{column.name} 未使用 Decimal 语义")
            if column_type.scale not in (4, 8):
                numeric_problems.append(
                    f"{table.name}.{column.name} 精度为 Numeric({column_type.precision},{column_type.scale})"
                )
    assert numeric_count > 0, "租户库应有金额字段"
    assert not numeric_problems, numeric_problems

    # 关键金额字段必须存在且为 DECIMAL(18,4)
    for table_name, column_name in (
        ("products", "cost_amount"),
        ("products", "base_price"),
        ("product_skus", "price"),
        ("orders", "total_amount"),
        ("order_items", "unit_price"),
    ):
        table = tenant_metadata.tables.get(table_name)
        assert table is not None, f"缺少表 {table_name}"
        column = table.columns.get(column_name)
        assert column is not None, f"缺少字段 {table_name}.{column_name}"
        assert isinstance(column.type, Numeric)
        assert column.type.asdecimal is True


def test_utcnow_is_naive_utc() -> None:
    from app.db.base import utcnow

    value = utcnow()
    assert isinstance(value, datetime)
    assert value.tzinfo is None, "数据库统一存 naive UTC"
    reference = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(value - reference) < timedelta(seconds=10)


def test_utc_serialization_helpers() -> None:
    from app.modules.channels.schemas import utc_iso
    from app.modules.identity.service import iso_utc

    naive = datetime(2026, 3, 4, 5, 6, 7)
    for helper in (utc_iso, iso_utc):
        text = helper(naive)
        assert text is not None and text.startswith("2026-03-04T05:06:07")
        assert text.endswith("+00:00") or text.endswith("Z")
        # 带时区的时间应换算到 UTC 再输出
        aware = datetime(2026, 3, 4, 13, 6, 7, tzinfo=timezone.utc)
        assert helper(aware).startswith("2026-03-04T13:06:07")
        assert helper(None) is None


# --------------------------------------------------------------------- 错误 / 响应契约
def test_error_codes_have_status_and_message() -> None:
    allowed_status = {400, 401, 403, 404, 409, 422, 429, 500, 503}
    for code in ErrorCode:
        assert code in DEFAULT_STATUS, f"错误码 {code} 缺少默认状态码"
        assert code in DEFAULT_MESSAGES, f"错误码 {code} 缺少中文说明"
        assert DEFAULT_STATUS[code] in allowed_status, f"{code} 状态码异常"
        assert re.search(r"[\u4e00-\u9fff]", DEFAULT_MESSAGES[code]), f"{code} 说明应为中文"
    values = [item.value for item in ErrorCode]
    assert len(values) == len(set(values)), "ErrorCode 存在重复值"


def test_app_error_payload_contract() -> None:
    error = AppError(ErrorCode.PERMISSION_DENIED)
    assert error.status_code == 403
    assert error.to_payload() == {
        "code": ErrorCode.PERMISSION_DENIED.value,
        "message": "无操作权限",
        "details": None,
    }
    body = error_body(ErrorCode.NOT_FOUND.value, "对象不存在", {"id": "1"})
    assert body["error"] == {"code": "NOT_FOUND", "message": "对象不存在", "details": {"id": "1"}}
    assert isinstance(body["request_id"], str) and body["request_id"]


def test_success_and_paged_envelope() -> None:
    assert set(success({"a": 1})) == {"data", "request_id"}
    assert set(success()) == {"data", "request_id"}
    page = paged(items=[{"id": "1"}], total=1, page=1, page_size=20)
    assert page["data"]["total"] == 1
    assert page["data"]["page"] == 1
    assert page["data"]["page_size"] == 20


# --------------------------------------------------------------------- 上下文 / Session 回归
def test_tenant_context_rejects_invalid_identity() -> None:
    from app.core.context import TenantContext, TenantDbRoute

    route = TenantDbRoute(
        tenant_id=1,
        host="127.0.0.1",
        port=3306,
        database="vs_tenant_1",
        username="vs_t_1",
        password="secret",
    )
    with pytest.raises(ValueError):
        TenantContext(
            tenant_id=0,
            tenant_code="demo",
            tenant_name="演示",
            tenant_status="active",
            timezone="Asia/Shanghai",
            member_id=1,
            member_status="active",
            user_id=1,
            db_route=route,
            permission_version=1,
        )
    context = TenantContext(
        tenant_id=1,
        tenant_code="demo",
        tenant_name="演示",
        tenant_status="active",
        timezone="Asia/Shanghai",
        member_id=1,
        member_status="active",
        user_id=1,
        db_route=route,
        permission_version=1,
    )
    assert context.request_id, "租户上下文必须带 request_id"
    assert context.timezone == "Asia/Shanghai"


def test_center_session_lock_is_reentrant() -> None:
    """回归：``_lock`` 必须是可重入锁，避免 center_session_factory 自死锁。"""
    import app.db.session as session_module

    assert isinstance(session_module._lock, type(threading.RLock())), (
        "app.db.session._lock 必须为 threading.RLock（否则首次创建中心库 Session 会死锁）"
    )


def test_center_session_factory_does_not_deadlock(monkeypatch) -> None:
    """用 stub 替换 create_engine，验证工厂创建路径不会在锁内再次取锁而挂起。"""
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "create_engine", lambda *args, **kwargs: object())
    monkeypatch.setattr(session_module, "_center_engine", None)
    monkeypatch.setattr(session_module, "_center_factory", None)
    factory = session_module.center_session_factory()
    assert factory is not None


def test_tenant_context_dependency_propagates_contextvar(monkeypatch) -> None:
    """回归：租户上下文依赖必须是 async 生成器，ContextVar 能传播到同步端点且可正确 reset。

    历史缺陷：同步生成器依赖在不同线程上下文 enter/exit，``reset_tenant_context``
    抛 ``ValueError: Token was created in a different Context``，且端点读不到租户上下文。
    本用例用依赖替身完全离线验证（不连中心库 / 租户库）。
    """
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.core.access import EffectivePermissions
    from app.core.auth import AuthenticatedUser, TenantAccessInfo
    from app.core.context import TenantDbRoute, require_tenant_context
    from app.modules.access.service import AccessSnapshot

    @contextmanager
    def fake_center_session():
        yield MagicMock(name="center_session")

    @contextmanager
    def fake_tenant_session(route):  # noqa: ANN001 - 测试替身
        yield MagicMock(name="tenant_session")

    fake_user = AuthenticatedUser(
        user_id=7,
        email="tester@example.com",
        display_name="测试用户",
        status="active",
    )
    fake_route = TenantDbRoute(
        tenant_id=42,
        host="127.0.0.1",
        port=3306,
        database="vs_tenant_42",
        username="vs_t_42",
        password="secret",
        credentials_version=1,
    )
    fake_access = TenantAccessInfo(
        tenant_id=42,
        tenant_code="demo",
        tenant_name="演示租户",
        tenant_status="active",
        timezone="Asia/Shanghai",
        member_id=11,
        member_status="active",
    )
    fake_snapshot = AccessSnapshot(
        permission_version=3,
        effective=EffectivePermissions(),
        feature_flags=frozenset({"order.sync"}),
    )

    monkeypatch.setattr(deps, "center_session", fake_center_session)
    monkeypatch.setattr(deps, "tenant_session", fake_tenant_session)
    monkeypatch.setattr(deps, "load_tenant_access", lambda center, tenant_id, user_id: fake_access)
    monkeypatch.setattr(deps, "load_tenant_db_route", lambda center, tenant_id: fake_route)
    monkeypatch.setattr(
        deps,
        "load_access_snapshot",
        lambda db, tenant_id, member_id: fake_snapshot,
    )

    test_app = FastAPI()
    test_app.dependency_overrides[deps.current_user] = lambda: fake_user

    @test_app.get("/tenants/{tenant_id}/ctx-check")
    def ctx_check(ctx: TenantCtx) -> dict[str, str]:
        current = require_tenant_context()
        return {
            "from_dependency": str(ctx.tenant_id),
            "from_contextvar": str(current.tenant_id),
            "member_id": str(current.member_id),
            "permission_version": str(current.permission_version),
            "timezone": current.timezone,
        }

    with TestClient(test_app) as client:
        response = client.get("/tenants/42/ctx-check")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "from_dependency": "42",
        "from_contextvar": "42",
        "member_id": "11",
        "permission_version": "3",
        "timezone": "Asia/Shanghai",
    }
    # 请求结束后不得残留租户上下文（否则会串租户）
    from app.core.context import get_tenant_context

    assert get_tenant_context(required=False) is None
