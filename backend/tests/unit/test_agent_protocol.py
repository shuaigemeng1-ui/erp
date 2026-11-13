"""Agent 2 可靠执行协议服务端测试（仅 SQLite 内存库，不依赖 MySQL/Redis/真实 Agent）。

覆盖 task-20 的 8 项验收点：

* 重复 poll 在租约内返回同一 attempt / lease_token；
* 任务心跳续租 120s，错误 token / 旧 attempt / 过期租约 409；
* 旧协议 Agent 不领取 deploy/upgrade/restart/backup/config 等变更任务；
* event_seq 幂等去重、重复终态幂等、冲突终态与旧 attempt 拒绝覆盖；
* reconcile 只核对标记（unknown → succeeded/failed/unknown），不重跑变更；
* 批量 telemetry 按 boot_id+sample_seq 去重，单条旧格式兼容。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.errors import AppError, ErrorCode
from app.db.base import CenterBase
from app.modules.ops import agent_protocol as protocol
from app.modules.ops import service
from app.modules.ops.agent_protocol import (
    JobEventIn,
    JobHeartbeatIn,
    JobPollIn,
    JobReconcileIn,
    NodeHeartbeatIn,
    TelemetrySampleIn,
)
from app.modules.ops.models_center import (
    NodeMetricSample,
    OperationJob,
    ReleaseVersion,
    ServerNode,
    TenantInstance,
)

AUTH_TOKEN = "node-token-for-tests"
CENTER_TABLES = (
    "tenant_instances",
    "server_nodes",
    "operation_jobs",
    "operation_job_steps",
    "node_metric_samples",
    "alert_events",
    "release_versions",
    "backup_records",
)


def _sqlite_metadata(metadata, table_names):
    """复制表结构并把 BIGINT 主键改为 SQLite 可自增的 INTEGER（不动真实模型）。"""
    from sqlalchemy import Integer, MetaData

    clone = MetaData()
    for name in table_names:
        table = metadata.tables[name].to_metadata(clone)
        id_column = table.columns.get("id")
        if id_column is not None:
            id_column.type = Integer()
            id_column.autoincrement = True
    return clone


@pytest.fixture()
def center():
    """SQLite 内存中心库 + 一个已注册的租户实例与节点。"""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _sqlite_metadata(CenterBase.metadata, CENTER_TABLES).create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    session.add(
        TenantInstance(
            id=1,
            tenant_id=1,
            name="测试实例",
            domain="demo.example.com",
            status="active",
            app_version="1.0.0",
        )
    )
    session.add(
        ServerNode(
            id=1,
            instance_id=1,
            tenant_id=1,
            node_uuid="11111111-1111-1111-1111-111111111111",
            hostname="node-1",
            token_hash=service.hash_token(AUTH_TOKEN),
            status="online",
            agent_version="1.0.0",
            app_version="1.0.0",
        )
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _node(center, node_id: int = 1) -> ServerNode:
    return center.get(ServerNode, node_id)


def _heartbeat(center, *, protocol_version: int = 2, node_id: int = 1, **kwargs):
    return protocol.node_heartbeat(
        center,
        _node(center, node_id),
        NodeHeartbeatIn(protocol_version=protocol_version, **kwargs),
    )


def _add_node(center, node_id: int = 2) -> ServerNode:
    node = ServerNode(
        id=node_id,
        instance_id=1,
        tenant_id=1,
        node_uuid=f"22222222-2222-2222-2222-{node_id:012d}",
        hostname=f"node-{node_id}",
        token_hash=service.hash_token(f"token-{node_id}"),
        status="online",
    )
    center.add(node)
    center.commit()
    return node


def _add_job(
    center,
    *,
    job_type: str = "deploy",
    status: str = "pending",
    target_version: str = "",
    node_id: int = 1,
    payload: dict | None = None,
    key: str = "job-key",
) -> OperationJob:
    job = OperationJob(
        instance_id=1,
        node_id=node_id,
        job_type=job_type,
        status=status,
        idempotency_key=key,
        target_version=target_version,
        payload_json=payload or {},
    )
    center.add(job)
    center.commit()
    return job


def _poll(center, node: ServerNode, **body):
    return protocol.poll_job(center, node, JobPollIn(**body))


def _lease(center, *, job_type: str = "deploy", target_version: str = ""):
    """心跳为新协议后领取一个任务，返回 (job, poll_data, attempt, token)。"""
    _heartbeat(center, protocol_version=2)
    job = _add_job(center, job_type=job_type, target_version=target_version)
    data = _poll(center, _node(center))
    assert data["job"] is not None
    return job, data, data["job"]["attempt_id"], data["job"]["lease_token"]


# --------------------------------------------------------------------------- 心跳
def test_node_heartbeat_tracks_protocol_capabilities_and_boot_seq(center):
    data = _heartbeat(
        center,
        protocol_version=2,
        hostname="new-host",
        app_version="2.0.0",
        agent_version="2.0.0",
        executor_version="2.0.0",
        capabilities=["telemetry", "heartbeat", "mutation_jobs"],
        boot_id="boot-a",
        seq=1,
    )
    node = _node(center)
    assert node.protocol_version == 2
    assert node.capabilities_json == ["telemetry", "heartbeat", "mutation_jobs"]
    assert node.hostname == "new-host"
    assert node.app_version == "2.0.0"
    assert data["min_protocol_version"] == 2
    assert data["upgrade_required"] is False
    assert data["mutation_allowed"] is True
    assert data["heartbeat_seconds"] == 30
    assert data["job_lease_seconds"] == 120
    assert data["telemetry_seconds"] == 30
    assert data["boot_id"] == "boot-a"
    assert data["seq_ack"] == 1

    # 同一 boot 的乱序旧 seq 不允许回退协议/版本
    stale = _heartbeat(
        center, protocol_version=9, agent_version="9.9.9", boot_id="boot-a", seq=1
    )
    assert stale["stale"] is True
    assert _node(center).protocol_version == 2
    assert _node(center).agent_version == "2.0.0"


def test_old_protocol_heartbeat_requires_upgrade(center):
    data = _heartbeat(center, protocol_version=1)
    assert data["upgrade_required"] is True
    assert data["mutation_allowed"] is False
    assert data["min_protocol_version"] == 2


# --------------------------------------------------------------------------- 任务领取
def test_poll_returns_attempt_and_repeat_poll_returns_same_attempt(center):
    _heartbeat(center, protocol_version=2)
    job = _add_job(center)
    first = _poll(center, _node(center))
    assert first["job"] is not None
    payload = first["job"]
    assert payload["attempt_id"]
    assert payload["lease_token"]
    assert payload["lease_seconds"] == 120
    assert payload["min_agent_protocol"] == 2
    assert payload["status"] == "leased"
    assert payload["attempts"] == 1

    second = _poll(center, _node(center))
    assert second["reused"] is True
    assert second["job"]["attempt_id"] == payload["attempt_id"]
    assert second["job"]["lease_token"] == payload["lease_token"]
    assert second["job"]["attempts"] == 1
    assert job.attempt_id == payload["attempt_id"]


def test_old_agent_cannot_claim_mutation_jobs(center):
    deploy = _add_job(center, job_type="deploy", key="deploy-key")
    data = _poll(center, _node(center))  # 节点协议默认 0
    assert data["job"] is None
    assert data["upgrade_required"] is True
    assert data["blocked_job"]["id"] == str(deploy.id)
    assert deploy.status == "pending"
    assert int(deploy.attempts or 0) == 0


def test_old_agent_can_still_claim_readonly_logs(center):
    _add_job(center, job_type="logs", key="logs-key")
    data = _poll(center, _node(center))
    assert data["job"] is not None
    assert data["job"]["job_type"] == "logs"


def test_mutation_requires_target_release_min_protocol(center):
    center.add(
        ReleaseVersion(
            version="3.0.0",
            commit_sha="a" * 40,
            min_agent_protocol=3,
            status="active",
        )
    )
    center.commit()
    _add_job(center, job_type="upgrade", target_version="3.0.0", key="upgrade-key")
    _heartbeat(center, protocol_version=2)
    data = _poll(center, _node(center))
    assert data["job"] is None
    assert data["blocked_job"]["min_agent_protocol"] == 3


def test_unknown_job_blocks_new_dispatch_until_reconcile(center):
    job, _data, attempt, token = _lease(center, job_type="deploy")
    job.status = "unknown"
    job.error = "执行中失联，结果待核对"
    job.finished_at = protocol._now()
    center.commit()
    pending = _add_job(center, job_type="deploy", key="pending-key")

    data = _poll(center, _node(center))
    assert data["job"] is None
    assert data["blocked_job"]["id"] == str(job.id)
    assert "unknown" in data["message"]
    assert pending.status == "pending"


def test_expired_lease_becomes_unknown_without_rerun(center):
    job, _data, _attempt, _token = _lease(center)
    job.lease_expires_at = protocol._now() - timedelta(seconds=5)
    center.commit()
    data = _poll(center, _node(center))
    assert data["job"] is None
    assert job.status == "unknown"
    assert job.error
    # unknown 不会被再次派发
    data2 = _poll(center, _node(center))
    assert data2["job"] is None


# --------------------------------------------------------------------------- 任务心跳
def test_job_heartbeat_renews_lease_and_rejects_bad_token(center):
    job, _data, attempt, token = _lease(center)
    job.lease_expires_at = protocol._now() + timedelta(seconds=10)
    center.commit()
    result = protocol.job_heartbeat(
        center, _node(center), job.id, JobHeartbeatIn(attempt_id=attempt, lease_token=token)
    )
    assert result["lease_seconds"] == 120
    assert result["status"] == "running"
    assert job.lease_expires_at > protocol._now() + timedelta(seconds=90)

    with pytest.raises(AppError) as exc_info:
        protocol.job_heartbeat(
            center, _node(center), job.id, JobHeartbeatIn(attempt_id=attempt, lease_token="bad")
        )
    assert exc_info.value.code == ErrorCode.CONFLICT
    assert exc_info.value.status_code == 409
    center.rollback()

    with pytest.raises(AppError):
        protocol.job_heartbeat(
            center, _node(center), job.id, JobHeartbeatIn(attempt_id="old-attempt", lease_token=token)
        )
    center.rollback()
    assert job.status == "running"


def test_job_heartbeat_expired_lease_marks_unknown_and_conflicts(center):
    job, _data, attempt, token = _lease(center)
    job.status = "running"
    job.lease_expires_at = protocol._now() - timedelta(seconds=1)
    center.commit()
    with pytest.raises(AppError) as exc_info:
        protocol.job_heartbeat(
            center, _node(center), job.id, JobHeartbeatIn(attempt_id=attempt, lease_token=token)
        )
    assert exc_info.value.code == ErrorCode.CONFLICT
    center.rollback()
    center.refresh(job)
    assert job.status == "unknown"


def test_job_heartbeat_allows_unknown_occupation(center):
    job, _data, attempt, token = _lease(center)
    job.status = "unknown"
    job.lease_expires_at = protocol._now() - timedelta(seconds=30)
    center.commit()
    result = protocol.job_heartbeat(
        center, _node(center), job.id, JobHeartbeatIn(attempt_id=attempt, lease_token=token, phase="unknown")
    )
    assert result["ok"] is True
    assert result["occupied"] is True
    assert job.lease_expires_at > protocol._now()


# --------------------------------------------------------------------------- 任务事件
def test_event_seq_idempotent_and_terminal_conflict(center):
    job, _data, attempt, token = _lease(center)
    first = protocol.job_event(
        center,
        _node(center),
        job.id,
        JobEventIn(
            status="running",
            step_no=1,
            step_name="构建",
            step_status="running",
            attempt_id=attempt,
            lease_token=token,
            event_seq=1,
        ),
    )
    assert first["duplicate"] is False
    assert job.status == "running"
    assert int(job.event_seq or 0) == 1

    duplicate = protocol.job_event(
        center,
        _node(center),
        job.id,
        JobEventIn(status="running", attempt_id=attempt, lease_token=token, event_seq=1),
    )
    assert duplicate["duplicate"] is True

    terminal = protocol.job_event(
        center,
        _node(center),
        job.id,
        JobEventIn(
            status="succeeded",
            result={"actual_version": "2.0.0"},
            attempt_id=attempt,
            lease_token=token,
            event_seq=2,
        ),
    )
    assert terminal["status"] == "succeeded"
    assert job.status == "succeeded"

    # 重复终态：幂等，不新增副作用
    repeated = protocol.job_event(
        center,
        _node(center),
        job.id,
        JobEventIn(
            status="succeeded",
            result={"actual_version": "2.0.0"},
            attempt_id=attempt,
            lease_token=token,
            event_seq=2,
        ),
    )
    assert repeated["duplicate"] is True

    with pytest.raises(AppError) as exc_info:
        protocol.job_event(
            center,
            _node(center),
            job.id,
            JobEventIn(status="failed", attempt_id=attempt, lease_token=token, event_seq=3),
        )
    assert exc_info.value.code == ErrorCode.CONFLICT
    center.rollback()
    center.refresh(job)
    assert job.status == "succeeded"


def test_event_rejects_old_attempt_and_wrong_lease(center):
    job, _data, attempt, token = _lease(center)
    with pytest.raises(AppError) as exc_info:
        protocol.job_event(
            center,
            _node(center),
            job.id,
            JobEventIn(status="running", attempt_id="old-attempt", event_seq=1),
        )
    assert exc_info.value.code == ErrorCode.CONFLICT
    center.rollback()
    center.refresh(job)
    assert job.status == "leased"

    with pytest.raises(AppError):
        protocol.job_event(
            center,
            _node(center),
            job.id,
            JobEventIn(status="running", attempt_id=attempt, lease_token="bad", event_seq=1),
        )
    center.rollback()
    assert job.status == "leased"


def test_progress_event_after_terminal_is_ignored(center):
    job, _data, attempt, token = _lease(center)
    protocol.job_event(
        center,
        _node(center),
        job.id,
        JobEventIn(status="succeeded", attempt_id=attempt, lease_token=token, event_seq=1),
    )
    result = protocol.job_event(
        center,
        _node(center),
        job.id,
        JobEventIn(status="running", step_no=2, step_name="上报", attempt_id=attempt, lease_token=token, event_seq=2),
    )
    assert result["duplicate"] is True
    assert job.status == "succeeded"


# --------------------------------------------------------------------------- 对账
def test_reconcile_unknown_marks_succeeded_without_rerun(center):
    job, _data, attempt, token = _lease(center)
    job.status = "unknown"
    job.error = "执行器重启，结果未知"
    job.finished_at = protocol._now()
    center.commit()
    result = protocol.reconcile_job(
        center,
        _node(center),
        job.id,
        JobReconcileIn(
            attempt_id=attempt,
            lease_token=token,
            local_status="succeeded",
            phase="finished",
            batch=1,
            result={"actual_version": "2.0.0"},
        ),
    )
    assert result["action"] == "marked_succeeded"
    assert result["server_status"] == "succeeded"
    assert result["retry_allowed"] is False
    assert job.status == "succeeded"
    assert job.result_json["reconcile"]["local_status"] == "succeeded"
    # 再次对账：不覆盖终态、不重跑
    again = protocol.reconcile_job(
        center, _node(center), job.id, JobReconcileIn(attempt_id=attempt, local_status="unknown")
    )
    assert again["action"] == "already_terminal"
    assert job.status == "succeeded"


def test_reconcile_active_expired_marks_unknown(center):
    job, _data, attempt, token = _lease(center)
    job.status = "running"
    job.lease_expires_at = protocol._now() - timedelta(seconds=5)
    center.commit()
    result = protocol.reconcile_job(
        center, _node(center), job.id, JobReconcileIn(attempt_id=attempt, local_status="running")
    )
    assert result["action"] == "marked_unknown"
    assert job.status == "unknown"


def test_reconcile_unknown_job_id_does_not_create_records(center):
    before = center.execute(select(func.count()).select_from(OperationJob)).scalar_one()
    result = protocol.reconcile_job(
        center, _node(center), 999, JobReconcileIn(attempt_id="x", local_status="succeeded")
    )
    assert result["known"] is False
    assert result["action"] == "ignored_unknown_task"
    after = center.execute(select(func.count()).select_from(OperationJob)).scalar_one()
    assert before == after


# --------------------------------------------------------------------------- 指标批量
def _sample(seq: int, **overrides):
    data = {
        "sampled_at": "2026-11-13T00:00:00Z",
        "hostname": "node-1",
        "cpu_cores": 4,
        "memory_total_mb": 8192,
        "disk_total_gb": 100,
        "cpu_percent": 10.0,
        "memory_percent": 20.0,
        "memory_used_mb": 1024,
        "memory_available_mb": 7168,
        "sample_seq": seq,
        "boot_id": "boot-1",
    }
    data.update(overrides)
    return data


def test_batch_telemetry_dedupes_by_boot_and_sample_seq(center):
    node = _node(center)
    payload = {
        "boot_id": "boot-1",
        "protocol_version": 2,
        "samples": [_sample(1), _sample(2)],
    }
    first = protocol.ingest_telemetry(center, node, payload)
    assert first["accepted"] == 2
    assert first["duplicates"] == 0
    assert first["results"][0]["status"] == "accepted"
    count = center.execute(select(func.count()).select_from(NodeMetricSample)).scalar_one()
    assert count == 2

    # 同一批重放：全部去重，不新增样本
    replay = protocol.ingest_telemetry(center, node, payload)
    assert replay["accepted"] == 0
    assert replay["duplicates"] == 2
    replay_count = center.execute(select(func.count()).select_from(NodeMetricSample)).scalar_one()
    assert replay_count == 2

    # 跨批次：seq2 重复、seq3 新增
    mixed = protocol.ingest_telemetry(
        center, node, {"boot_id": "boot-1", "samples": [_sample(2), _sample(3)]}
    )
    assert mixed["accepted"] == 1
    assert mixed["duplicates"] == 1
    mixed_count = center.execute(select(func.count()).select_from(NodeMetricSample)).scalar_one()
    assert mixed_count == 3


def test_batch_telemetry_marks_invalid_items_and_limits_batch(center):
    node = _node(center)
    result = protocol.ingest_telemetry(
        center,
        node,
        {"boot_id": "boot-1", "samples": [_sample(1, cpu_percent=999), _sample(2)]},
    )
    assert result["invalid"] == 1
    assert result["accepted"] == 1
    assert result["results"][0]["status"] == "invalid"
    assert result["results"][1]["status"] == "accepted"

    too_many = {
        "boot_id": "boot-1",
        "samples": [_sample(index + 10) for index in range(protocol.MAX_TELEMETRY_BATCH + 1)],
    }
    with pytest.raises(AppError) as exc_info:
        protocol.ingest_telemetry(center, node, too_many)
    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR


def test_legacy_single_telemetry_still_supported(center):
    node = _node(center)
    legacy = {
        "sampled_at": "2026-11-13T00:00:00Z",
        "hostname": "node-1",
        "cpu_cores": 4,
        "memory_total_mb": 8192,
        "disk_total_gb": 100,
        "cpu_percent": 12.5,
        "memory_percent": 20.0,
        "memory_used_mb": 1024,
        "memory_available_mb": 7168,
        "agent_version": "2.0.0",
        "app_version": "2.0.0",
    }
    result = protocol.ingest_telemetry(center, node, legacy)
    assert result["ok"] is True
    assert result["next_report_seconds"] == 30
    count = center.execute(select(func.count()).select_from(NodeMetricSample)).scalar_one()
    assert count == 1
    # 单条也支持 boot_id+sample_seq 幂等
    with_seq = dict(legacy, boot_id="boot-single", sample_seq=1)
    assert protocol.ingest_telemetry(center, node, with_seq)["accepted"] == 1
    duplicate = protocol.ingest_telemetry(center, node, with_seq)
    assert duplicate["duplicate"] is True
    assert duplicate["duplicates"] == 1


def test_low_level_attempt_helpers(center):
    job = _add_job(center, key="helper-key")
    assert protocol.attempt_state(job).attempt_id == ""
    protocol._set_attempt(job, attempt_id="a" * 32, lease_token="t" * 32, protocol=2)
    state = protocol.attempt_state(job)
    assert state.attempt_id == "a" * 32
    assert state.lease_token == "t" * 32
    assert protocol._agent_payload(job) == {}


# --------------------------------------------------------------------------- HTTP 路由
def test_http_agent_endpoints_require_token_and_return_contract(center):
    """真实路由：Bearer 认证、统一 success/error 包裹、409 冲突信封。"""
    from fastapi.testclient import TestClient

    import app.api.deps as deps
    import app.main as main_module

    application = main_module.app
    application.dependency_overrides[deps.get_center_db] = lambda: center
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    try:
        client = TestClient(application)
        unauthorized = client.post("/api/v1/agent/heartbeat", json={"protocol_version": 2})
        assert unauthorized.status_code == 401
        assert unauthorized.json()["error"]["code"] == "AUTH_SESSION_REQUIRED"

        heartbeat = client.post(
            "/api/v1/agent/heartbeat",
            json={"protocol_version": 2, "boot_id": "http-boot", "seq": 1},
            headers=headers,
        )
        assert heartbeat.status_code == 200
        assert heartbeat.json()["data"]["min_protocol_version"] == 2
        assert "request_id" in heartbeat.json()

        job, _data, attempt, _token = _lease(center)
        conflict = client.post(
            f"/api/v1/agent/jobs/{job.id}/heartbeat",
            json={"attempt_id": attempt, "lease_token": "wrong-token"},
            headers=headers,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "CONFLICT"
    finally:
        application.dependency_overrides.pop(deps.get_center_db, None)
