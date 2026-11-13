"""客户端协议对齐测试：心跳/续租/poll/events/reconcile/批量指标。

全部使用假 transport 与本地临时目录，不连接真实服务器、不执行 docker/systemd。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentlib.constants import (
    PROTOCOL_VERSION,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
)
from agentlib.heartbeat import HeartbeatSender, JobLeaseRenewer
from agentlib.httpc import CenterClient, NetworkError
from agentlib.jobs import JobRunner
from agentlib.journal import Journal
from agentlib.telemetry import TelemetryBatchSender, TelemetryCache, TelemetryReporter


class FakeTransport:
    """按路径匹配的假 transport；handler 可以是 (status, body)、异常或可调用对象。"""

    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self.handlers = handlers or {}
        self.calls: list[dict[str, Any]] = []

    def request(self, method, url, *, headers, body, timeout):  # noqa: ANN001
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "payload": payload, "headers": headers})
        for suffix, response in self.handlers.items():
            if url.endswith(suffix):
                if callable(response) and not isinstance(response, tuple):
                    return response(payload)
                if isinstance(response, Exception):
                    raise response
                return response
        return 200, b'{"success": true, "data": {}}'

    def payloads(self, suffix: str) -> list[dict[str, Any]]:
        return [
            call["payload"]
            for call in self.calls
            if call["url"].endswith(suffix) and call["payload"] is not None
        ]


def _client(transport: FakeTransport) -> CenterClient:
    return CenterClient("https://center.invalid", "node-token", transport=transport)


@pytest.fixture()
def journal(tmp_path: Path):
    db = Journal(tmp_path / "agent.db", lease_seconds=120)
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- 心跳
def test_heartbeat_payload_and_server_timings() -> None:
    transport = FakeTransport(
        {
            "/agent/heartbeat": (
                200,
                json.dumps(
                    {
                        "data": {
                            "server_time": "2026-01-01T00:00:00Z",
                            "protocol_version": 2,
                            "min_protocol_version": 2,
                            "upgrade_required": False,
                            "mutation_allowed": True,
                            "heartbeat_seconds": 45,
                            "job_lease_seconds": 120,
                            "job_poll_seconds": 5,
                            "telemetry_seconds": 30,
                            "seq_ack": 1,
                        }
                    }
                ).encode(),
            )
        }
    )
    sender = HeartbeatSender(
        _client(transport),
        role="collector",
        config={"deploy_dir": "/nonexistent"},
        boot_id="boot-1",
        state_provider=lambda: {"executor": {"busy": False}},
    )
    result = sender.send_once()
    assert result["ok"] is True and result["supported"] is True
    assert sender.interval == 45  # 采用服务端心跳间隔
    assert sender.remote["mutation_allowed"] is True
    payload = transport.payloads("/agent/heartbeat")[0]
    for key in ("agent_version", "executor_version", "protocol_version", "capabilities", "boot_id", "seq", "role", "state"):
        assert key in payload
    assert payload["protocol_version"] == PROTOCOL_VERSION


def test_heartbeat_endpoint_unsupported_degrades() -> None:
    transport = FakeTransport({"/agent/heartbeat": (404, b'{"detail": "Not Found"}')})
    sender = HeartbeatSender(_client(transport), boot_id="b")
    result = sender.send_once()
    assert result["ok"] is False and result["supported"] is False


# --------------------------------------------------------------------------- 续租
def test_lease_renewal_success_and_409_lease_lost(journal: Journal) -> None:
    journal.start_attempt(task_id="1", attempt_id="a1", job_type="restart", lease_token="lt")
    transport = FakeTransport(
        {
            "/jobs/1/heartbeat": (
                200,
                json.dumps(
                    {"data": {"ok": True, "lease_expires_at": "2026-01-01T00:02:00Z", "lease_lost": False}}
                ).encode(),
            )
        }
    )
    renewer = JobLeaseRenewer(_client(transport), journal, task_id="1", attempt_id="a1", lease_token="lt")
    result = renewer.renew_once()
    assert result["ok"] is True and result["lease_lost"] is False
    payload = transport.payloads("/jobs/1/heartbeat")[0]
    assert payload["attempt_id"] == "a1" and payload["lease_token"] == "lt"
    assert payload["lease_seconds"] == 120

    transport.handlers["/jobs/1/heartbeat"] = (
        409,
        b'{"error": {"code": "CONFLICT", "message": "attempt mismatch"}}',
    )
    lost = renewer.renew_once()
    assert lost["lease_lost"] is True
    assert journal.active_lease()["phase"] == STATUS_UNKNOWN
    # 判定丢失后不再续租，避免对终态任务反复请求
    before = len(transport.calls)
    renewer.renew_once()
    assert len(transport.calls) == before


def test_lease_renewal_network_failures_mark_unknown(journal: Journal) -> None:
    journal.start_attempt(task_id="2", attempt_id="b1", job_type="backup", lease_token="lt")
    transport = FakeTransport({"/jobs/2/heartbeat": NetworkError("offline")})
    renewer = JobLeaseRenewer(_client(transport), journal, task_id="2", attempt_id="b1", lease_token="lt")
    for _ in range(3):
        renewer.renew_once()
    assert renewer.lease_lost is True
    assert journal.active_lease()["phase"] == STATUS_UNKNOWN


# --------------------------------------------------------------------------- 任务执行
def _job_payload(**overrides: Any) -> dict[str, Any]:
    job = {
        "id": "31",
        "job_type": "backup",
        "payload": {},
        "attempts": 1,
        "attempt_id": "attempt-1",
        "lease_token": "lease-1",
        "lease_seconds": 120,
        "min_agent_protocol": 2,
    }
    job.update(overrides)
    return job


def test_execute_persists_before_report_and_event_seq(journal: Journal) -> None:
    transport = FakeTransport(
        {
            "/jobs/poll": (200, json.dumps({"data": {"job": _job_payload()}}).encode()),
            "/jobs/31/events": NetworkError("offline"),
        }
    )
    runner = JobRunner(
        _client(transport), journal, {}, executor=lambda job, config, report, recorder: {"ok": True, "exit_code": 0, "checks": {"services_ready": "succeeded"}}
    )
    result = runner.run_once()
    assert result["status"] == STATUS_SUCCEEDED
    row = journal.result_of("31", "attempt-1")
    assert row is not None and row["reported_at"] is None
    assert row["event_seq"] >= 1
    # 阶段事件带 attempt_id/lease_token/event_seq
    step_events = transport.payloads("/jobs/31/events")
    assert step_events and all("attempt_id" in item and "event_seq" in item for item in step_events)
    # 网络恢复后补发终态事件，带 attempt_id/lease_token/event_seq/checks
    transport.handlers["/jobs/31/events"] = (200, b'{"success": true, "data": {"ok": true}}')
    summary = runner.flush_results(force=True)
    assert summary["sent"] == 1
    final = transport.payloads("/jobs/31/events")[-1]
    assert final["status"] == STATUS_SUCCEEDED
    assert final["attempt_id"] == "attempt-1" and final["lease_token"] == "lease-1"
    assert final["event_seq"] == row["event_seq"]
    assert final["checks"] == {"services_ready": "succeeded"}


def test_result_report_409_is_dead_letter(journal: Journal) -> None:
    journal.start_attempt(task_id="32", attempt_id="a32", job_type="restart", lease_token="lt")
    journal.persist_result(
        task_id="32", attempt_id="a32", status=STATUS_FAILED, error="boom", lease_token="lt"
    )
    transport = FakeTransport(
        {"/jobs/32/events": (409, b'{"error": {"code": "CONFLICT", "message": "attempt mismatch"}}')}
    )
    runner = JobRunner(_client(transport), journal, {}, executor=lambda *a, **k: {"ok": True})
    summary = runner.flush_results(force=True)
    assert summary["dead_letter"] == 1
    # 死信后不再重试，且本地状态保持（不改写结果）
    assert journal.pending_results(ignore_backoff=True) == []
    assert journal.result_of("32", "a32")["status"] == STATUS_FAILED


def test_result_report_network_failure_is_retried_later(journal: Journal) -> None:
    journal.start_attempt(task_id="33", attempt_id="a33", job_type="backup", lease_token="lt")
    journal.persist_result(task_id="33", attempt_id="a33", status=STATUS_SUCCEEDED)
    transport = FakeTransport({"/jobs/33/events": NetworkError("offline")})
    runner = JobRunner(_client(transport), journal, {}, executor=lambda *a, **k: {"ok": True})
    assert runner.flush_results(force=True)["failed"] == 1
    # 退避期内不重复上报，强制补报可立即重试
    assert journal.pending_results() == []
    transport.handlers["/jobs/33/events"] = (200, b'{"success": true}')
    assert runner.flush_results(force=True)["sent"] == 1


def test_unknown_blocks_polling_and_reconcile_payload(journal: Journal) -> None:
    journal.start_attempt(task_id="34", attempt_id="a34", job_type="upgrade", lease_token="lt")
    journal.mark_unknown(task_id="34", attempt_id="a34", reason="执行器重启")
    transport = FakeTransport(
        {
            "/jobs/34/reconcile": (
                200,
                json.dumps({"data": {"known": True, "action": "marked_failed", "server_status": "failed"}}).encode(),
            )
        }
    )
    runner = JobRunner(_client(transport), journal, {}, executor=lambda *a, **k: {"ok": True})
    result = runner.run_once()
    assert result["status"] == "blocked_unknown"
    # unknown 期间不轮询新任务
    assert transport.payloads("/jobs/poll") == []
    # reconcile 负载包含 attempt_id/lease_token/local_status/phases
    runner.reconcile_remote(journal.active_lease(), local_status="unknown", note="核对")
    payload = transport.payloads("/jobs/34/reconcile")[0]
    assert payload["attempt_id"] == "a34" and payload["lease_token"] == "lt"
    assert payload["local_status"] == "unknown" and isinstance(payload["phases"], list)


def test_recover_on_start_uses_server_terminal_status(journal: Journal) -> None:
    journal.start_attempt(task_id="35", attempt_id="a35", job_type="upgrade", lease_token="lt")
    transport = FakeTransport(
        {
            "/jobs/35/reconcile": (
                200,
                json.dumps({"data": {"known": True, "action": "marked_succeeded", "server_status": "succeeded"}}).encode(),
            ),
            "/jobs/35/events": (200, b'{"success": true}'),
        }
    )
    runner = JobRunner(_client(transport), journal, {}, executor=lambda *a, **k: {"ok": True})
    recovered = runner.recover_on_start()
    assert recovered is not None
    assert journal.active_lease() is None  # 服务端已确认终态 → 解除占用
    assert journal.result_of("35")["status"] == STATUS_SUCCEEDED


# --------------------------------------------------------------------------- 批量指标
def test_telemetry_batch_dedupe_and_invalid(tmp_path: Path) -> None:
    transport = FakeTransport(
        {
            "/agent/telemetry": (
                200,
                json.dumps(
                    {
                        "data": {
                            "ok": True,
                            "accepted": 1,
                            "duplicates": 1,
                            "invalid": 1,
                            "results": [
                                {"index": 0, "status": "accepted"},
                                {"index": 1, "status": "duplicate"},
                                {"index": 2, "status": "invalid", "error": "bad sample"},
                            ],
                        }
                    }
                ).encode(),
            )
        }
    )
    sender = TelemetryBatchSender(_client(transport), boot_id="boot-9")
    samples = [
        {"sample_seq": 1, "boot_id": "boot-9"},
        {"sample_seq": 2, "boot_id": "boot-9"},
        {"sample_seq": 3, "boot_id": "boot-9"},
    ]
    result = sender.send_batch(samples)
    assert result == {"sent": 2, "invalid": 1, "unsupported": 0}
    payload = transport.payloads("/agent/telemetry")[0]
    assert payload["boot_id"] == "boot-9" and len(payload["samples"]) == 3


def test_telemetry_batch_falls_back_to_single(tmp_path: Path) -> None:
    """旧服务端不认识批量字段（422）时回落单条旧格式，避免指标丢失。"""

    def handler(payload: dict[str, Any]):
        if isinstance(payload, dict) and "samples" in payload:
            return 422, b'{"error": {"code": "VALIDATION_ERROR", "message": "extra fields"}}'
        return 200, b'{"success": true, "data": {"ok": true}}'

    transport = FakeTransport({"/agent/telemetry": handler})
    cache = TelemetryCache(tmp_path / "cache.json")
    reporter = TelemetryReporter(_client(transport), cache, boot_id="boot-10")
    reporter.enqueue({"sampled_at": "t1"})
    reporter.enqueue({"sampled_at": "t2"})
    summary = reporter.flush()
    assert summary["sent"] == 2 and reporter.queued == 0
    singles = [item for item in transport.payloads("/agent/telemetry") if "samples" not in item]
    assert len(singles) == 2


def test_telemetry_network_failure_keeps_order(tmp_path: Path) -> None:
    transport = FakeTransport({"/agent/telemetry": NetworkError("offline")})
    cache = TelemetryCache(tmp_path / "cache.json")
    reporter = TelemetryReporter(_client(transport), cache, boot_id="boot-11")
    reporter.enqueue({"sampled_at": "t1"})
    reporter.enqueue({"sampled_at": "t2"})
    summary = reporter.flush()
    assert summary["sent"] == 0 and reporter.queued == 2
    queued = cache.read()
    assert [item["sampled_at"] for item in queued] == ["t1", "t2"]
    assert all(item["boot_id"] == "boot-11" and item["sample_seq"] >= 1 for item in queued)


# --------------------------------------------------------------------------- 执行器命令
def test_executor_rejects_short_commit_without_git_calls() -> None:
    import executor

    calls: list[list[str]] = []

    class Runner:
        def run(self, args, cwd, *, timeout=300, env=None):  # noqa: ANN001
            calls.append([str(item) for item in args])
            return 0, ""

    result = executor.execute(
        {"job_type": "upgrade", "target_version": "abc1234", "payload": {"commit": "abc1234"}},
        {"deploy_dir": "/tmp", "env_file": ".env"},
        lambda *a, **k: None,
        runner=Runner(),
    )
    assert result["ok"] is False and result["error"] == "invalid_commit"
    assert calls == []  # 短 commit 直接拒绝，不触发任何 git/docker 命令


def test_executor_upgrade_uses_version_dir_not_reset(tmp_path: Path) -> None:
    import executor

    commit = "a" * 40
    (tmp_path / "deploy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "deploy" / "backup.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    calls: list[list[str]] = []

    class Runner:
        def run(self, args, cwd, *, timeout=300, env=None):  # noqa: ANN001
            command = [str(item) for item in args]
            calls.append(command)
            joined = " ".join(command)
            if "rev-parse" in joined:
                return 0, commit
            if "cat-file" in joined:
                return 0, ""
            if "worktree" in joined:
                return 0, ""
            if command[:2] == ["docker", "compose"] and "ps" in command:
                return 0, json.dumps([{"Service": "api", "State": "running"}, {"Service": "nginx", "State": "running"}])
            if "readyz" in joined:
                return 0, '{"ready":{"status":"ready"},"app":{"ok":true}}'
            return 0, ""

    result = executor.execute(
        {"job_type": "upgrade", "target_version": commit[:12], "payload": {"commit": commit}},
        {
            "deploy_dir": str(tmp_path / "deploy"),
            "env_file": ".env",
            "repo_dir": str(tmp_path / "repo"),
        },
        lambda *a, **k: None,
        runner=Runner(),
    )
    joined = [" ".join(item) for item in calls]
    assert any("worktree add" in item for item in joined)
    assert not any("reset --hard" in item for item in joined)
    assert result.get("ok") is True
    assert result.get("commit") == commit


def test_readiness_parser_accepts_long_ndjson() -> None:
    """docker compose ps 输出超过 4KB 时不能被截断，否则 readiness 误判。"""
    from agentlib.versions import _parse_ps, _service_ready

    rows = []
    for name in ("api", "nginx"):
        rows.append(
            '{"Service":"%s","Name":"ventasync-%s-1","State":"running","Health":"healthy","Labels":"%s"}'
            % (name, name, "x" * 1200)
        )
    statuses = _parse_ps("\n".join(rows))
    assert set(statuses) == {"api", "nginx"}
    assert all(_service_ready(statuses[name]) for name in ("api", "nginx"))
