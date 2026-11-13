"""批次2：租户模式边界、固定实例绑定、实例迁移状态与启用防双写测试。

不连接 MySQL / Redis：

* 模式守卫用 FastAPI TestClient（真实中间件/依赖链，仅 monkeypatch settings）；
* 实例字段与启用防双写用 SQLite 内存库直接调用 Service 层。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Integer, MetaData, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.core.config import settings
from app.core.errors import AppError, ConflictError, ErrorCode
from app.core.mode import (
    boundary_violation,
    collect_tenant_ids,
    ensure_payload_tenant_id,
    ensure_task_payload_tenant_id,
    mode_allows_path,
    platform_only_path,
)
from app.db.base import CenterBase
from app.modules.ops import service as ops_service
from app.modules.ops.models_center import ServerNode, TenantInstance
from app.modules.ops.schemas import InstanceUpdate, OperationCreate

# SQLite 只建测试涉及的表（克隆元数据，主键改 INTEGER 以支持自增）
TEST_TABLES = (
    "tenants",
    "tenant_databases",
    "tenant_instances",
    "server_nodes",
    "alert_events",
    "platform_audit_logs",
    "operation_jobs",
    "operation_job_steps",
    "backup_records",
    "release_versions",
    "node_metric_samples",
    "node_checks",
    "maintenance_windows",
    "node_metric_aggregates",
    "users",
    "tenant_memberships",
    "tenant_invitations",
    "provisioning_jobs",
    "platform_roles",
    "platform_user_roles",
    "plans",
    "plan_features",
    "subscriptions",
)


def _sqlite_metadata() -> MetaData:
    clone = MetaData()
    for name in TEST_TABLES:
        table = CenterBase.metadata.tables[name].to_metadata(clone)
        id_column = table.columns.get("id")
        if id_column is not None:
            id_column.type = Integer()
            id_column.autoincrement = True
    return clone


@pytest.fixture()
def center() -> Session:
    import app.modules.access.models_tenant  # noqa: F401
    import app.modules.identity.models_center  # noqa: F401
    import app.modules.ops.models_center  # noqa: F401
    import app.modules.platform.models_center  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    _sqlite_metadata().create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- 模式边界纯函数
def test_platform_only_path_rules() -> None:
    assert platform_only_path("/api/v1/platform/instances")
    assert platform_only_path("/api/v1/platform/nodes/1/ssh/ws")
    assert platform_only_path("/api/v1/agent/ping")
    assert not platform_only_path("/api/v1/auth/csrf")
    assert not platform_only_path("/api/v1/tenants/3/bootstrap")
    assert not platform_only_path("/healthz")


def test_mode_allows_path_rules() -> None:
    assert mode_allows_path("center", "/api/v1/platform/instances")
    assert not mode_allows_path("tenant", "/api/v1/platform/instances")
    assert not mode_allows_path("tenant", "/api/v1/agent/ping")
    assert mode_allows_path("tenant", "/api/v1/auth/csrf")
    assert mode_allows_path("tenant", "/api/v1/tenants/3/bootstrap")
    assert mode_allows_path("tenant", "/healthz")
    assert mode_allows_path("gateway", "/api/v1/gateway/health")
    assert not mode_allows_path("gateway", "/api/v1/tenants/3/bootstrap")
    assert boundary_violation("tenant", "/api/v1/platform/instances") is True
    assert boundary_violation("tenant", "/api/v1/auth/login") is False


def test_collect_tenant_ids_nested_payload() -> None:
    payload = {
        "tenant_id": "3",
        "items": [{"tenant_id": 4}, {"nested": {"tenant_id": 5}}],
        "other": "x",
    }
    assert sorted(collect_tenant_ids(payload)) == [3, 4, 5]


def test_ensure_payload_tenant_id_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "tenant")
    monkeypatch.setattr(settings, "tenant_id", 3)
    ensure_payload_tenant_id({"tenant_id": 3, "items": [{"tenant_id": 3}]})
    with pytest.raises(AppError) as error:
        ensure_payload_tenant_id({"items": [{"tenant_id": 4}]})
    assert error.value.code is ErrorCode.TENANT_FORBIDDEN


def test_ensure_task_payload_tenant_id() -> None:
    ensure_task_payload_tenant_id({"tenant_id": 7}, expected_tenant_id=7)
    with pytest.raises(AppError) as mismatch:
        ensure_task_payload_tenant_id({"tenant_id": 8}, expected_tenant_id=7)
    assert mismatch.value.code is ErrorCode.TENANT_FORBIDDEN
    with pytest.raises(AppError):
        ensure_task_payload_tenant_id({"tenant_id": 7}, expected_tenant_id=0)


# --------------------------------------------------------------------------- 中间件与 HTTP 边界
def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_mode_boundary_middleware_registered() -> None:
    from app.api.mode_guard import ModeBoundaryMiddleware
    from app.main import app

    assert any(middleware.cls is ModeBoundaryMiddleware for middleware in app.user_middleware)


def test_tenant_mode_blocks_platform_and_agent_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "tenant")
    monkeypatch.setattr(settings, "tenant_id", 3)
    client = _client()
    assert client.get("/api/v1/platform/instances").status_code == 404
    assert client.get("/api/v1/agent/ping").status_code == 404
    # 健康检查 / 运行时 / 认证接口不被误伤
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/runtime").status_code == 200
    blocked = client.get("/api/v1/platform/instances")
    assert blocked.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


def test_center_mode_keeps_platform_and_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "center")
    client = _client()
    assert client.get("/api/v1/agent/ping").status_code == 200
    platform_status = client.get("/api/v1/platform/instances").status_code
    assert platform_status != 404  # 未登录 -> 401/403，但不能被模式守卫吞掉


def test_tenant_mode_blocks_platform_websocket(monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr(settings, "deployment_mode", "tenant")
    monkeypatch.setattr(settings, "tenant_id", 3)
    client = _client()
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/api/v1/platform/nodes/1/ssh/ws") as websocket:
            websocket.receive_text()
    assert error.value.code == 4404


def test_tenant_binding_guard_on_http_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "tenant")
    monkeypatch.setattr(settings, "tenant_id", 3)
    client = _client()
    mismatch = client.get("/api/v1/tenants/9/bootstrap")
    assert mismatch.status_code == 403
    assert mismatch.json()["error"]["code"] == ErrorCode.TENANT_FORBIDDEN.value
    # 绑定一致时进入正常认证流程（未登录 401，而不是 403）
    matched = client.get("/api/v1/tenants/3/bootstrap")
    assert matched.status_code == 401
    query_mismatch = client.get("/api/v1/tenants/3/bootstrap?tenant_id=9")
    assert query_mismatch.status_code == 403


def test_tenant_binding_requires_configured_tenant_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment_mode", "tenant")
    monkeypatch.setattr(settings, "tenant_id", 0)
    client = _client()
    response = client.get("/api/v1/tenants/3/bootstrap")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.SERVICE_UNAVAILABLE.value
    # 不涉及 tenant_id 的接口仍可访问
    assert client.get("/api/v1/runtime").status_code == 200


def test_http_tenant_binding_guard_reads_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.mode_guard import http_tenant_binding_guard

    monkeypatch.setattr(settings, "deployment_mode", "tenant")
    monkeypatch.setattr(settings, "tenant_id", 3)

    def make_request(body: bytes) -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/tenants/3/invitations",
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "path_params": {"tenant_id": "3"},
        }

        async def receive() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(scope, receive)

    asyncio.run(http_tenant_binding_guard(make_request(b'{"tenant_id": 3}')))
    with pytest.raises(AppError) as mismatch:
        asyncio.run(http_tenant_binding_guard(make_request(b'{"tenant_id": 9}')))
    assert mismatch.value.code is ErrorCode.TENANT_FORBIDDEN


# --------------------------------------------------------------------------- 实例部署模式 / 迁移状态
def seed_tenant_instance(
    center: Session,
    *,
    deployment_mode: str = "shared",
    migration_status: str = "registered",
    status: str = "suspended",
) -> TenantInstance:
    from app.modules.identity.models_center import Tenant, TenantDatabase

    center.add(
        Tenant(
            id=1,
            code="demo",
            name="演示租户",
            status=status,
            timezone="Asia/Shanghai",
        )
    )
    center.add(
        TenantDatabase(
            id=1,
            tenant_id=1,
            host="127.0.0.1",
            port=3306,
            database_name="vs_demo",
            username="u",
            password_cipher="cipher",
            credentials_version=1,
            schema_version="",
            status="ready",
        )
    )
    instance = TenantInstance(
        id=1,
        tenant_id=1,
        name="演示实例",
        domain="demo.example.com",
        environment="production",
        status="active",
        deployment_mode=deployment_mode,
        migration_status=migration_status,
    )
    center.add(instance)
    center.add(
        ServerNode(
            id=1,
            instance_id=1,
            tenant_id=1,
            node_uuid="11111111-1111-1111-1111-111111111111",
            hostname="demo-host",
            status="online",
            token_hash="hash",
        )
    )
    center.commit()
    return instance


def test_instance_payload_exposes_deployment_fields(center: Session) -> None:
    instance = seed_tenant_instance(center, deployment_mode="independent", migration_status="migrated")
    payload = ops_service.instance_payload(center, instance, "演示租户")
    assert payload["deployment_mode"] == "independent"
    assert payload["migration_status"] == "migrated"
    assert payload["write_blocked"] is True


def test_instance_defaults_are_backward_compatible(center: Session) -> None:
    instance = seed_tenant_instance(center)
    instance.deployment_mode = ""
    instance.migration_status = ""
    center.commit()
    payload = ops_service.instance_payload(center, instance)
    assert payload["deployment_mode"] == "shared"
    assert payload["migration_status"] == "registered"
    assert payload["write_blocked"] is False


def test_update_instance_blocks_activate_when_migrated(center: Session) -> None:
    instance = seed_tenant_instance(center, deployment_mode="independent", migration_status="migrated")
    instance.status = "suspended"
    center.commit()
    with pytest.raises(ConflictError) as error:
        ops_service.update_instance(
            center, 1, InstanceUpdate(status="active"), actor_user_id=1
        )
    assert error.value.code is ErrorCode.CONFLICT
    assert "迁移" in error.value.message


def test_update_instance_allows_activate_when_shared(center: Session) -> None:
    seed_tenant_instance(center)
    payload = ops_service.update_instance(
        center, 1, InstanceUpdate(status="active"), actor_user_id=1
    )
    assert payload["status"] == "active"


def test_list_instances_filters_by_deployment_and_migration(center: Session) -> None:
    seed_tenant_instance(center, deployment_mode="independent", migration_status="migrated")
    result = ops_service.list_instances(
        center, page=1, page_size=20, deployment_mode="independent", migration_status="migrated"
    )
    assert result["total"] == 1
    empty = ops_service.list_instances(
        center, page=1, page_size=20, deployment_mode="shared", migration_status="registered"
    )
    assert empty["total"] == 0


def test_create_operation_blocked_for_migrating_instance(center: Session) -> None:
    seed_tenant_instance(center, migration_status="migrating")
    with pytest.raises(ConflictError):
        ops_service.create_operation(
            center,
            1,
            OperationCreate(job_type="restart"),
            actor_user_id=1,
        )


def test_tenant_scheduling_blocked_helper(center: Session) -> None:
    seed_tenant_instance(center, deployment_mode="independent", migration_status="migrated")
    assert ops_service.tenant_scheduling_blocked(center, 1) is True
    assert ops_service.get_instance_by_tenant(center, 999) is None


# --------------------------------------------------------------------------- 普通启用防双写（identity）
def test_activate_tenant_blocked_for_independent_instance(center: Session) -> None:
    from app.modules.identity import provisioning

    seed_tenant_instance(center, deployment_mode="independent", migration_status="migrated")
    with pytest.raises(AppError) as error:
        provisioning.activate_tenant(center, tenant_id=1, operator_user_id=1)
    assert error.value.code is ErrorCode.CONFLICT


def test_activate_tenant_allowed_for_shared_instance(center: Session) -> None:
    from app.modules.identity import provisioning

    seed_tenant_instance(center)
    tenant = provisioning.activate_tenant(center, tenant_id=1, operator_user_id=1)
    assert tenant.status == "active"


def test_provision_tenant_blocked_for_migrating_instance(center: Session) -> None:
    from app.modules.identity import provisioning
    from app.modules.identity.models_center import Tenant

    seed_tenant_instance(center, migration_status="migrating", status="provisioning")
    with pytest.raises(AppError) as error:
        provisioning.provision_tenant(center, 1)
    assert error.value.code is ErrorCode.CONFLICT
    center.expire_all()
    tenant = center.get(Tenant, 1)
    assert tenant is not None and tenant.status == "provisioning"


# --------------------------------------------------------------------------- 历史重复账号同步路径已停用
def test_sync_local_users_is_deprecated() -> None:
    from app.ops_tools import sync_local_users

    with pytest.raises(RuntimeError) as error:
        sync_local_users.sync(1)
    assert "不是认证来源" in str(error.value)

    import sys

    original = sys.argv
    sys.argv = ["sync_local_users", "--tenant-id", "1"]
    try:
        assert sync_local_users.main() == 2
    finally:
        sys.argv = original


# --------------------------------------------------------------------------- 模型列与迁移一致性
def test_models_have_new_columns_with_chinese_comments() -> None:
    from app.modules.ops.models_center import ReleaseVersion

    instance_columns = TenantInstance.__table__.columns
    assert "deployment_mode" in instance_columns
    assert "migration_status" in instance_columns
    node_columns = ServerNode.__table__.columns
    for column_name in (
        "ssh_host_fingerprint",
        "ssh_host_fingerprint_verified_at",
        "ssh_fingerprint_algorithm",
    ):
        assert column_name in node_columns
        assert any("\u4e00" <= char <= "\u9fff" for char in (node_columns[column_name].comment or ""))
    assert ReleaseVersion.__table__.columns["commit_sha"].comment
