"""批次2：Worker / Scheduler / Outbox 实例绑定测试（不连接 MySQL / Redis）。

覆盖：

* 固定 ``instance_id`` / ``tenant_id`` 绑定来自进程环境，任务消息不可覆盖；
* 租户 active、实例一致、未独立部署 / 未迁移才允许调度与执行；
* 负载伪造（tenant/instance 不一致）拒绝投递并写审计；
* Scheduler / Outbox 跳过不可调度租户；执行前拒绝会置任务 failed 并审计；
* 任务创建、重试、Outbox 投递幂等（不重复生成任务 / 不重复投递）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.errors import AppError, ErrorCode
from app.modules.tasks import instance
from app.modules.tasks.instance import (
    BindingConfigError,
    TenantDeployment,
    WorkerBinding,
    evaluate_dispatch,
    filter_dispatchable_tenants,
    load_worker_binding,
    payload_binding_conflict,
)


# --------------------------------------------------------------------------- 绑定解析
class TestWorkerBinding:
    def test_default_center_is_unbound(self) -> None:
        binding = load_worker_binding(env={}, deployment_mode="center")
        assert binding.instance_id is None and binding.tenant_id is None
        assert binding.strict is False and binding.bound is False

    def test_tenant_mode_requires_tenant(self) -> None:
        with pytest.raises(BindingConfigError):
            load_worker_binding(env={}, deployment_mode="tenant")
        with pytest.raises(BindingConfigError):
            load_worker_binding(env={"ERP_WORKER_STRICT_BINDING": "true"}, deployment_mode="center")

    def test_env_binding_parsed(self) -> None:
        binding = load_worker_binding(
            env={
                "ERP_WORKER_INSTANCE_ID": "12",
                "ERP_WORKER_TENANT_ID": "3",
                "ERP_WORKER_STRICT_BINDING": "1",
            },
            deployment_mode="tenant",
        )
        assert binding.describe() == {
            "instance_id": 12,
            "tenant_id": 3,
            "deployment_mode": "tenant",
            "strict": True,
        }

    def test_alias_env_names(self) -> None:
        binding = load_worker_binding(
            env={"WORKER_INSTANCE_ID": "9", "WORKER_TENANT_ID": "4"},
            deployment_mode="tenant",
        )
        assert (binding.instance_id, binding.tenant_id) == (9, 4)

    @pytest.mark.parametrize(
        "env",
        [
            {"ERP_WORKER_INSTANCE_ID": "abc"},
            {"ERP_WORKER_INSTANCE_ID": "0"},
            {"ERP_WORKER_INSTANCE_ID": "-1"},
            {"ERP_WORKER_STRICT_BINDING": "maybe"},
        ],
    )
    def test_invalid_env_rejected(self, env: dict[str, str]) -> None:
        with pytest.raises(BindingConfigError):
            load_worker_binding(env=env, deployment_mode="center")

    def test_unknown_deployment_mode(self) -> None:
        with pytest.raises(BindingConfigError):
            load_worker_binding(env={}, deployment_mode="edge")

    def test_current_binding_uses_contextvar(self, monkeypatch) -> None:
        instance.reset_binding_cache()
        monkeypatch.setenv("ERP_WORKER_INSTANCE_ID", "5")
        monkeypatch.setenv("ERP_WORKER_TENANT_ID", "6")
        monkeypatch.setenv("ERP_WORKER_STRICT_BINDING", "true")
        try:
            binding = instance.current_binding()
            assert (binding.instance_id, binding.tenant_id, binding.strict) == (5, 6, True)
        finally:
            instance.reset_binding_cache()


def _deployment(**overrides: Any) -> TenantDeployment:
    data = {
        "tenant_id": 7,
        "tenant_status": "active",
        "instance_id": 2,
        "deployment_mode": "shared",
        "migration_status": "registered",
        "source": "center",
    }
    data.update(overrides)
    return TenantDeployment(**data)


CENTER_BINDING = WorkerBinding(instance_id=None, tenant_id=None, deployment_mode="center", strict=False)
INSTANCE_BINDING = WorkerBinding(instance_id=2, tenant_id=None, deployment_mode="center", strict=True)
TENANT_BINDING = WorkerBinding(instance_id=2, tenant_id=7, deployment_mode="tenant", strict=True)


# --------------------------------------------------------------------------- 判定矩阵
class TestDispatchDecision:
    def test_center_worker_allows_active_shared_tenant(self) -> None:
        decision = evaluate_dispatch(CENTER_BINDING, _deployment())
        assert decision.allowed is True and decision.reason == ""

    def test_legacy_center_fields_allowed(self) -> None:
        decision = evaluate_dispatch(
            CENTER_BINDING,
            _deployment(deployment_mode="unknown", migration_status="unknown", instance_id=None),
        )
        assert decision.allowed is True

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            ("provisioning", "tenant_not_active"),
            ("suspended", "tenant_not_active"),
            ("provision_failed", "tenant_not_active"),
        ],
    )
    def test_inactive_tenant_rejected_with_audit(self, status: str, reason: str) -> None:
        decision = evaluate_dispatch(CENTER_BINDING, _deployment(tenant_status=status))
        assert decision.allowed is False and decision.reason == reason
        assert decision.audit_required is True

    def test_missing_tenant_rejected(self) -> None:
        decision = evaluate_dispatch(CENTER_BINDING, TenantDeployment(tenant_id=7))
        assert decision.reason == "tenant_not_found" and decision.audit_required is True

    def test_tenant_binding_mismatch(self) -> None:
        decision = evaluate_dispatch(TENANT_BINDING, _deployment(tenant_id=8))
        assert decision.reason == "tenant_binding_mismatch" and decision.audit_required is True

    def test_instance_binding_mismatch(self) -> None:
        decision = evaluate_dispatch(INSTANCE_BINDING, _deployment(instance_id=99))
        assert decision.reason == "instance_binding_mismatch" and decision.audit_required is True

    def test_strict_binding_requires_instance_registration(self) -> None:
        decision = evaluate_dispatch(INSTANCE_BINDING, _deployment(instance_id=None))
        assert decision.reason == "tenant_instance_unbound" and decision.audit_required is True
        # 非严格中心 Worker 允许没有实例登记的租户
        assert evaluate_dispatch(CENTER_BINDING, _deployment(instance_id=None)).allowed is True

    @pytest.mark.parametrize("mode", ["independent"])
    def test_independent_deployment_not_scheduled(self, mode: str) -> None:
        decision = evaluate_dispatch(CENTER_BINDING, _deployment(deployment_mode=mode))
        assert decision.reason == "tenant_independent_deployment"
        assert decision.audit_required is False

    @pytest.mark.parametrize("migration", ["migrating", "migrated", "rollback"])
    def test_migrating_tenant_not_scheduled(self, migration: str) -> None:
        decision = evaluate_dispatch(CENTER_BINDING, _deployment(migration_status=migration))
        assert decision.reason == "tenant_migrating"
        assert decision.audit_required is False

    def test_tenant_worker_serves_only_own_tenant(self) -> None:
        assert evaluate_dispatch(TENANT_BINDING, _deployment(tenant_id=7)).allowed is True
        assert evaluate_dispatch(TENANT_BINDING, _deployment(tenant_id=8)).allowed is False


class TestFilterDispatchableTenants:
    def test_filter_splits_allowed_and_skipped(self) -> None:
        deployments = {
            1: _deployment(tenant_id=1),
            2: _deployment(tenant_id=2, migration_status="migrated"),
            3: _deployment(tenant_id=3, deployment_mode="independent"),
            4: _deployment(tenant_id=4, tenant_status="suspended"),
        }
        allowed, skipped = filter_dispatchable_tenants(
            [1, 2, 3, 4], binding=CENTER_BINDING, loader=lambda tenant_id: deployments[tenant_id]
        )
        assert allowed == [1]
        reasons = {item["tenant_id"]: item["reason"] for item in skipped}
        assert reasons == {
            2: "tenant_migrating",
            3: "tenant_independent_deployment",
            4: "tenant_not_active",
        }

    def test_loader_failure_skips_tenant(self) -> None:
        def loader(tenant_id: int) -> TenantDeployment:
            raise RuntimeError("center unavailable")

        allowed, skipped = filter_dispatchable_tenants(
            [1], binding=CENTER_BINDING, loader=loader
        )
        assert allowed == [] and skipped[0]["reason"].startswith("load_failed")


# --------------------------------------------------------------------------- 负载伪造
class TestPayloadBinding:
    def test_missing_or_matching_payload(self) -> None:
        assert payload_binding_conflict(None, tenant_id=7) is None
        assert payload_binding_conflict({}, tenant_id=7) is None
        assert payload_binding_conflict({"tenant_id": 7}, tenant_id=7) is None
        assert payload_binding_conflict({"tenant_id": "7"}, tenant_id=7) is None

    def test_tenant_override_rejected(self) -> None:
        assert (
            payload_binding_conflict({"tenant_id": 8}, tenant_id=7)
            == "payload_tenant_mismatch"
        )
        assert (
            payload_binding_conflict({"tenant_id": "abc"}, tenant_id=7)
            == "payload_tenant_invalid"
        )

    def test_instance_override_rules(self) -> None:
        binding = WorkerBinding(instance_id=2, tenant_id=None, deployment_mode="center", strict=True)
        assert (
            payload_binding_conflict({"instance_id": 2}, tenant_id=7, binding=binding) is None
        )
        assert (
            payload_binding_conflict({"instance_id": 3}, tenant_id=7, binding=binding)
            == "payload_instance_mismatch"
        )
        assert (
            payload_binding_conflict({"instance_id": 3}, tenant_id=7, binding=CENTER_BINDING)
            == "payload_instance_not_allowed"
        )

    def test_authoritative_tenant_ignores_payload(self) -> None:
        assert instance.authoritative_tenant({"tenant_id": 99}, 7) == 7


# --------------------------------------------------------------------------- 投递解析
class TestResolveTargetTenant:
    def test_payload_cannot_override_sync_run_tenant(self) -> None:
        from app.modules.tasks.outbox import resolve_target

        _task, queue, kwargs = resolve_target(
            "sync_run.created",
            {
                "sync_run_id": 5,
                "tenant_id": 99,
                "job_type": "order_sync",
                "store_id": 3,
                "triggered_by": "schedule",
            },
            tenant_id=7,
        )
        assert kwargs["tenant_id"] == 7  # 数据库事件行的租户是权威值
        assert queue == "sales"

    def test_payload_cannot_override_stock_push_tenant(self) -> None:
        from app.modules.tasks.outbox import resolve_target

        _task, _queue, kwargs = resolve_target(
            "stock.push.requested",
            {"tenant_id": 99, "store_id": 4, "sku_ids": [11]},
            tenant_id=7,
        )
        assert kwargs["tenant_id"] == 7 and kwargs["sku_id"] == 11

    def test_payload_cannot_override_normalize_tenant(self) -> None:
        from app.modules.tasks.outbox import resolve_target

        _task, _queue, kwargs = resolve_target(
            "order.normalize", {"tenant_id": 99, "raw_id": 3}, tenant_id=7
        )
        assert kwargs["tenant_id"] == 7
        assert "instance_id" not in kwargs


# --------------------------------------------------------------------------- Outbox 投递
class _FakeEvent:
    def __init__(self, *, event_id: int, tenant_id: int, payload: dict[str, Any]) -> None:
        self.id = event_id
        self.tenant_id = tenant_id
        self.event_type = "sync_run.created"
        self.payload = payload
        self.status = "pending"
        self.attempts = 0
        self.last_error = ""
        self.available_at = None
        self.dispatched_at = None


class _FakeDb:
    def __init__(self) -> None:
        self.flushed = 0

    def flush(self) -> None:
        self.flushed += 1

    def commit(self) -> None:
        self.flushed += 1


class TestOutboxDispatchBinding:
    def _patch(self, monkeypatch, events: list[_FakeEvent]) -> dict[str, Any]:
        from app.modules.tasks import outbox, repository
        from app.modules.tasks import service as task_service

        sent: list[dict[str, Any]] = []
        audited: list[dict[str, Any]] = []
        monkeypatch.setattr(repository, "pending_outbox_events", lambda *a, **k: events)
        monkeypatch.setattr(task_service, "job_enabled", lambda *a, **k: True)
        monkeypatch.setattr(
            outbox, "_celery_send", lambda task, queue, kwargs: sent.append(kwargs)
        )
        monkeypatch.setattr(
            instance,
            "audit_rejection",
            lambda tenant_id, **kwargs: audited.append({"tenant_id": tenant_id, **kwargs}),
        )
        return {"sent": sent, "audited": audited}

    def test_tampered_payload_rejected_and_audited(self, monkeypatch) -> None:
        from app.modules.tasks import outbox

        event = _FakeEvent(
            event_id=1,
            tenant_id=7,
            payload={"sync_run_id": 3, "tenant_id": 8, "job_type": "order_sync"},
        )
        captured = self._patch(monkeypatch, [event])
        summary = outbox.dispatch_tenant_batch(
            _FakeDb(), 7, limit=10, binding=CENTER_BINDING
        )
        assert summary["rejected"] == 1 and summary["dispatched"] == 0
        assert captured["sent"] == []
        assert event.status == "failed" and "冲突" in event.last_error
        assert captured["audited"] and captured["audited"][0]["action"] == "task.outbox_rejected"

    def test_valid_event_uses_authoritative_tenant(self, monkeypatch) -> None:
        from app.modules.tasks import outbox

        event = _FakeEvent(
            event_id=2,
            tenant_id=7,
            payload={"sync_run_id": 3, "tenant_id": 7, "job_type": "order_sync", "store_id": 1},
        )
        captured = self._patch(monkeypatch, [event])
        summary = outbox.dispatch_tenant_batch(
            _FakeDb(), 7, limit=10, binding=CENTER_BINDING
        )
        assert summary["dispatched"] == 1
        assert captured["sent"][0]["tenant_id"] == 7
        assert event.status == "dispatched"

    def test_redelivery_is_idempotent(self, monkeypatch) -> None:
        """同一事件重复扫描不会重复投递（第二次已不是 pending）。"""
        from app.modules.tasks import outbox, repository

        event = _FakeEvent(
            event_id=3,
            tenant_id=7,
            payload={"sync_run_id": 4, "tenant_id": 7, "job_type": "order_sync"},
        )
        captured = self._patch(monkeypatch, [event])
        outbox.dispatch_tenant_batch(_FakeDb(), 7, limit=10, binding=CENTER_BINDING)
        # 模拟数据库：已投递的事件不再出现在待投递列表
        monkeypatch.setattr(repository, "pending_outbox_events", lambda *a, **k: [])
        summary = outbox.dispatch_tenant_batch(_FakeDb(), 7, limit=10, binding=CENTER_BINDING)
        assert summary["scanned"] == 0 and len(captured["sent"]) == 1

    def test_dispatch_pending_skips_migrated_tenants(self, monkeypatch) -> None:
        from app.modules.tasks import outbox

        monkeypatch.setattr(outbox, "active_tenant_ids", lambda: [1, 2, 3])
        monkeypatch.setattr(
            instance,
            "filter_dispatchable_tenants",
            lambda tenant_ids, binding=None: ([1], [{"tenant_id": 2, "reason": "tenant_migrating"}]),
        )
        processed: list[int] = []
        monkeypatch.setattr(
            outbox,
            "dispatch_tenant_batch",
            lambda db, tenant_id, limit, binding=None: (
                processed.append(tenant_id)
                or {"scanned": 1, "dispatched": 1, "failed": 0, "rejected": 0}
            ),
        )
        monkeypatch.setattr(outbox, "load_optional_route_providers", lambda: None)
        monkeypatch.setattr(outbox, "center_session", lambda: _null_context(_FakeCenter()))
        monkeypatch.setattr(
            outbox, "load_tenant_route", lambda center, tenant_id: SimpleNamespace(tenant_id=tenant_id)
        )
        monkeypatch.setattr(outbox, "tenant_session", lambda route: _null_context(_FakeDb()))
        summary = outbox.dispatch_pending(binding=CENTER_BINDING)
        assert processed == [1]
        assert summary["dispatched"] == 1
        assert summary["skipped_tenants"] == 1


class _FakeCenter:
    pass


class _null_context:
    def __init__(self, value: Any) -> None:
        self.value = value

    def __enter__(self) -> Any:
        return self.value

    def __exit__(self, *exc: Any) -> bool:
        return False


# --------------------------------------------------------------------------- Scheduler
class TestSchedulerBinding:
    def test_run_once_only_schedules_bound_tenants(self, monkeypatch) -> None:
        from app.modules.tasks import scheduler

        monkeypatch.setattr(scheduler, "active_tenant_ids", lambda: [1, 2])
        monkeypatch.setattr(
            instance,
            "filter_dispatchable_tenants",
            lambda tenant_ids, binding=None: (
                [1],
                [
                    {"tenant_id": 2, "reason": "tenant_migrating"},
                    {"tenant_id": 3, "reason": "tenant_independent_deployment"},
                ],
            ),
        )
        monkeypatch.setattr(scheduler, "center_session", lambda: _null_context(_FakeCenter()))
        monkeypatch.setattr(
            scheduler, "load_tenant_route", lambda center, tenant_id: SimpleNamespace(tenant_id=tenant_id)
        )
        monkeypatch.setattr(scheduler, "tenant_session", lambda route: _null_context(_FakeDb()))
        monkeypatch.setattr(
            scheduler,
            "run_tenant_schedules",
            lambda db, tenant_id, now, limit: {"scanned": 2, "created": 1, "reused": 1},
        )
        monkeypatch.setattr(scheduler, "recover_stale_runs_for_tenant", lambda tenant_id: 0)
        monkeypatch.setattr(
            scheduler, "compensate_webhook_events_for_tenant", lambda tenant_id: (0, 0)
        )
        summary = scheduler.run_once(binding=CENTER_BINDING)
        assert summary["tenants"] == 1
        assert summary["skipped_tenants"] == 2
        assert summary["created"] == 1 and summary["reused"] == 1


# --------------------------------------------------------------------------- 执行前拒绝
class TestTaskExecutionGuard:
    def test_rejection_marks_failed_and_audits(self, monkeypatch) -> None:
        from app.modules.tasks import tasks

        marked: list[tuple[int, int, str]] = []
        audited: list[dict[str, Any]] = []

        def reject(tenant_id: int, *, purpose: str = "task", center=None):
            raise AppError(
                ErrorCode.TENANT_FORBIDDEN,
                "租户不满足本实例调度条件",
                details={"reason": "instance_binding_mismatch"},
            )

        monkeypatch.setattr(instance, "ensure_dispatch_allowed", reject)
        monkeypatch.setattr(
            tasks, "_mark_run_rejected", lambda tenant_id, run_id, reason: marked.append((tenant_id, run_id, reason))
        )
        monkeypatch.setattr(
            instance, "audit_rejection", lambda tenant_id, **kwargs: audited.append({"tenant_id": tenant_id, **kwargs})
        )
        result = tasks.guard_task_execution(7, 21)
        assert result is not None and result["status"] == "rejected"
        assert result["reason"] == "instance_binding_mismatch"
        assert marked == [(7, 21, "instance_binding_mismatch")]
        assert audited and audited[0]["action"] == "task.execution_rejected"

    def test_allowed_task_passes_guard(self, monkeypatch) -> None:
        from app.modules.tasks import tasks

        monkeypatch.setattr(
            instance,
            "ensure_dispatch_allowed",
            lambda tenant_id, purpose="task", center=None: SimpleNamespace(allowed=True),
        )
        assert tasks.guard_task_execution(7, 21) is None

    def test_message_tenant_cannot_override_run_tenant(self, monkeypatch) -> None:
        from app.modules.tasks import tasks

        rejected: list[tuple[int, int, str]] = []
        audited: list[dict[str, Any]] = []
        run = SimpleNamespace(
            id=31,
            tenant_id=8,  # 任务记录真实租户
            status="pending",
            job_type="order_sync",
            requested_by=None,
            store_id=None,
            triggered_by="schedule",
        )
        monkeypatch.setattr(tasks, "_begin_run", lambda tenant_id, run_id: run)
        monkeypatch.setattr(
            tasks, "_mark_run_rejected", lambda tenant_id, run_id, reason: rejected.append((tenant_id, run_id, reason))
        )
        monkeypatch.setattr(
            instance, "audit_rejection", lambda tenant_id, **kwargs: audited.append({"tenant_id": tenant_id, **kwargs})
        )
        result = tasks._execute_sync_run_impl(
            SimpleNamespace(request=SimpleNamespace(retries=0), max_retries=3),
            sync_run_id=31,
            tenant_id=7,  # 消息里伪造的租户
            job_type="order_sync",
        )
        assert result["status"] == "rejected" and result["reason"] == "task_tenant_mismatch"
        assert rejected == [(8, 31, "task_tenant_mismatch")]
        assert audited and audited[0]["tenant_id"] == 8

    def test_handler_kwargs_uses_authoritative_tenant(self) -> None:
        from app.modules.tasks import tasks

        kwargs = tasks.handler_kwargs(
            "order_sync",
            tenant_id=8,
            sync_run_id=1,
            store_id=2,
            requested_by=None,
            triggered_by="schedule",
        )
        assert kwargs["tenant_id"] == 8


# --------------------------------------------------------------------------- 幂等
def _sqlite_task_session():
    """内存 SQLite：只建 sync_runs + outbox_events，验证任务创建幂等（不连 MySQL）。"""
    from sqlalchemy import Integer, MetaData, create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import TenantBase

    copy = MetaData()
    for name in ("sync_runs", "outbox_events"):
        TenantBase.metadata.tables[name].to_metadata(copy)
    for table in copy.tables.values():
        table.c.id.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    copy.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)(), engine


class TestTaskIdempotency:
    def _patch_service(self, monkeypatch) -> None:
        from app.modules.tasks import service as task_service

        monkeypatch.setattr(task_service, "job_enabled", lambda *a, **k: True)
        monkeypatch.setattr(task_service, "ensure_job_enabled", lambda *a, **k: None)
        monkeypatch.setattr(task_service, "record_audit", lambda *a, **k: None)

    def test_request_sync_run_reuses_pending(self, monkeypatch) -> None:
        from sqlalchemy import func, select

        from app.modules.tasks import service as task_service
        from app.modules.tasks.models_tenant import OutboxEvent, SyncRun

        self._patch_service(monkeypatch)
        db, engine = _sqlite_task_session()
        try:
            first = task_service.request_sync_run(
                db, tenant_id=1, job_type="order_sync", store_id=2, triggered_by="schedule"
            )
            second = task_service.request_sync_run(
                db, tenant_id=1, job_type="order_sync", store_id=2, triggered_by="schedule"
            )
            db.commit()
            assert first == second  # 同一目标已有 pending 任务，复用
            assert db.execute(select(func.count()).select_from(SyncRun)).scalar_one() == 1
            assert db.execute(select(func.count()).select_from(OutboxEvent)).scalar_one() == 1
        finally:
            db.close()
            engine.dispose()

    def test_retry_creates_new_run_but_keeps_single_pending_event(self, monkeypatch) -> None:
        from sqlalchemy import func, select

        from app.modules.tasks import service as task_service
        from app.modules.tasks.models_tenant import OutboxEvent, SyncRun

        self._patch_service(monkeypatch)
        db, engine = _sqlite_task_session()
        try:
            run_id = task_service.request_sync_run(
                db,
                tenant_id=1,
                job_type="order_sync",
                store_id=2,
                triggered_by="manual",
                requested_by=9,
            )
            db.commit()
            run = db.get(SyncRun, run_id)
            task_service.mark_run_finished(
                db, run, status=task_service.RUN_FAILED, error_message="boom"
            )
            db.commit()
            new_run_id = task_service.retry_sync_run(db, tenant_id=1, run_id=run_id, actor_id=9)
            db.commit()
            assert new_run_id != run_id
            assert db.execute(select(func.count()).select_from(SyncRun)).scalar_one() == 2
            # 重试只产生一个新的待投递事件（旧事件已处理/失败不在 pending 复用之列）
            pending = db.execute(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "pending")
            ).scalar_one()
            assert pending == 2  # 手动任务原始事件 + 重试事件，均为一次性记录
        finally:
            db.close()
            engine.dispose()
