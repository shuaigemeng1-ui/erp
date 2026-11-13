"""发布版本清单与升级校验的正式单元测试（task-16 / task-21）。

使用 SQLite 内存库直接调用 Service 层，不连接 MySQL / Redis：

* 发布版本：完整 commit 校验、字段回填（PATCH）、旧记录兼容、平台审计；
* 升级任务：目标版本未登记 / 已退役 / Agent 协议不足 / 幂等重放；
* Agent 事件：校验失败判定 failed、旧式/缺失校验 partial/missing、事件序号去重；
* 实例展示字段：target_version / actual_version / db_version / upgrade_status。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import Integer, MetaData, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import AppError, ConflictError, ErrorCode
from app.db.base import CenterBase
from app.modules.ops import service
from app.modules.ops.models_center import OperationJob, ReleaseVersion, ServerNode, TenantInstance
from app.modules.ops.schemas import AgentJobEvent, OperationCreate, ReleaseCreate, ReleaseUpdate
from app.modules.platform.models_center import PlatformAuditLog

# 只创建本测试涉及的运维中心表（避免依赖其他模块的建表顺序）
OPS_TABLES = (
    "release_versions",
    "tenant_instances",
    "server_nodes",
    "operation_jobs",
    "operation_job_steps",
    "backup_records",
    "alert_events",
    "platform_audit_logs",
    "node_metric_samples",
    "node_checks",
    "maintenance_windows",
    "node_metric_aggregates",
)


def _sqlite_metadata() -> MetaData:
    """复制表结构到独立 MetaData，并把主键改为 SQLite 可自增的 INTEGER。"""
    clone = MetaData()
    for name in OPS_TABLES:
        table = CenterBase.metadata.tables[name].to_metadata(clone)
        id_column = table.columns.get("id")
        if id_column is not None:
            id_column.type = Integer()
            id_column.autoincrement = True
    return clone


@pytest.fixture()
def center() -> Session:
    import app.modules.ops.models_center  # noqa: F401  注册运维中心表
    import app.modules.platform.models_center  # noqa: F401  注册平台审计表

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    _sqlite_metadata().create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def seed_instance_and_node(center: Session, *, protocol_version: int = 2) -> tuple[TenantInstance, ServerNode]:
    instance = TenantInstance(
        id=1,
        tenant_id=1,
        name="演示实例",
        domain="demo.example.com",
        environment="production",
        status="active",
        app_version="0.9.0",
        upgrade_status="unknown",
    )
    node = ServerNode(
        id=1,
        instance_id=1,
        tenant_id=1,
        node_uuid="11111111-1111-1111-1111-111111111111",
        hostname="demo-host",
        status="online",
        token_hash="hash",
        protocol_version=protocol_version,
        agent_version="0.2.0",
        app_version="0.9.0",
    )
    center.add_all([instance, node])
    center.commit()
    return instance, node


def create_valid_release(center: Session, version: str = "1.0.0", **overrides) -> dict:
    payload = {
        "version": version,
        "commit_sha": "a" * 40,
        "tenant_db_version": "tenant_0016",
    }
    payload.update(overrides)
    return service.create_release(center, ReleaseCreate(**payload), actor_user_id=1)


def create_upgrade_job(center: Session, target_version: str, *, idempotency_key: str = "") -> dict:
    payload = OperationCreate(
        job_type="upgrade", target_version=target_version, idempotency_key=idempotency_key
    )
    return service.create_operation(center, 1, payload, actor_user_id=1)


# --------------------------------------------------------------------------- 发布版本登记
def test_short_commit_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        ReleaseCreate(version="1.0.0", commit_sha="abc123")


def test_full_commit_normalized_and_fields_exposed(center: Session) -> None:
    release = service.create_release(
        center,
        ReleaseCreate(
            version="1.0.0",
            commit_sha="A" * 40,
            image_tag="erp:1.0.0",
            build_config_summary="python3.12 / requirements.lock",
            min_agent_protocol=2,
            center_db_version="center_0016",
            tenant_db_version="tenant_0016",
            rollback_compatible=True,
            build_status="verified",
            notes="首个可信版本",
        ),
        actor_user_id=1,
    )
    assert release["commit_sha"] == "a" * 40
    assert release["min_agent_protocol"] == 2
    assert release["rollback_compatible"] is True
    assert release["build_status"] == "verified"
    assert release["center_db_version"] == "center_0016"
    assert release["tenant_db_version"] == "tenant_0016"


def test_legacy_release_defaults_are_backward_compatible(center: Session) -> None:
    assert service.ensure_release(center, "0.9.0") is True
    legacy = next(item for item in service.list_releases(center) if item["version"] == "0.9.0")
    assert legacy["commit_sha"] == ""
    assert legacy["min_agent_protocol"] == 0
    assert legacy["build_status"] == "built"
    assert legacy["rollback_compatible"] is False


def test_patch_release_backfills_legacy_commit(center: Session) -> None:
    service.ensure_release(center, "0.9.0")
    legacy = center.query(ReleaseVersion).filter(ReleaseVersion.version == "0.9.0").one()

    updated = service.update_release(
        center,
        int(legacy.id),
        ReleaseUpdate(
            commit_sha="b" * 64,
            build_config_summary="回填构建摘要",
            min_agent_protocol=1,
            center_db_version="center_0016",
            tenant_db_version="tenant_0016",
            rollback_compatible=True,
            build_status="verified",
            notes="回填完成",
        ),
        actor_user_id=7,
    )
    assert updated["commit_sha"] == "b" * 64
    assert updated["build_status"] == "verified"
    assert updated["min_agent_protocol"] == 1
    assert updated["rollback_compatible"] is True

    # 回填后即可作为升级目标
    seed_instance_and_node(center)
    job = create_upgrade_job(center, "0.9.0")
    assert job["target_version"] == "0.9.0"
    assert job["upgrade_status"] == "pending"

    audit = (
        center.query(PlatformAuditLog)
        .filter(PlatformAuditLog.action == "release.update")
        .one()
    )
    assert audit.resource_id == int(legacy.id)
    assert "commit_sha" in (audit.payload or {}).get("fields", [])


def test_patch_release_invalid_commit_and_missing_release(center: Session) -> None:
    with pytest.raises(ValidationError):
        ReleaseUpdate(commit_sha="not-a-commit")
    with pytest.raises(AppError) as not_found:
        service.update_release(center, 999, ReleaseUpdate(notes="x"), actor_user_id=1)
    assert not_found.value.code is ErrorCode.NOT_FOUND


# --------------------------------------------------------------------------- 升级目标校验
def test_unregistered_target_rejected(center: Session) -> None:
    seed_instance_and_node(center)
    with pytest.raises(AppError) as error:
        create_upgrade_job(center, "9.9.9")
    assert error.value.code is ErrorCode.VALIDATION_ERROR


def test_legacy_release_without_commit_rejected(center: Session) -> None:
    seed_instance_and_node(center)
    service.ensure_release(center, "0.9.0")
    with pytest.raises(AppError) as error:
        create_upgrade_job(center, "0.9.0")
    assert error.value.code is ErrorCode.VALIDATION_ERROR


def test_retired_release_rejected(center: Session) -> None:
    seed_instance_and_node(center)
    create_valid_release(center, "1.1.0", status="retired")
    with pytest.raises(AppError) as error:
        create_upgrade_job(center, "1.1.0")
    assert error.value.code is ErrorCode.VALIDATION_ERROR


def test_agent_protocol_too_low_conflict(center: Session) -> None:
    seed_instance_and_node(center, protocol_version=1)
    create_valid_release(center, "1.2.0", min_agent_protocol=3)
    with pytest.raises(ConflictError) as error:
        create_upgrade_job(center, "1.2.0")
    assert error.value.code is ErrorCode.CONFLICT


def test_duplicate_idempotency_returns_same_job(center: Session) -> None:
    seed_instance_and_node(center)
    create_valid_release(center, "1.3.0")
    first = create_upgrade_job(center, "1.3.0", idempotency_key="dup-key")
    second = create_upgrade_job(center, "1.3.0", idempotency_key="dup-key")
    assert first["id"] == second["id"]
    assert center.query(OperationJob).count() == 1


# --------------------------------------------------------------------------- Agent 事件与校验
def _claim(center: Session, node: ServerNode) -> dict:
    claimed = service.claim_agent_job(center, node, agent_version="0.3.0", protocol_version=2)
    assert claimed is not None
    return claimed


def test_checks_failed_marks_job_and_instance_failed(center: Session) -> None:
    instance, node = seed_instance_and_node(center)
    create_valid_release(center, "1.4.0")
    job = create_upgrade_job(center, "1.4.0")
    claimed = _claim(center, node)

    service.agent_job_event(
        center,
        node,
        int(job["id"]),
        AgentJobEvent(
            status="succeeded",
            attempt_id=claimed["attempt_id"],
            lease_token=claimed["lease_token"],
            event_seq=1,
            result={"actual_version": "1.4.0"},
            checks={
                "db_version": {"ok": True, "value": "tenant_0016"},
                "readiness": {"ok": False, "value": "api unhealthy"},
                "business_readonly": {"ok": True, "value": "readonly"},
            },
        ),
    )
    row = center.get(OperationJob, int(job["id"]))
    assert row.status == "failed"
    assert row.checks_status == "failed"
    assert "readiness" in row.error
    center.refresh(instance)
    assert instance.upgrade_status == "failed"


def test_legacy_checks_partial_and_missing(center: Session) -> None:
    instance, node = seed_instance_and_node(center)
    create_valid_release(center, "1.5.0")
    job = create_upgrade_job(center, "1.5.0")
    claimed = _claim(center, node)
    # 旧式校验（commit/build/readiness 之外的自定义项）→ partial
    service.agent_job_event(
        center,
        node,
        int(job["id"]),
        AgentJobEvent(
            status="succeeded",
            attempt_id=claimed["attempt_id"],
            lease_token=claimed["lease_token"],
            event_seq=1,
            result={
                "target_version": "1.5.0",
                "checks": {
                    "commit_verified": True,
                    "build": "succeeded",
                    "services_ready": "succeeded",
                },
            },
        ),
    )
    row = center.get(OperationJob, int(job["id"]))
    assert row.status == "succeeded"
    assert row.checks_status == "partial"
    assert row.actual_version == "1.5.0"
    center.refresh(instance)
    assert instance.upgrade_status == "succeeded"
    assert instance.actual_version == "1.5.0"

    # 完全没有校验结果 → missing，但旧 Agent 兼容不阻断成功
    create_valid_release(center, "1.6.0")
    job2 = create_upgrade_job(center, "1.6.0")
    claimed2 = _claim(center, node)
    service.agent_job_event(
        center,
        node,
        int(job2["id"]),
        AgentJobEvent(
            status="succeeded",
            attempt_id=claimed2["attempt_id"],
            lease_token=claimed2["lease_token"],
            event_seq=1,
        ),
    )
    row2 = center.get(OperationJob, int(job2["id"]))
    assert row2.status == "succeeded"
    assert row2.checks_status == "missing"


def test_event_seq_is_idempotent(center: Session) -> None:
    seed_instance_and_node(center)
    create_valid_release(center, "1.7.0")
    job = create_upgrade_job(center, "1.7.0")
    claimed = _claim(center, node=center.get(ServerNode, 1))
    event = AgentJobEvent(
        status="running",
        step_no=4,
        step_name="构建镜像",
        step_status="running",
        attempt_id=claimed["attempt_id"],
        lease_token=claimed["lease_token"],
        event_seq=1,
    )
    first = service.agent_job_event(center, center.get(ServerNode, 1), int(job["id"]), event)
    assert first["event_seq"] == 1
    duplicate = service.agent_job_event(center, center.get(ServerNode, 1), int(job["id"]), event)
    assert duplicate.get("duplicate") is True


def test_instance_payload_exposes_upgrade_fields(center: Session) -> None:
    instance, _node = seed_instance_and_node(center)
    create_valid_release(center, "1.8.0")
    create_upgrade_job(center, "1.8.0")
    payload = service.instance_payload(center, instance, "演示租户")
    assert payload["target_version"] == "1.8.0"
    assert payload["upgrade_status"] == "pending"
    assert "actual_version" in payload
    assert "agent_version" in payload
    assert "executor_version" in payload
    assert "db_version" in payload
    assert payload["nodes"][0]["protocol_version"] == 2


# --------------------------------------------------------------------------- PATCH 回填补充场景
def test_patch_release_retire_blocks_following_upgrade(center: Session) -> None:
    """PATCH 把版本标记 retired 后，新的升级任务必须被拒绝。"""
    seed_instance_and_node(center)
    release = create_valid_release(center, "1.9.0")
    retired = service.update_release(
        center, int(release["id"]), ReleaseUpdate(status="retired"), actor_user_id=1
    )
    assert retired["status"] == "retired"
    with pytest.raises(AppError) as error:
        create_upgrade_job(center, "1.9.0")
    assert error.value.code is ErrorCode.VALIDATION_ERROR
    # 恢复 active 后可继续作为目标
    service.update_release(center, int(release["id"]), ReleaseUpdate(status="active"), actor_user_id=1)
    job = create_upgrade_job(center, "1.9.0")
    assert job["target_version"] == "1.9.0"


def test_platform_audit_labels_cover_release_actions() -> None:
    """平台审计标签必须覆盖 release.create / release.update（前端审计页展示中文）。"""
    from app.modules.identity.constants import PLATFORM_AUDIT_ACTIONS

    assert PLATFORM_AUDIT_ACTIONS["release.create"] == "登记发布版本"
    assert PLATFORM_AUDIT_ACTIONS["release.update"] == "更新发布版本"
