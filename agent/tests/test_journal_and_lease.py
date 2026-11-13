"""本地 SQLite 执行日志与租约语义测试（不依赖真实服务器）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentlib.constants import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
)
from agentlib.journal import ActiveJobExists, Journal


@pytest.fixture()
def journal(tmp_path: Path):
    db = Journal(tmp_path / "agent.db", lease_seconds=120)
    try:
        yield db
    finally:
        db.close()


def test_single_active_lease(journal: Journal) -> None:
    journal.start_attempt(task_id="1", attempt_id="a1", job_type="restart", lease_token="lt1")
    with pytest.raises(ActiveJobExists):
        journal.start_attempt(task_id="2", attempt_id="a2", job_type="backup")


def test_event_seq_monotonic_and_result_replay_keeps_seq(journal: Journal) -> None:
    journal.start_attempt(task_id="9", attempt_id="a9", job_type="upgrade", lease_token="lt")
    seqs = [journal.next_event_seq("9", "a9") for _ in range(3)]
    assert seqs == [1, 2, 3]
    result = journal.persist_result(
        task_id="9", attempt_id="a9", status=STATUS_SUCCEEDED, result={"ok": True}, lease_token="lt"
    )
    assert result["event_seq"] == 4
    # 重复写入：终态幂等，保留首次内容与事件序号
    again = journal.persist_result(
        task_id="9", attempt_id="a9", status=STATUS_FAILED, result={"ok": False}
    )
    assert again["status"] == STATUS_SUCCEEDED
    assert again["event_seq"] == 4
    # 覆盖结果（人工核对）后重放仍使用同一事件序号
    journal.override_result(task_id="9", status=STATUS_FAILED, error="人工核对为失败")
    pending = journal.pending_results(ignore_backoff=True)
    assert pending[0]["event_seq"] == 4 and pending[0]["status"] == STATUS_FAILED


def test_unknown_occupies_and_blocks_new_tasks(journal: Journal) -> None:
    journal.start_attempt(task_id="21", attempt_id="b1", job_type="upgrade", lease_token="lt")
    journal.mark_unknown(task_id="21", attempt_id="b1", reason="执行器重启")
    assert journal.has_unknown() is True
    with pytest.raises(ActiveJobExists):
        journal.start_attempt(task_id="22", attempt_id="b2", job_type="restart")
    journal.reconcile(task_id="21", note="人工确认")
    assert journal.active_lease() is None
    # reconcile 只解除占用、保留历史；旧任务不会被本地自动重试
    assert journal.get_lease("21")["phase"] == "reconciled"


def test_recover_interrupted_marks_unknown(journal: Journal) -> None:
    journal.start_attempt(task_id="31", attempt_id="c1", job_type="deploy", lease_token="lt")
    recovered = journal.recover_interrupted()
    assert recovered is not None and recovered["phase"] == STATUS_UNKNOWN
    assert "重启" in (recovered.get("unknown_reason") or "")


def test_phase_and_subprocess_logs(journal: Journal) -> None:
    journal.start_attempt(task_id="41", attempt_id="d1", job_type="logs")
    journal.record_phase(
        task_id="41",
        attempt_id="d1",
        phase="step",
        step_no=1,
        step_name="读取日志",
        status="succeeded",
        message="ok",
        event_seq=1,
    )
    journal.record_subprocess(
        task_id="41",
        attempt_id="d1",
        phase="logs:api",
        command=["docker", "compose", "logs", "--tail", "20", "api"],
        pid=1234,
        exit_code=0,
        output_tail="服务正常",
    )
    phases = journal.list_phases("41")
    subprocesses = journal.list_subprocesses("41")
    assert phases[0]["event_seq"] == 1
    assert subprocesses[0]["exit_code"] == 0 and "docker" in subprocesses[0]["command_json"]


def test_result_report_backoff(journal: Journal) -> None:
    journal.start_attempt(task_id="51", attempt_id="e1", job_type="backup")
    row = journal.persist_result(
        task_id="51", attempt_id="e1", status=STATUS_FAILED, error="boom", lease_token="lt"
    )
    attempts = journal.mark_result_report_failed(int(row["id"]), "network down", backoff_seconds=5)
    assert attempts == 1
    # 退避期内不立即重试，忽略退避可以立即重试
    assert journal.pending_results() == []
    assert len(journal.pending_results(ignore_backoff=True)) == 1


def test_lease_expiry_detection(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db = Journal(
        tmp_path / "expire.db",
        lease_seconds=120,
        now=lambda: fixed,
    )
    try:
        db.start_attempt(task_id="61", attempt_id="f1", job_type="restart")
        assert db.lease_expired() is False
        db._now = lambda: fixed + timedelta(seconds=121)  # type: ignore[attr-defined]
        assert db.lease_expired() is True
    finally:
        db.close()
