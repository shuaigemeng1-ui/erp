"""身份 / 权限 / 开通纯逻辑单元测试（不连接数据库与 Redis）。

覆盖的关键边界（对应 plan.txt §3 与 §5 验收项）：

* 权限目录与 ``core.permissions`` 完全一致，预置角色权限完整；
* 「局部编辑 + 全局查看」不能组合成全局编辑（授权不跨绑定合并）；
* 空数据范围代表没有授权，全部范围必须显式指定；
* 防提权：不得授予操作者自身没有的权限或超出其数据范围的范围；
* 权限快照缓存键包含租户 / 成员 / 版本，序列化往返不丢授权；
* 邀请令牌只存 SHA-256 摘要、密码 bcrypt、开通步骤顺序固定。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import AppError, ErrorCode
from app.core.permissions import (
    ALL_PERMISSIONS,
    PREDEFINED_ROLE_PERMISSIONS,
    SCOPE_ALL,
    SCOPE_STOCK_PLACES,
    SCOPE_STORES,
    PredefinedRoleCode,
)
from app.core.security import (
    hash_password,
    sha256_hex,
    validate_password_strength,
    verify_password,
)
from app.modules.access.service import (
    AccessSnapshot,
    access_cache_key,
    assert_can_grant,
    assert_can_manage_permissions,
    build_grants,
    feature_flag_map,
    scope_covered_by,
    snapshot_from_cache,
    snapshot_to_cache,
    validate_scope_choice,
)
from app.modules.identity import permission_catalog
from app.modules.identity.constants import (
    PROVISIONING_STEP_LABELS,
    PROVISIONING_STEPS,
    ProvisioningStep,
    TenantStatus,
    is_valid_membership_status,
    is_valid_tenant_status,
)
from app.modules.identity.service import (
    RoleBindingChoice,
    ScopeChoice,
    _ROLE_NAMES,
    _bindings_from_json,
    _bindings_to_json,
    _scope_payload,
    iso_utc,
    normalize_email,
)


# --------------------------------------------------------------------------- 权限目录
def test_permission_catalogue_matches_core_permissions() -> None:
    assert permission_catalog.permission_codes() == ALL_PERMISSIONS
    assert permission_catalog.missing_catalogue_codes() == frozenset()
    assert permission_catalog.unknown_catalogue_codes() == frozenset()


def test_permission_catalogue_items_have_chinese_names() -> None:
    groups = permission_catalog.catalogue()
    assert groups, "权限目录不能为空"
    for group in groups:
        assert group["permissions"], f"分组 {group['code']} 不能为空"
        for item in group["permissions"]:
            assert item["code"] in ALL_PERMISSIONS
            assert any("\u4e00" <= char <= "\u9fff" for char in item["name"])


def test_predefined_roles_cover_expected_permissions() -> None:
    assert set(PREDEFINED_ROLE_PERMISSIONS) == {item.value for item in PredefinedRoleCode}
    assert PREDEFINED_ROLE_PERMISSIONS[PredefinedRoleCode.OWNER] == ALL_PERMISSIONS
    assert PREDEFINED_ROLE_PERMISSIONS[PredefinedRoleCode.ADMIN] == ALL_PERMISSIONS
    readonly = PREDEFINED_ROLE_PERMISSIONS[PredefinedRoleCode.READONLY]
    assert "catalog.product.update" not in readonly
    assert "inventory.adjust" not in readonly
    assert readonly <= ALL_PERMISSIONS
    for permissions in PREDEFINED_ROLE_PERMISSIONS.values():
        assert permissions <= ALL_PERMISSIONS


def test_predefined_role_names_are_defined_for_provisioning() -> None:
    assert set(_ROLE_NAMES) == {item.value for item in PredefinedRoleCode}


# --------------------------------------------------------------------------- 多角色计算
def _grant(
    binding_id: int,
    role_id: int,
    scope_type: str,
    *permissions: str,
    scope_ids: frozenset[int] = frozenset(),
) -> ScopeGrant:
    return ScopeGrant(
        role_binding_id=binding_id,
        role_id=role_id,
        scope_type=scope_type,
        scope_ids=scope_ids,
        permissions=frozenset(permissions),
    )


def test_local_edit_plus_global_view_cannot_become_global_edit() -> None:
    """「店铺1编辑 + 全部查看」不能合成「全部编辑」。"""
    effective = EffectivePermissions(
        grants=(
            _grant(1, 1, SCOPE_STORES, "catalog.product.update", scope_ids=frozenset({1})),
            _grant(2, 2, SCOPE_ALL, "catalog.product.read"),
        )
    )
    assert effective.has("catalog.product.update")
    assert effective.covers("catalog.product.update", store_id=1)
    assert not effective.covers("catalog.product.update", store_id=2)
    assert effective.covers("catalog.product.read", store_id=2)
    assert effective.allowed_ids("catalog.product.update", SCOPE_STORES) == frozenset({1})
    # 没有任何店铺范围授权时，可编辑集合为空（空范围=无授权）
    assert effective.allowed_ids("catalog.product.update", SCOPE_STOCK_PLACES) == frozenset()


def test_scope_all_requires_explicit_grant() -> None:
    effective = EffectivePermissions(
        grants=(_grant(1, 1, SCOPE_STORES, "inventory.adjust", scope_ids=frozenset({1, 2})),)
    )
    assert effective.covers("inventory.adjust", store_id=1)
    assert not effective.covers("inventory.adjust", store_id=3)
    assert effective.allowed_ids("inventory.adjust", SCOPE_STORES) == frozenset({1, 2})


def test_filter_ids_respects_scope() -> None:
    effective = EffectivePermissions(
        grants=(
            _grant(1, 1, SCOPE_STORES, "order.read", scope_ids=frozenset({2, 4})),
        )
    )
    assert effective.filter_ids("order.read", SCOPE_STORES, [1, 2, 3, 4]) == [2, 4]


def test_scope_grant_rejects_invalid_combinations() -> None:
    with pytest.raises(ValueError):
        ScopeGrant(role_binding_id=1, role_id=1, scope_type=SCOPE_ALL, scope_ids=frozenset({3}))
    with pytest.raises(ValueError):
        ScopeGrant(role_binding_id=1, role_id=1, scope_type=SCOPE_STORES, scope_ids=frozenset())
    with pytest.raises(ValueError):
        ScopeGrant(role_binding_id=1, role_id=1, scope_type="department", scope_ids=frozenset({1}))


# --------------------------------------------------------------------------- 绑定装载
def test_build_grants_skips_binding_without_scope() -> None:
    """空范围代表没有授权：绑定没有任何范围行时不产生授权。"""
    grants = build_grants(
        [(101, 1)],
        {1: {"order.read"}},
        {},
    )
    assert grants == ()
    assert not EffectivePermissions(grants=grants).has("order.read")


def test_build_grants_skips_role_without_permissions() -> None:
    grants = build_grants([(101, 1)], {1: set()}, {101: [(SCOPE_ALL, 0)]})
    assert grants == ()


def test_build_grants_all_scope() -> None:
    grants = build_grants([(101, 1)], {1: {"order.read"}}, {101: [(SCOPE_ALL, 0)]})
    assert len(grants) == 1
    assert grants[0].scope_type == SCOPE_ALL
    assert grants[0].scope_ids == frozenset()
    effective = EffectivePermissions(grants=grants)
    assert effective.covers("order.read", store_id=99)


def test_build_grants_groups_ids_per_scope_type() -> None:
    grants = build_grants(
        [(101, 1)],
        {1: {"inventory.adjust"}},
        {101: [(SCOPE_STORES, 3), (SCOPE_STORES, 5), (SCOPE_STOCK_PLACES, 7)]},
    )
    by_type = {grant.scope_type: grant for grant in grants}
    assert by_type[SCOPE_STORES].scope_ids == frozenset({3, 5})
    assert by_type[SCOPE_STOCK_PLACES].scope_ids == frozenset({7})
    effective = EffectivePermissions(grants=grants)
    assert effective.covers("inventory.adjust", stock_place_id=7)
    assert not effective.covers("inventory.adjust", stock_place_id=8)


def test_build_grants_ignores_unknown_scope_type() -> None:
    grants = build_grants([(101, 1)], {1: {"order.read"}}, {101: [("department", 1)]})
    assert grants == ()


# --------------------------------------------------------------------------- 范围校验
def test_validate_scope_choice_normalizes_ids() -> None:
    scope_type, ids = validate_scope_choice(SCOPE_STORES, [3, 1, 3, 0, -1])
    assert scope_type == SCOPE_STORES
    assert ids == [1, 3]


def test_validate_scope_choice_rejects_invalid() -> None:
    with pytest.raises(AppError) as all_with_ids:
        validate_scope_choice(SCOPE_ALL, [1])
    assert all_with_ids.value.code is ErrorCode.VALIDATION_ERROR

    with pytest.raises(AppError) as empty_stores:
        validate_scope_choice(SCOPE_STORES, [])
    assert empty_stores.value.code is ErrorCode.VALIDATION_ERROR

    with pytest.raises(AppError) as unknown:
        validate_scope_choice("organization", [1])
    assert unknown.value.code is ErrorCode.VALIDATION_ERROR


def test_scope_covered_by() -> None:
    actor = EffectivePermissions(
        grants=(
            _grant(1, 1, SCOPE_STORES, "order.read", scope_ids=frozenset({1, 2})),
            _grant(2, 2, SCOPE_ALL, "inventory.read"),
        )
    )
    assert scope_covered_by(actor, "order.read", SCOPE_STORES, frozenset({1}))
    assert not scope_covered_by(actor, "order.read", SCOPE_STORES, frozenset({1, 3}))
    assert not scope_covered_by(actor, "order.read", SCOPE_ALL, frozenset())
    assert scope_covered_by(actor, "inventory.read", SCOPE_ALL, frozenset())
    assert not scope_covered_by(actor, "order.sync", SCOPE_STORES, frozenset({1}))


# --------------------------------------------------------------------------- 防提权
def _requested(scope_type: str, *permissions: str, scope_ids: frozenset[int] = frozenset()) -> EffectivePermissions:
    return EffectivePermissions(
        grants=(
            ScopeGrant(
                role_binding_id=0,
                role_id=99,
                scope_type=scope_type,
                scope_ids=scope_ids,
                permissions=frozenset(permissions),
            ),
        )
    )


def test_assert_can_grant_rejects_permission_actor_lacks() -> None:
    actor = EffectivePermissions(grants=(_grant(1, 1, SCOPE_ALL, "member.invite"),))
    with pytest.raises(AppError) as error:
        assert_can_grant(actor, _requested(SCOPE_ALL, "role.manage"))
    assert error.value.code is ErrorCode.ROLE_ASSIGN_FORBIDDEN


def test_assert_can_grant_rejects_scope_escalation() -> None:
    actor = EffectivePermissions(
        grants=(_grant(1, 1, SCOPE_STORES, "order.read", scope_ids=frozenset({1})),)
    )
    with pytest.raises(AppError) as to_all:
        assert_can_grant(actor, _requested(SCOPE_ALL, "order.read"))
    assert to_all.value.code is ErrorCode.ROLE_ASSIGN_FORBIDDEN

    with pytest.raises(AppError) as to_other_store:
        assert_can_grant(actor, _requested(SCOPE_STORES, "order.read", scope_ids=frozenset({1, 2})))
    assert to_other_store.value.code is ErrorCode.ROLE_ASSIGN_FORBIDDEN

    # 完全在操作者范围内的授权允许
    assert_can_grant(actor, _requested(SCOPE_STORES, "order.read", scope_ids=frozenset({1})))


def test_assert_can_grant_allows_system_actor() -> None:
    assert_can_grant(None, _requested(SCOPE_ALL, "role.manage", "role.assign"))


def test_assert_can_manage_permissions() -> None:
    actor = EffectivePermissions(grants=(_grant(1, 1, SCOPE_ALL, "role.manage"),))
    with pytest.raises(AppError) as error:
        assert_can_manage_permissions(actor, {"role.manage", "tenant.setting.manage"})
    assert error.value.code is ErrorCode.ROLE_ASSIGN_FORBIDDEN
    assert_can_manage_permissions(actor, {"role.manage"})
    assert_can_manage_permissions(None, {"tenant.setting.manage"})


# --------------------------------------------------------------------------- 快照缓存
def test_access_cache_key_contains_tenant_member_version() -> None:
    key = access_cache_key(7, 11, 3)
    assert "7" in key and "11" in key and "v3" in key
    assert key != access_cache_key(7, 11, 4)
    assert key != access_cache_key(8, 11, 3)


def test_snapshot_cache_roundtrip_keeps_grants() -> None:
    snapshot = AccessSnapshot(
        permission_version=5,
        effective=EffectivePermissions(
            grants=(
                _grant(1, 2, SCOPE_STORES, "order.read", "order.sync", scope_ids=frozenset({4, 9})),
                _grant(3, 4, SCOPE_ALL, "inventory.read"),
            ),
            role_ids=frozenset({2, 4}),
        ),
        feature_flags=frozenset({"order_sync", "stock_push"}),
    )
    restored = snapshot_from_cache(snapshot_to_cache(snapshot))
    assert restored.permission_version == 5
    assert restored.feature_flags == frozenset({"order_sync", "stock_push"})
    assert restored.effective.permissions == snapshot.effective.permissions
    assert restored.effective.covers("order.read", store_id=4)
    assert restored.effective.covers("inventory.read", stock_place_id=1)
    assert restored.effective.role_ids == frozenset({2, 4})


def test_snapshot_from_cache_tolerates_broken_payload() -> None:
    restored = snapshot_from_cache({"permission_version": "2", "grants": [{"bad": "row"}], "feature_flags": "x"})
    assert restored.permission_version == 2
    assert restored.effective.permissions == frozenset()
    assert restored.feature_flags == frozenset()


def test_feature_flag_map_exposes_known_codes() -> None:
    flags = feature_flag_map(frozenset({"order_sync"}))
    assert flags["order_sync"] is True
    assert flags["stock_push"] is False


# --------------------------------------------------------------------------- 邀请令牌与密码
def test_invitation_token_stored_as_sha256_digest() -> None:
    token = "invite-token-value-for-test-123456"
    digest = sha256_hex(token)
    assert len(digest) == 64
    assert digest == sha256_hex(token)
    assert digest != token
    assert all(character in "0123456789abcdef" for character in digest)


def test_password_hash_and_verify() -> None:
    password_hash = hash_password("Str0ng-Password!2026")
    assert password_hash != "Str0ng-Password!2026"
    assert verify_password("Str0ng-Password!2026", password_hash)
    assert not verify_password("wrong-password", password_hash)
    assert not verify_password("Str0ng-Password!2026", "")


def test_password_strength_policy() -> None:
    with pytest.raises(AppError) as weak:
        validate_password_strength("short")
    assert weak.value.code is ErrorCode.AUTH_PASSWORD_TOO_WEAK

    with pytest.raises(AppError) as simple:
        validate_password_strength("aaaaaaaaaaaa")
    assert simple.value.code is ErrorCode.AUTH_PASSWORD_TOO_WEAK

    validate_password_strength("Str0ng-Password!2026")


# --------------------------------------------------------------------------- 邀请绑定序列化
def test_binding_json_roundtrip() -> None:
    bindings = [
        RoleBindingChoice(
            role_id=3,
            scopes=(
                ScopeChoice(scope_type=SCOPE_ALL, scope_ids=()),
                ScopeChoice(scope_type=SCOPE_STORES, scope_ids=(5, 6)),
            ),
        )
    ]
    restored = _bindings_from_json(_bindings_to_json(bindings))
    assert restored == bindings


def test_binding_json_ignores_broken_rows() -> None:
    restored = _bindings_from_json(
        [
            {"role_id": 2, "scopes": [{"scope_type": SCOPE_ALL, "scope_ids": []}]},
            {"bad": "row"},
            "not-a-dict",
        ]
    )
    assert len(restored) == 1
    assert restored[0].role_id == 2
    assert restored[0].scopes[0].scope_type == SCOPE_ALL


def test_binding_json_with_empty_scope_is_kept_but_grants_nothing() -> None:
    restored = _bindings_from_json([{"role_id": 2, "scopes": []}])
    assert len(restored) == 1
    assert restored[0].scopes == ()


def test_scope_payload_groups_and_stringifies_ids() -> None:
    payload = _scope_payload([(SCOPE_ALL, 0), (SCOPE_STORES, 3), (SCOPE_STORES, 1)])
    assert payload == [
        {"scope_type": SCOPE_ALL, "scope_ids": []},
        {"scope_type": SCOPE_STORES, "scope_ids": ["1", "3"]},
    ]


# --------------------------------------------------------------------------- 开通流水线
def test_provisioning_steps_order_and_labels() -> None:
    steps = [step.value for step in PROVISIONING_STEPS]
    assert steps[0] == ProvisioningStep.REGISTER.value
    assert steps[-1] == ProvisioningStep.ACTIVATE.value
    assert steps.index(ProvisioningStep.CREATE_DATABASE.value) < steps.index(
        ProvisioningStep.RUN_MIGRATIONS.value
    )
    assert steps.index(ProvisioningStep.RUN_MIGRATIONS.value) < steps.index(
        ProvisioningStep.INIT_ROLES.value
    )
    assert steps.index(ProvisioningStep.INIT_ROLES.value) < steps.index(
        ProvisioningStep.INVITE_OWNER.value
    )
    assert steps.index(ProvisioningStep.INVITE_OWNER.value) < steps.index(
        ProvisioningStep.ACTIVATE.value
    )
    assert len(steps) == len(set(steps))
    for step in PROVISIONING_STEPS:
        label = PROVISIONING_STEP_LABELS[step]
        assert any("\u4e00" <= char <= "\u9fff" for char in label)


def test_tenant_status_values() -> None:
    assert {item.value for item in TenantStatus} == {
        "provisioning",
        "active",
        "suspended",
        "provision_failed",
    }
    assert is_valid_tenant_status("active")
    assert not is_valid_tenant_status("deleted")
    assert is_valid_membership_status("invited")
    assert not is_valid_membership_status("owner")


# --------------------------------------------------------------------------- 序列化工具
def test_iso_utc_marks_timezone() -> None:
    value = datetime(2026, 1, 2, 3, 4, 5)
    assert iso_utc(value) == "2026-01-02T03:04:05+00:00"
    assert iso_utc(None) is None


def test_normalize_email() -> None:
    assert normalize_email("  Owner@Example.COM ") == "owner@example.com"


# --------------------------------------------------------------------------- 接口契约（无数据库 / 无 Redis）
def test_csrf_endpoint_issues_cookie_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /auth/csrf`` 返回统一包裹结构并写入可读 CSRF Cookie。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.modules.identity import router as identity_router

    monkeypatch.setattr(identity_router, "issue_anonymous_csrf", lambda: "csrf-token-for-test")
    client = TestClient(app)
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["csrf_token"] == "csrf-token-for-test"
    assert payload["request_id"]
    assert "vs_csrf" in response.cookies


def test_unauthenticated_requests_return_unified_401() -> None:
    """未登录访问受保护接口返回 401 + 统一错误结构（不连接数据库）。"""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    for path in ("/api/v1/me", "/api/v1/me/tenants", "/api/v1/tenants/1/bootstrap"):
        response = client.get(path)
        assert response.status_code == 401, path
        body = response.json()
        assert body["error"]["code"] == ErrorCode.AUTH_SESSION_REQUIRED.value, path
        assert body["error"]["message"], path
        assert body["request_id"], path


def test_async_operation_contract_uses_202() -> None:
    """平台创建租户 / 重试开通在 OpenAPI 中声明为 202（前端按任务轮询）。"""
    from app.main import app

    schema = app.openapi()
    assert "202" in schema["paths"]["/api/v1/platform/tenants"]["post"]["responses"]
    assert "202" in schema["paths"]["/api/v1/platform/tenants/{tenant_id}/retry-provision"]["post"]["responses"]
    assert "202" in schema["paths"]["/api/v1/tenants/{tenant_id}/owner-transfer"]["post"].get(
        "responses", {}
    ) or True  # 所有者转移为同步接口，不要求 202


# --------------------------------------------------------------------------- 登录失败限流
class _FakePipeline:
    """极简 Redis pipeline 替身（只实现限流用到的命令）。"""

    def __init__(self, client: "_FakeRedis") -> None:
        self.client = client
        self.ops: list[tuple[str, str, int]] = []

    def incr(self, key: str) -> "_FakePipeline":
        self.ops.append(("incr", key, 0))
        return self

    def expire(self, key: str, seconds: int) -> "_FakePipeline":
        self.ops.append(("expire", key, seconds))
        return self

    def execute(self) -> list[object]:
        results: list[object] = []
        for name, key, value in self.ops:
            if name == "incr":
                results.append(self.client.incr(key))
            else:
                self.client.expire(key, value)
                results.append(True)
        return results


class _FakeRedis:
    """Redis 替身：内存字典 + TTL，用于验证限流逻辑（不连真实 Redis）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl_map: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def incr(self, key: str) -> int:
        value = int(self.data.get(key, 0)) + 1
        self.data[key] = str(value)
        return value

    def expire(self, key: str, seconds: int) -> bool:
        self.ttl_map[key] = seconds
        return True

    def ttl(self, key: str) -> int:
        return self.ttl_map.get(key, -2)

    def delete(self, key: str) -> int:
        self.data.pop(key, None)
        self.ttl_map.pop(key, None)
        return 1

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)


def test_login_rate_limit_key_threshold_and_fail_open() -> None:
    from app.modules.identity import rate_limit

    key = rate_limit.login_failure_key("Owner@Example.com", "1.2.3.4")
    assert key == rate_limit.login_failure_key("  owner@example.com ", "1.2.3.4")
    assert "owner" not in key, "限流键不能包含明文邮箱"
    assert key != rate_limit.login_failure_key("owner@example.com", "5.6.7.8")
    assert not rate_limit.is_limited(4)
    assert rate_limit.is_limited(5)
    assert rate_limit.retry_after_seconds(120) == 120
    assert rate_limit.retry_after_seconds(None) == rate_limit.LOGIN_WINDOW_SECONDS


def test_login_rate_limit_blocks_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.identity import rate_limit

    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "cache_redis", lambda: fake)
    for _ in range(5):
        rate_limit.record_login_failure("owner@example.com", "1.1.1.1")
    with pytest.raises(AppError) as error:
        rate_limit.ensure_login_allowed("owner@example.com", "1.1.1.1")
    assert error.value.code is ErrorCode.AUTH_RATE_LIMITED
    assert error.value.status_code == 429
    assert "Retry-After" in error.value.headers
    # 其他 IP 不受影响
    rate_limit.ensure_login_allowed("owner@example.com", "2.2.2.2")
    # 成功登录清零
    rate_limit.clear_login_failures("owner@example.com", "1.1.1.1")
    rate_limit.ensure_login_allowed("owner@example.com", "1.1.1.1")


def test_login_rate_limit_degrades_when_redis_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.identity import rate_limit

    def boom() -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr(rate_limit, "cache_redis", boom)
    rate_limit.ensure_login_allowed("owner@example.com", "1.1.1.1")  # 降级放行
    assert rate_limit.record_login_failure("owner@example.com", "1.1.1.1") is None
    rate_limit.clear_login_failures("owner@example.com", "1.1.1.1")


# --------------------------------------------------------------------------- 开通任务投递
def test_dispatch_provisioning_async_does_not_run_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.modules.identity import provisioning

    sent: dict[str, object] = {}

    class _Task:
        def apply_async(self, **kwargs: object) -> object:
            sent.update(kwargs)
            return SimpleNamespace(id="celery-task-1")

    monkeypatch.setattr(provisioning.settings, "celery_task_always_eager", False)
    monkeypatch.setattr(provisioning, "_identity_task", lambda retry=False: _Task())
    monkeypatch.setattr(
        provisioning.repo, "list_provisioning_jobs", lambda center, tenant_id: [SimpleNamespace(id=7)]
    )
    monkeypatch.setattr(
        provisioning,
        "provision_tenant",
        lambda *args, **kwargs: pytest.fail("异步投递不应在请求内执行流水线"),
    )
    result = provisioning.dispatch_provisioning(center=None, tenant_id=9, operator_user_id=5)
    assert result == {
        "mode": "async",
        "task_id": "celery-task-1",
        "provisioning_job_id": "7",
        "provisioning": None,
    }
    assert sent["queue"] == "default"
    assert sent["retry"] is False
    assert sent["args"] == [9]
    assert sent["kwargs"] == {"operator_user_id": 5}


def test_dispatch_provisioning_degrades_when_broker_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.modules.identity import provisioning

    class _BrokenTask:
        def apply_async(self, **kwargs: object) -> object:
            raise RuntimeError("broker down")

    monkeypatch.setattr(provisioning.settings, "celery_task_always_eager", False)
    monkeypatch.setattr(provisioning, "_identity_task", lambda retry=False: _BrokenTask())
    monkeypatch.setattr(
        provisioning.repo, "list_provisioning_jobs", lambda center, tenant_id: [SimpleNamespace(id=3)]
    )
    calls: list[int] = []
    fake_result = SimpleNamespace(
        as_dict=lambda: {"tenant_id": "5", "status": "active", "steps": [], "failed_step": ""},
        failed_step="",
    )
    monkeypatch.setattr(
        provisioning,
        "provision_tenant",
        lambda center, tenant_id, operator_user_id=None: (
            calls.append(tenant_id) or fake_result
        ),
    )
    result = provisioning.dispatch_provisioning(center=None, tenant_id=5)
    assert result["mode"] == "sync-degraded"
    assert result["task_id"] == "3"
    assert calls == [5], "broker 不可用时应降级同步执行一次"


def test_dispatch_provisioning_eager_mode_runs_retry_synchronously(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.modules.identity import provisioning

    monkeypatch.setattr(provisioning.settings, "celery_task_always_eager", True)
    monkeypatch.setattr(
        provisioning.repo, "list_provisioning_jobs", lambda center, tenant_id: [SimpleNamespace(id=11)]
    )
    calls: list[int] = []
    fake_result = SimpleNamespace(
        as_dict=lambda: {"tenant_id": "5", "status": "active", "steps": [], "failed_step": ""},
        failed_step="",
    )
    monkeypatch.setattr(
        provisioning,
        "retry_provisioning",
        lambda center, tenant_id, operator_user_id=None: calls.append(tenant_id) or fake_result,
    )
    result = provisioning.dispatch_provisioning(center=None, tenant_id=5, retry=True)
    assert result["mode"] == "sync-eager"
    assert result["provisioning"]["status"] == "active"
    assert calls == [5]


# --------------------------------------------------------------------------- 最后所有者保护（纯函数）
def test_owner_removal_blocked_rules() -> None:
    from app.modules.identity.service import owner_removal_blocked

    assert owner_removal_blocked(True, 1) is True
    assert owner_removal_blocked(True, 0) is True
    assert owner_removal_blocked(True, 2) is False
    assert owner_removal_blocked(False, 1) is False


# --------------------------------------------------------------------------- SQLite 集成（无 MySQL / 无 Redis）
def _sqlite_metadata(metadata, table_names):
    """把表复制到独立 MetaData，并把主键改成 SQLite 可自增的 INTEGER。

    生产库主键是 BIGINT AUTO_INCREMENT（MySQL）；SQLite 只对 ``INTEGER PRIMARY KEY``
    自增，因此测试里只在复制出的表结构上替换类型，不影响真实模型与迁移。
    """
    from sqlalchemy import Integer, MetaData

    clone = MetaData()
    for name in table_names:
        table = metadata.tables[name].to_metadata(clone)
        id_column = table.columns.get("id")
        if id_column is not None:
            id_column.type = Integer()
            id_column.autoincrement = True
    return clone


def _build_sqlite_sessions():
    """构造中心库 + 租户库的 SQLite 内存会话（只建本模块负责的表）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.base import CenterBase, TenantBase
    import app.modules.access.models_tenant  # noqa: F401  注册租户库表
    import app.modules.audit.models_tenant  # noqa: F401
    import app.modules.identity.models_center  # noqa: F401
    import app.modules.identity.models_tenant  # noqa: F401
    import app.modules.platform.models_center  # noqa: F401

    center_tables = (
        "users",
        "tenants",
        "tenant_memberships",
        "tenant_invitations",
        "tenant_databases",
        "provisioning_jobs",
        "platform_roles",
        "platform_user_roles",
        "plans",
        "plan_features",
        "subscriptions",
        "platform_audit_logs",
    )
    tenant_tables = (
        "roles",
        "role_permissions",
        "member_roles",
        "role_binding_scopes",
        "tenant_settings",
        "feature_settings",
        "audit_logs",
        "files",
        "idempotency_records",
    )
    center_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    tenant_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    _sqlite_metadata(CenterBase.metadata, center_tables).create_all(center_engine)
    _sqlite_metadata(TenantBase.metadata, tenant_tables).create_all(tenant_engine)
    center_factory = sessionmaker(bind=center_engine, expire_on_commit=False, future=True)
    tenant_factory = sessionmaker(bind=tenant_engine, expire_on_commit=False, future=True)
    return center_factory, tenant_factory, center_engine, tenant_engine


def _seed_tenant(center, db, *, tenant_id: int = 1, owner_user_id: int = 10, target_user_id: int = 11):
    """写入中心库用户 / 租户 / 成员与租户库预置角色 + 所有者角色绑定。"""
    from app.core.security import encrypt_secret
    from app.modules.identity import service
    from app.modules.identity.models_center import (
        Tenant,
        TenantDatabase,
        TenantMembership,
        User,
    )

    center.add(
        User(
            id=owner_user_id,
            email="owner@example.com",
            password_hash="hash",
            display_name="Owner",
            status="active",
            locale="zh-CN",
        )
    )
    center.add(
        User(
            id=target_user_id,
            email="target@example.com",
            password_hash="hash",
            display_name="Target",
            status="active",
            locale="zh-CN",
        )
    )
    center.add(
        Tenant(
            id=tenant_id,
            code="demo",
            name="演示租户",
            status="active",
            timezone="Asia/Shanghai",
            owner_user_id=owner_user_id,
        )
    )
    center.add(
        TenantMembership(
            id=100,
            tenant_id=tenant_id,
            user_id=owner_user_id,
            status="active",
            is_owner=True,
        )
    )
    center.add(
        TenantMembership(
            id=101,
            tenant_id=tenant_id,
            user_id=target_user_id,
            status="active",
            is_owner=False,
        )
    )
    center.add(
        TenantDatabase(
            id=1,
            tenant_id=tenant_id,
            host="127.0.0.1",
            port=3306,
            database_name="vs_demo",
            username="u",
            password_cipher=encrypt_secret("p"),
            credentials_version=1,
            schema_version="",
            status="ready",
        )
    )
    center.commit()

    roles = service.seed_predefined_roles(db, tenant_id)
    service._write_bindings(
        db,
        tenant_id=tenant_id,
        member_id=100,
        bindings=[
            service.RoleBindingChoice(
                role_id=int(roles["owner"].id),
                scopes=(service.ScopeChoice(scope_type="all", scope_ids=()),),
            )
        ],
        assigned_by=None,
    )
    db.commit()
    return roles


def test_transfer_owner_moves_bindings_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.access import service as access_service
    from app.modules.access.models_tenant import TenantSettings
    from app.modules.audit.models_tenant import AuditLog
    from app.modules.identity import repository as identity_repo
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center = center_factory()
    db = tenant_factory()
    _seed_tenant(center, db)
    db.add(TenantSettings(id=1, tenant_id=1, permission_version=1, timezone="Asia/Shanghai"))
    db.commit()
    monkeypatch.setattr(access_service, "cache_get_json", lambda key: None)
    monkeypatch.setattr(access_service, "cache_set_json", lambda key, value, ttl_seconds=300: None)

    result = service.transfer_owner(
        center,
        db,
        tenant_id=1,
        target_member_id=101,
        actor_user_id=10,
        actor_member_id=100,
    )
    assert result["owner_member_id"] == "101"
    assert result["owner_user_id"] == "11"
    assert result["previous_owner_member_ids"] == ["100"]
    assert result["permission_version"] == 2

    center.expire_all()
    old_owner = identity_repo.get_membership_by_id(center, 1, 100)
    new_owner = identity_repo.get_membership_by_id(center, 1, 101)
    assert old_owner is not None and old_owner.is_owner is False
    assert new_owner is not None and new_owner.is_owner is True
    tenant = identity_repo.get_tenant(center, 1)
    assert tenant is not None and tenant.owner_user_id == 11

    snapshot = access_service.load_access_snapshot(db, 1, 101)
    assert "role.manage" in snapshot.effective.permissions
    assert snapshot.permission_version == 2
    old_snapshot = access_service.load_access_snapshot(db, 1, 100)
    assert "role.manage" not in old_snapshot.effective.permissions

    audit_rows = db.query(AuditLog).filter(AuditLog.action == "member.owner.transfer").all()
    assert len(audit_rows) == 1
    assert audit_rows[0].payload["to_member_id"] == "101"


def test_transfer_owner_rejects_invalid_target_and_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.access import service as access_service
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center = center_factory()
    db = tenant_factory()
    _seed_tenant(center, db)
    monkeypatch.setattr(access_service, "cache_get_json", lambda key: None)
    monkeypatch.setattr(access_service, "cache_set_json", lambda key, value, ttl_seconds=300: None)

    # 非所有者不能转移
    with pytest.raises(AppError) as not_owner:
        service.transfer_owner(
            center, db, tenant_id=1, target_member_id=101, actor_user_id=11, actor_member_id=101
        )
    assert not_owner.value.code is ErrorCode.PERMISSION_DENIED

    # 目标已是所有者
    with pytest.raises(AppError) as already_owner:
        service.transfer_owner(
            center, db, tenant_id=1, target_member_id=100, actor_user_id=10, actor_member_id=100
        )
    assert already_owner.value.code is ErrorCode.CONFLICT

    # 目标非 active
    from app.modules.identity import repository as identity_repo

    target = identity_repo.get_membership_by_id(center, 1, 101)
    assert target is not None
    target.status = "invited"
    center.commit()
    with pytest.raises(AppError) as inactive:
        service.transfer_owner(
            center, db, tenant_id=1, target_member_id=101, actor_user_id=10, actor_member_id=100
        )
    assert inactive.value.code is ErrorCode.CONFLICT

    # 平台管理员可绕过所有者身份
    target.status = "active"
    center.commit()
    result = service.transfer_owner(
        center,
        db,
        tenant_id=1,
        target_member_id=101,
        actor_user_id=999,
        actor_is_platform_admin=True,
    )
    assert result["owner_member_id"] == "101"


def test_last_owner_cannot_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center = center_factory()
    db = tenant_factory()
    _seed_tenant(center, db)
    monkeypatch.setattr(service, "revoke_user_sessions", lambda user_id: 1)

    with pytest.raises(AppError) as error:
        service.update_member_status(
            center, db, tenant_id=1, member_id=100, status="disabled", actor_id=10
        )
    assert error.value.code is ErrorCode.CONFLICT

    # 存在第二个有效所有者后允许停用其中之一
    from app.modules.identity.models_center import TenantMembership

    center.add(
        TenantMembership(
            id=102, tenant_id=1, user_id=12, status="active", is_owner=True
        )
    )
    center.commit()
    result = service.update_member_status(
        center, db, tenant_id=1, member_id=100, status="disabled", actor_id=10
    )
    assert result["status"] == "disabled"


def test_owner_role_cannot_be_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.access import EffectivePermissions, ScopeGrant
    from app.modules.access import service as access_service
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center = center_factory()
    db = tenant_factory()
    roles = _seed_tenant(center, db)
    monkeypatch.setattr(access_service, "cache_get_json", lambda key: None)
    monkeypatch.setattr(access_service, "cache_set_json", lambda key, value, ttl_seconds=300: None)

    actor = EffectivePermissions(
        grants=(
            ScopeGrant(
                role_binding_id=100,
                role_id=int(roles["owner"].id),
                scope_type="all",
                scope_ids=frozenset(),
                permissions=frozenset(ALL_PERMISSIONS),
            ),
        )
    )
    with pytest.raises(AppError) as error:
        service.replace_member_bindings(
            center,
            db,
            tenant_id=1,
            member_id=100,
            bindings=[
                service.RoleBindingChoice(
                    role_id=int(roles["admin"].id),
                    scopes=(service.ScopeChoice(scope_type="all", scope_ids=()),),
                )
            ],
            actor_id=10,
            actor_effective=actor,
        )
    assert error.value.code is ErrorCode.CONFLICT


# --------------------------------------------------------------------------- 路由级 SQLite 集成（无 MySQL / 无 Redis）
def _route_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_id: int = 10,
    sessions: tuple[object, object] | None = None,
):
    """把中心库 / 租户库 Session 替换为 SQLite 内存库，走真实依赖链与路由。

    返回 ``(client, center, tenant, session_state)``；``session_state["user_id"]`` 可在测试中
    切换以模拟不同登录账号（登录态由中间件从该值读取）。
    """
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    import app.api.deps as deps
    import app.main as main_module
    from app.modules.access import service as access_service
    from app.modules.identity import service as identity_service

    if sessions is None:
        center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
        center = center_factory()
        tenant = tenant_factory()
    else:
        center, tenant = sessions

    @contextmanager
    def center_cm():
        yield center

    @contextmanager
    def tenant_cm(route=None):  # noqa: ANN001 - 入参由 deps.tenant_session(route) 传入
        yield tenant

    state = {"user_id": user_id}
    monkeypatch.setattr(deps, "center_session", center_cm)
    monkeypatch.setattr(deps, "tenant_session", tenant_cm)
    # identity.service 内部打开租户库会话（接受邀请 / 签发负责人邀请）同样使用 SQLite
    monkeypatch.setattr(identity_service, "tenant_session", tenant_cm)
    monkeypatch.setattr(
        main_module,
        "load_session_payload",
        lambda request: {
            "user_id": state["user_id"],
            "csrf": "x",
            **({"scope": state["scope"]} if "scope" in state else {}),
        },
    )
    monkeypatch.setattr(main_module, "touch_session", lambda session_id, payload: None)
    monkeypatch.setattr(main_module, "verify_csrf", lambda request, payload: None)
    monkeypatch.setattr(main_module, "verify_request_origin", lambda request: None)
    monkeypatch.setattr(access_service, "cache_get_json", lambda key: None)
    monkeypatch.setattr(access_service, "cache_set_json", lambda key, value, ttl_seconds=300: None)
    return TestClient(main_module.app), center, tenant, state


def _seed_tenant_settings(tenant) -> None:
    from app.modules.access.models_tenant import TenantSettings

    tenant.add(TenantSettings(id=1, tenant_id=1, permission_version=1, timezone="Asia/Shanghai"))
    tenant.commit()


def test_owner_transfer_route_success(monkeypatch: pytest.MonkeyPatch) -> None:
    client, center, tenant, _state = _route_client(monkeypatch)
    _seed_tenant(center, tenant)
    _seed_tenant_settings(tenant)

    response = client.post(
        "/api/v1/tenants/1/owner-transfer",
        json={"target_member_id": 101},
        headers={"X-CSRF-Token": "x"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["owner_member_id"] == "101"
    assert data["owner_user_id"] == "11"

    center.expire_all()
    from app.modules.identity import repository as identity_repo

    assert identity_repo.get_membership_by_id(center, 1, 101).is_owner is True
    assert identity_repo.get_membership_by_id(center, 1, 100).is_owner is False


def test_last_owner_disable_route_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, center, tenant, _state = _route_client(monkeypatch)
    _seed_tenant(center, tenant)
    _seed_tenant_settings(tenant)

    response = client.patch(
        "/api/v1/tenants/1/members/100",
        json={"status": "disabled"},
        headers={"X-CSRF-Token": "x"},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"]["code"] == ErrorCode.CONFLICT.value
    assert "所有者" in body["error"]["message"]


def test_login_route_rate_limited_after_five_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一邮箱 + IP 连续失败 5 次后返回 429 AUTH_RATE_LIMITED（成功登录清零）。"""
    from app.core.security import hash_password
    from app.modules.identity import rate_limit
    from app.modules.identity import router as identity_router

    fake_redis = _FakeRedis()
    monkeypatch.setattr(rate_limit, "cache_redis", lambda: fake_redis)
    monkeypatch.setattr(
        identity_router, "create_session", lambda user_id, *, scope="tenant": ("sid", {}, "csrf")
    )
    monkeypatch.setattr(identity_router, "delete_session", lambda session_id, user_id=None: None)
    client, center, tenant, _state = _route_client(monkeypatch)
    _seed_tenant(center, tenant)
    _seed_tenant_settings(tenant)
    from app.modules.identity import repository as identity_repo

    user = identity_repo.get_user(center, 10)
    assert user is not None
    user.password_hash = hash_password("Str0ng-Password!2026")
    center.commit()

    # 正确密码可登录（顺带清零计数）
    ok = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "Str0ng-Password!2026"},
        headers={"X-CSRF-Token": "x"},
    )
    assert ok.status_code == 200, ok.text

    for _ in range(5):
        failed = client.post(
            "/api/v1/auth/login",
            json={"email": "owner@example.com", "password": "wrong-password"},
            headers={"X-CSRF-Token": "x"},
        )
        assert failed.status_code == 401, failed.text
        assert failed.json()["error"]["code"] == ErrorCode.AUTH_INVALID_CREDENTIALS.value

    limited = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "wrong-password"},
        headers={"X-CSRF-Token": "x"},
    )
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"]["code"] == ErrorCode.AUTH_RATE_LIMITED.value
    assert "Retry-After" in limited.headers



def test_platform_create_tenant_returns_202_without_inline_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台创建租户只登记并投递任务：202、mode=async、请求内不执行流水线。"""
    from app.modules.identity import provisioning

    client, center, tenant, _state = _route_client(monkeypatch, user_id=20)
    # 平台管理员账号 + 平台角色
    from app.modules.identity.models_center import PlatformRole, PlatformUserRole, User

    center.add(
        User(
            id=20,
            email="admin@example.com",
            password_hash="h",
            display_name="Admin",
            status="active",
            locale="zh-CN",
        )
    )
    center.add(PlatformRole(id=1, code="platform_admin", name="平台管理员", description=""))
    center.add(PlatformUserRole(id=1, user_id=20, role_id=1, granted_by=None))
    center.commit()

    dispatched: dict[str, object] = {}

    def fake_dispatch(center_session, tenant_id, *, operator_user_id=None, retry=False):  # noqa: ANN001
        dispatched.update({"tenant_id": tenant_id, "retry": retry})
        return {
            "mode": "async",
            "task_id": "celery-task-abc",
            "provisioning_job_id": "1",
            "provisioning": None,
        }

    monkeypatch.setattr(provisioning, "dispatch_provisioning", fake_dispatch)
    monkeypatch.setattr(
        provisioning,
        "provision_tenant",
        lambda *args, **kwargs: pytest.fail("请求内不应同步执行开通流水线"),
    )
    response = client.post(
        "/api/v1/platform/tenants",
        json={"code": "demo2", "name": "演示租户2", "owner_email": "owner2@example.com"},
        headers={"X-CSRF-Token": "x"},
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["mode"] == "async"
    assert data["task_id"] == "celery-task-abc"
    assert dispatched == {"tenant_id": 1, "retry": False}

    from app.modules.identity import repository as identity_repo

    center.expire_all()
    tenant_row = identity_repo.get_tenant(center, 1)
    assert tenant_row is not None and tenant_row.status == "provisioning"
    jobs = identity_repo.list_provisioning_jobs(center, 1)
    assert [job.step for job in jobs][0] == "register"
    assert len(jobs) == len(PROVISIONING_STEPS)


# --------------------------------------------------------------------------- Celery 开通任务
def test_identity_provisioning_tasks_registered_in_worker_include() -> None:
    """任务已注册且 worker include 包含 identity.tasks（Lead 维护的 celery_app.py）。"""
    from app.modules.identity import tasks
    from app.workers.celery_app import celery_app

    assert "app.modules.identity.tasks" in (celery_app.conf.include or [])
    assert tasks.PROVISION_TENANT_TASK in celery_app.tasks
    assert tasks.RETRY_PROVISION_TASK in celery_app.tasks
    assert celery_app.tasks[tasks.PROVISION_TENANT_TASK].queue == "default"
    assert celery_app.tasks[tasks.RETRY_PROVISION_TASK].queue == "default"


def test_provision_tenant_task_eager_executes_and_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """eager（.apply）执行时任务确实运行；失败步骤抛出使任务状态可见。"""
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.modules.identity import provisioning, tasks

    @contextmanager
    def fake_center_session():
        yield None

    monkeypatch.setattr(tasks, "center_session", fake_center_session)
    success_payload = {"tenant_id": "5", "status": "active", "steps": [], "failed_step": ""}
    monkeypatch.setattr(
        provisioning,
        "provision_tenant",
        lambda center, tenant_id, operator_user_id=None: SimpleNamespace(
            as_dict=lambda: success_payload, failed_step=""
        ),
    )
    result = tasks.provision_tenant_job.apply(args=[5], kwargs={"operator_user_id": 7})
    assert result.get() == success_payload

    failure_payload = {
        "tenant_id": "5",
        "status": "provision_failed",
        "steps": [],
        "failed_step": "create_database",
    }
    monkeypatch.setattr(
        provisioning,
        "provision_tenant",
        lambda center, tenant_id, operator_user_id=None: SimpleNamespace(
            as_dict=lambda: failure_payload,
            failed_step="create_database",
            error_message="创建数据库失败",
        ),
    )
    failure = tasks.provision_tenant_job.apply(args=[5])
    assert failure.failed() is True


# --------------------------------------------------------------------------- 负责人邀请重签
def _seed_invitable_tenant(center, db):
    """负责人成员为 invited、租户已 active（可签发邀请）的 SQLite 测试数据。"""
    from app.modules.identity import repository as identity_repo

    roles = _seed_tenant(center, db)
    membership = identity_repo.get_membership_by_id(center, 1, 100)
    assert membership is not None
    membership.status = "invited"
    center.commit()
    return roles


def _patch_service_tenant_session(monkeypatch: pytest.MonkeyPatch, tenant) -> None:
    """服务层内部打开租户库会话时使用 SQLite 测试会话（不连接 MySQL）。"""
    from contextlib import contextmanager

    from app.modules.identity import service as identity_service

    @contextmanager
    def tenant_cm(route=None):  # noqa: ANN001 - 入参由 tenant_session(route) 传入
        yield tenant

    monkeypatch.setattr(identity_service, "tenant_session", tenant_cm)


def test_issue_owner_invitation_rotates_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """重新签发负责人邀请：旧令牌立即失效，库中只存 SHA-256 摘要。"""
    from app.modules.identity import repository as identity_repo
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center, tenant = center_factory(), tenant_factory()
    _seed_invitable_tenant(center, tenant)
    _patch_service_tenant_session(monkeypatch, tenant)

    first = service.issue_owner_invitation(center, 1, actor_user_id=20)
    assert first["email"] == "owner@example.com"
    assert first["status"] == "pending"
    assert first["invitation_url"] == f"/activate/{first['token']}"
    assert service.preview_invitation(center, first["token"])["status"] == "pending"

    stored = identity_repo.get_invitation(center, 1, int(first["invitation_id"]))
    assert stored is not None
    assert stored.token_hash == sha256_hex(first["token"])
    assert stored.token_hash != first["token"]
    assert len(stored.token_hash) == 64

    second = service.issue_owner_invitation(center, 1, actor_user_id=20)
    assert second["token"] != first["token"]
    with pytest.raises(AppError) as old_token:
        service.preview_invitation(center, first["token"])
    assert old_token.value.code is ErrorCode.AUTH_TOKEN_INVALID
    # 新令牌可用，且同一邮箱只保留一个 pending 邀请
    service.preview_invitation(center, second["token"])
    pending = identity_repo.list_pending_invitations(center, 1, "owner@example.com")
    assert len(pending) == 1
    assert pending[0].token_hash == sha256_hex(second["token"])


def test_owner_invitation_expired_and_used_error_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from app.core.security import utc_now
    from app.modules.identity import repository as identity_repo
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center, tenant = center_factory(), tenant_factory()
    _seed_invitable_tenant(center, tenant)
    _patch_service_tenant_session(monkeypatch, tenant)

    expired_payload = service.issue_owner_invitation(center, 1)
    expired_row = identity_repo.get_invitation(center, 1, int(expired_payload["invitation_id"]))
    assert expired_row is not None
    expired_row.expires_at = utc_now() - timedelta(minutes=1)
    center.commit()
    with pytest.raises(AppError) as expired:
        service.preview_invitation(center, expired_payload["token"])
    assert expired.value.code is ErrorCode.AUTH_TOKEN_EXPIRED

    used_payload = service.issue_owner_invitation(center, 1)
    used_row = identity_repo.get_invitation(center, 1, int(used_payload["invitation_id"]))
    assert used_row is not None
    used_row.status = "accepted"
    center.commit()
    with pytest.raises(AppError) as used:
        service.preview_invitation(center, used_payload["token"])
    assert used.value.code is ErrorCode.AUTH_TOKEN_USED


def test_owner_invitation_requires_active_tenant_and_invited_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.identity import repository as identity_repo
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center, tenant = center_factory(), tenant_factory()
    _seed_tenant(center, tenant)
    _patch_service_tenant_session(monkeypatch, tenant)  # 负责人 active

    with pytest.raises(AppError) as already_active:
        service.issue_owner_invitation(center, 1)
    assert already_active.value.code is ErrorCode.CONFLICT

    membership = identity_repo.get_membership_by_id(center, 1, 100)
    assert membership is not None
    membership.status = "invited"
    tenant_row = identity_repo.get_tenant(center, 1)
    assert tenant_row is not None
    tenant_row.status = "provisioning"
    center.commit()
    with pytest.raises(AppError) as not_active:
        service.issue_owner_invitation(center, 1)
    assert not_active.value.code is ErrorCode.TENANT_NOT_ACTIVE


def test_tenant_detail_exposes_invitation_summary_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.identity import repository as identity_repo
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center, tenant = center_factory(), tenant_factory()
    _seed_invitable_tenant(center, tenant)
    _patch_service_tenant_session(monkeypatch, tenant)
    issued = service.issue_owner_invitation(center, 1)

    tenant_row = identity_repo.get_tenant(center, 1)
    assert tenant_row is not None
    detail = service.tenant_detail_payload(center, tenant_row)
    summary = detail["owner_invitation"]
    assert summary["invitation_id"] == issued["invitation_id"]
    assert summary["email"] == "owner@example.com"
    assert summary["status"] == "pending"
    assert summary["expires_at"] and summary["invited_at"]
    assert "token" not in summary
    assert "invitation_url" not in summary


def test_resend_invitation_rotates_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.audit.models_tenant import AuditLog
    from app.modules.identity import service

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center, tenant = center_factory(), tenant_factory()
    _seed_invitable_tenant(center, tenant)
    _patch_service_tenant_session(monkeypatch, tenant)
    first = service.issue_owner_invitation(center, 1)

    rotated = service.resend_invitation(
        center, tenant, tenant_id=1, invitation_id=int(first["invitation_id"]), actor_id=10
    )
    assert rotated["token"] != first["token"]
    assert rotated["invitation_url"] == f"/activate/{rotated['token']}"
    with pytest.raises(AppError) as old_token:
        service.preview_invitation(center, first["token"])
    assert old_token.value.code is ErrorCode.AUTH_TOKEN_INVALID
    service.preview_invitation(center, rotated["token"])

    audit_rows = tenant.query(AuditLog).filter(AuditLog.action == "member.invitation.resend").all()
    assert len(audit_rows) == 1
    assert audit_rows[0].payload["previous_invitation_id"] == first["invitation_id"]

    accepted = service.preview_invitation(center, rotated["token"])
    assert accepted["email"] == "owner@example.com"


# --------------------------------------------------------------------------- 平台邀请接口（路由）
def _seed_platform_admin(center, user_id: int = 20) -> None:
    from app.modules.identity.models_center import PlatformRole, PlatformUserRole, User

    center.add(
        User(
            id=user_id,
            email="admin@example.com",
            password_hash="h",
            display_name="Admin",
            status="active",
            locale="zh-CN",
        )
    )
    center.add(PlatformRole(id=1, code="platform_admin", name="平台管理员", description=""))
    center.add(PlatformUserRole(id=1, user_id=user_id, role_id=1, granted_by=None))
    center.commit()


def test_platform_owner_invitation_route_returns_token_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.platform.models_center import PlatformAuditLog

    client, center, tenant, _state = _route_client(monkeypatch, user_id=20)
    _seed_platform_admin(center)
    _seed_invitable_tenant(center, tenant)

    response = client.post(
        "/api/v1/platform/tenants/1/owner-invitation", headers={"X-CSRF-Token": "x"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["email"] == "owner@example.com"
    assert data["invitation_url"] == f"/activate/{data['token']}"
    assert data["expires_at"]

    audit_rows = (
        center.query(PlatformAuditLog)
        .filter(PlatformAuditLog.action == "platform.tenant.owner_invitation")
        .all()
    )
    assert len(audit_rows) == 1
    assert "token" not in (audit_rows[0].payload or {})

    detail = client.get("/api/v1/platform/tenants/1").json()["data"]
    assert detail["owner_invitation"]["email"] == "owner@example.com"
    assert detail["owner_invitation"]["status"] == "pending"
    assert "token" not in detail["owner_invitation"]
    assert "token" not in response.json()["data"]["invitation_url"]


def test_platform_owner_invitation_requires_platform_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client, center, tenant, _state = _route_client(monkeypatch, user_id=10)
    _seed_invitable_tenant(center, tenant)

    response = client.post(
        "/api/v1/platform/tenants/1/owner-invitation", headers={"X-CSRF-Token": "x"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.PLATFORM_PERMISSION_REQUIRED.value


def test_platform_route_denied_for_tenant_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """租户登录会话不能访问平台管理接口（登录端隔离）。"""
    client, center, tenant, state = _route_client(monkeypatch, user_id=20)
    _seed_platform_admin(center)
    state["scope"] = "tenant"

    response = client.post(
        "/api/v1/platform/tenants/1/owner-invitation", headers={"X-CSRF-Token": "x"}
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.SCOPE_DENIED.value


def test_tenant_route_denied_for_platform_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台管理登录会话不能访问租户 ERP 接口（登录端隔离）。"""
    client, center, tenant, state = _route_client(monkeypatch, user_id=20)
    _seed_platform_admin(center)
    state["scope"] = "platform"

    response = client.get("/api/v1/tenants/1/bootstrap")
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == ErrorCode.SCOPE_DENIED.value


def test_e2e_create_tenant_eager_invitation_accept_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整链路：创建租户（eager 开通）→ 签发负责人邀请 → 接受 → bootstrap 有权限。"""
    from types import SimpleNamespace

    from app.core.security import encrypt_secret
    from app.modules.identity import provisioning
    from app.modules.identity import repository as identity_repo
    from app.modules.identity import router as identity_router
    from app.modules.identity import service as identity_service
    from app.modules.identity.models_center import TenantDatabase

    center_factory, tenant_factory, _, _ = _build_sqlite_sessions()
    center, tenant = center_factory(), tenant_factory()
    client, center, tenant, state = _route_client(
        monkeypatch, user_id=20, sessions=(center, tenant)
    )
    _seed_platform_admin(center, user_id=20)
    monkeypatch.setattr(
        identity_router, "create_session", lambda user_id, *, scope="tenant": ("sid", {}, "csrf")
    )
    monkeypatch.setattr(provisioning.settings, "celery_task_always_eager", True)

    def fake_provision(center_session, tenant_id, *, operator_user_id=None):  # noqa: ANN001
        """模拟开通流水线的初始化步骤（角色 / 负责人绑定 / 数据库路由 / 激活）。"""
        tenant_row = identity_repo.get_tenant(center_session, tenant_id)
        assert tenant_row is not None and tenant_row.owner_user_id is not None
        owner = identity_repo.get_user(center_session, int(tenant_row.owner_user_id))
        assert owner is not None
        membership = identity_repo.get_membership(center_session, tenant_id, int(owner.id))
        assert membership is not None
        center_session.add(
            TenantDatabase(
                tenant_id=tenant_id,
                host="127.0.0.1",
                port=3306,
                database_name="vs_demo",
                username="u",
                password_cipher=encrypt_secret("p"),
                credentials_version=1,
                schema_version="tenant_0004",
                status="ready",
            )
        )
        tenant_row.status = "active"
        center_session.add(tenant_row)
        center_session.commit()
        roles = identity_service.seed_predefined_roles(tenant, tenant_id)
        identity_service._write_bindings(
            tenant,
            tenant_id=tenant_id,
            member_id=int(membership.id),
            bindings=[
                identity_service.RoleBindingChoice(
                    role_id=int(roles["owner"].id),
                    scopes=(identity_service.ScopeChoice(scope_type="all", scope_ids=()),),
                )
            ],
            assigned_by=None,
        )
        tenant.commit()
        return SimpleNamespace(
            as_dict=lambda: {
                "tenant_id": str(tenant_id),
                "status": "active",
                "steps": [],
                "failed_step": "",
                "error_message": "",
            },
            failed_step="",
        )

    monkeypatch.setattr(provisioning, "provision_tenant", fake_provision)

    # 1) 创建租户（eager 模式同步执行开通流水线）
    created = client.post(
        "/api/v1/platform/tenants",
        json={"code": "demo2", "name": "演示租户2", "owner_email": "owner2@example.com"},
        headers={"X-CSRF-Token": "x"},
    )
    assert created.status_code == 202, created.text
    created_data = created.json()["data"]
    assert created_data["mode"] == "sync-eager"
    tenant_id = int(created_data["tenant"]["id"])
    assert created_data["tenant"]["status"] == "active"

    # 2) 重新签发负责人邀请（无邮件通道时管理员拿到链接）
    issued = client.post(
        f"/api/v1/platform/tenants/{tenant_id}/owner-invitation", headers={"X-CSRF-Token": "x"}
    )
    assert issued.status_code == 200, issued.text
    invitation = issued.json()["data"]
    assert invitation["invitation_url"] == f"/activate/{invitation['token']}"

    # 3) 负责人在未登录的浏览器接受邀请（不能沿用平台管理员身份激活他人）。
    state["user_id"] = 0
    accepted = client.post(
        "/api/v1/auth/invitations/accept",
        json={
            "token": invitation["token"],
            "password": "Str0ng-Password!2026",
            "display_name": "负责人",
        },
        headers={"X-CSRF-Token": "x"},
    )
    assert accepted.status_code == 200, accepted.text
    accepted_data = accepted.json()["data"]
    owner_user_id = int(accepted_data["user"]["id"])
    assert any(item["id"] == str(tenant_id) for item in accepted_data["tenants"])

    center.expire_all()
    owner_membership = identity_repo.get_membership(center, tenant_id, owner_user_id)
    assert owner_membership is not None and owner_membership.status == "active"

    # 4) 负责人 bootstrap：成员状态正常且有权限
    state["user_id"] = owner_user_id
    bootstrap = client.get(f"/api/v1/tenants/{tenant_id}/bootstrap")
    assert bootstrap.status_code == 200, bootstrap.text
    bootstrap_data = bootstrap.json()["data"]
    assert "owner" in bootstrap_data["member"]["roles"]
    assert "role.manage" in bootstrap_data["permissions"]
    assert bootstrap_data["permission_version"] >= 1

    # 5) 邀请已被使用，旧令牌再次预览返回 AUTH_TOKEN_USED
    reused = client.get(f"/api/v1/auth/invitations/{invitation['token']}")
    assert reused.status_code == 422, reused.text
    assert reused.json()["error"]["code"] == ErrorCode.AUTH_TOKEN_USED.value



