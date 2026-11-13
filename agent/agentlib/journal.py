"""本地 SQLite 执行日志（仅标准库）。

持久化内容（Agent 2 可靠性协议）：

* ``job_leases``：task_id / attempt_id / lease_token / 阶段 / 租约到期时间 / 任务参数；
  **同一实例同时只允许一条活动租约**（含 unknown 占用），由唯一与事务约束保证；
* ``job_phases``：阶段日志（步骤、状态、消息、子进程 PID、命令、退出码）；
* ``subprocess_runs``：子进程执行记录（命令、退出码、输出尾部），用于事后核对；
* ``job_results``：**待发送结果**。结果先落库再上报，网络恢复后补发；
  ``(task_id, attempt_id)`` 唯一，重复写入是幂等的（先写先得）。

unknown 语义：任务结果无法确认（进程被杀、租约丢失、执行器重启）时标记为
``unknown``，**继续占用实例**，直到人工执行 ``reconcile`` 或后端核对完成；
在此之前执行器不领取任何新任务，也不自动重试。

SQLite 使用 WAL + busy_timeout，采集进程（结果上报线程）与执行器进程可同时访问。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .constants import (
    ACTIVE_LEASE_STATUSES,
    LEASE_RENEW_FAILURE_LIMIT,
    RESULT_REPORT_MAX_ATTEMPTS,
    STATUS_LEASED,
    STATUS_RECONCILED,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
    TERMINAL_STATUSES,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lease_token TEXT NOT NULL DEFAULT '',
    job_type TEXT NOT NULL DEFAULT '',
    phase TEXT NOT NULL DEFAULT 'leased',
    unknown_reason TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT,
    lease_seconds INTEGER NOT NULL DEFAULT 120,
    renew_failures INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS ix_job_leases_phase ON job_leases(phase);

CREATE TABLE IF NOT EXISTS job_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    step_no INTEGER,
    step_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    message TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    command_json TEXT NOT NULL DEFAULT '[]',
    exit_code INTEGER,
    event_seq INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE(task_id, attempt_id, phase, step_no)
);
CREATE INDEX IF NOT EXISTS ix_job_phases_task ON job_phases(task_id);

CREATE TABLE IF NOT EXISTS subprocess_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    command_json TEXT NOT NULL DEFAULT '[]',
    pid INTEGER,
    exit_code INTEGER,
    output_tail TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_subprocess_task ON subprocess_runs(task_id);

CREATE TABLE IF NOT EXISTS job_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lease_token TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    terminal INTEGER NOT NULL DEFAULT 1,
    event_seq INTEGER NOT NULL DEFAULT 1,
    report_attempts INTEGER NOT NULL DEFAULT 0,
    last_report_error TEXT NOT NULL DEFAULT '',
    next_report_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reported_at TEXT,
    UNIQUE(task_id, attempt_id)
);
CREATE INDEX IF NOT EXISTS ix_job_results_pending ON job_results(reported_at);

-- 事件序号计数器：每 (task_id, attempt_id) 单调递增，重放使用原始序号
CREATE TABLE IF NOT EXISTS event_counters (
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (task_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ActiveJobExists(Exception):
    """同一实例已有活动任务（含 unknown 占用）。"""

    def __init__(self, lease: dict[str, Any]) -> None:
        self.lease = lease
        super().__init__(f"已有活动任务 task_id={lease.get('task_id')} phase={lease.get('phase')}")


class Journal:
    """SQLite 执行日志门面。线程内使用独立连接（默认 ``check_same_thread=False`` + 锁）。"""

    def __init__(
        self,
        path: str | Path,
        *,
        now: Callable[[], datetime] = utc_now,
        lease_seconds: int = 120,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self.lease_seconds = int(lease_seconds)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ 基础
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def _commit(self) -> None:
        self._conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def now_iso(self) -> str:
        return iso(self._now()) or ""

    # ------------------------------------------------------------------ 租约
    def active_lease(self) -> dict[str, Any] | None:
        """当前活动租约（leased/running/reporting/unknown 中最新一条）。"""
        with self._lock:
            placeholders = ",".join("?" for _ in ACTIVE_LEASE_STATUSES)
            row = self._execute(
                f"SELECT * FROM job_leases WHERE phase IN ({placeholders}) "
                "ORDER BY id DESC LIMIT 1",
                tuple(sorted(ACTIVE_LEASE_STATUSES)),
            ).fetchone()
        return self._row(row)

    def has_unknown(self) -> bool:
        with self._lock:
            row = self._execute(
                "SELECT 1 FROM job_leases WHERE phase = ? LIMIT 1", (STATUS_UNKNOWN,)
            ).fetchone()
        return row is not None

    def get_lease(self, task_id: str, attempt_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if attempt_id is None:
                row = self._execute(
                    "SELECT * FROM job_leases WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (str(task_id),),
                ).fetchone()
            else:
                row = self._execute(
                    "SELECT * FROM job_leases WHERE task_id = ? AND attempt_id = ?",
                    (str(task_id), str(attempt_id)),
                ).fetchone()
        return self._row(row)

    def start_attempt(
        self,
        *,
        task_id: str,
        attempt_id: str,
        job_type: str,
        lease_token: str = "",
        payload: dict[str, Any] | None = None,
        lease_seconds: int | None = None,
    ) -> dict[str, Any]:
        """登记一次执行尝试；已有活动任务时抛 ``ActiveJobExists``（单实例单变更任务）。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self.active_lease()
                if existing is not None:
                    self._conn.rollback()
                    raise ActiveJobExists(existing)
                seconds = int(lease_seconds or self.lease_seconds)
                now = self._now()
                self._execute(
                    "INSERT INTO job_leases (task_id, attempt_id, lease_token, job_type, phase,"
                    " lease_expires_at, lease_seconds, payload_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(task_id),
                        str(attempt_id),
                        str(lease_token),
                        str(job_type),
                        STATUS_LEASED,
                        iso(now + timedelta(seconds=seconds)),
                        seconds,
                        json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                        iso(now),
                        iso(now),
                    ),
                )
                self._conn.commit()
            except ActiveJobExists:
                raise
            except Exception:
                self._conn.rollback()
                raise
        return self.get_lease(task_id, attempt_id) or {}

    def update_lease(
        self,
        *,
        task_id: str,
        attempt_id: str,
        phase: str | None = None,
        lease_token: str | None = None,
        unknown_reason: str | None = None,
        renew_failures: int | None = None,
        lease_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        fields: list[str] = ["updated_at = ?"]
        params: list[Any] = [self.now_iso()]
        if phase is not None:
            fields.append("phase = ?")
            params.append(phase)
            if phase in TERMINAL_STATUSES or phase == STATUS_RECONCILED:
                fields.append("lease_expires_at = ?")
                params.append(None)
        if lease_token is not None:
            fields.append("lease_token = ?")
            params.append(str(lease_token))
        if unknown_reason is not None:
            fields.append("unknown_reason = ?")
            params.append(str(unknown_reason)[:500])
        if renew_failures is not None:
            fields.append("renew_failures = ?")
            params.append(int(renew_failures))
        if lease_seconds is not None:
            lease = int(lease_seconds)
            fields.extend(["lease_seconds = ?", "lease_expires_at = ?"])
            params.extend([lease, iso(self._now() + timedelta(seconds=lease))])
        params.extend([str(task_id), str(attempt_id)])
        with self._lock:
            self._execute(
                f"UPDATE job_leases SET {', '.join(fields)} WHERE task_id = ? AND attempt_id = ?",
                tuple(params),
            )
            self._commit()
        return self.get_lease(task_id, attempt_id)

    def touch_lease(self, *, task_id: str, attempt_id: str) -> None:
        """仅刷新 updated_at，不延长租约（续租由后端返回的到期时间驱动）。"""
        with self._lock:
            self._execute(
                "UPDATE job_leases SET updated_at = ? WHERE task_id = ? AND attempt_id = ?",
                (self.now_iso(), str(task_id), str(attempt_id)),
            )
            self._commit()

    def lease_expired(self, lease: dict[str, Any] | None = None) -> bool:
        current = lease or self.active_lease()
        if not current:
            return False
        expires = parse_iso(current.get("lease_expires_at"))
        if expires is None:
            return False
        return expires <= self._now()

    def mark_unknown(self, *, task_id: str, attempt_id: str, reason: str) -> dict[str, Any] | None:
        """标记结果待核对：继续占用实例，等待 reconcile。"""
        return self.update_lease(
            task_id=task_id,
            attempt_id=attempt_id,
            phase=STATUS_UNKNOWN,
            unknown_reason=reason[:500],
        )

    def recover_interrupted(self) -> dict[str, Any] | None:
        """执行器重启后检查未完成租约：进程已不在 → 迁移为 unknown（禁止自动重试）。"""
        lease = self.active_lease()
        if lease is None:
            return None
        if lease.get("phase") == STATUS_UNKNOWN:
            return lease
        if lease.get("phase") in ACTIVE_LEASE_STATUSES:
            return self.mark_unknown(
                task_id=str(lease["task_id"]),
                attempt_id=str(lease["attempt_id"]),
                reason="执行器进程重启，执行结果未知，等待核对",
            )
        return lease

    def reconcile(self, *, task_id: str | None = None, note: str = "") -> dict[str, Any] | None:
        """人工核对完成：解除 unknown 占用；保留历史日志与结果。"""
        with self._lock:
            if task_id:
                lease = self.get_lease(task_id)
            else:
                lease = self.active_lease()
            if lease is None:
                return None
            reason = f"reconciled: {note}"[:500] if note else "reconciled"
            return self.update_lease(
                task_id=str(lease["task_id"]),
                attempt_id=str(lease["attempt_id"]),
                phase=STATUS_RECONCILED,
                unknown_reason=reason,
            )

    def record_renew_failure(self, *, task_id: str, attempt_id: str) -> int:
        lease = self.get_lease(task_id, attempt_id)
        failures = int((lease or {}).get("renew_failures") or 0) + 1
        self.update_lease(
            task_id=task_id, attempt_id=attempt_id, renew_failures=failures
        )
        return failures

    def reset_renew_failures(self, *, task_id: str, attempt_id: str) -> None:
        self.update_lease(task_id=task_id, attempt_id=attempt_id, renew_failures=0)

    @staticmethod
    def lease_lost(failures: int) -> bool:
        """连续续租失败达到阈值 → 本地判定租约丢失（结果转 unknown）。"""
        return int(failures) >= LEASE_RENEW_FAILURE_LIMIT

    # ------------------------------------------------------------------ 事件序号
    def next_event_seq(self, task_id: str, attempt_id: str) -> int:
        """同一 (task_id, attempt_id) 内单调递增的事件序号（从 1 开始）。

        服务端按 ``event_seq <= 已收水位`` 判重复；重放结果时复用结果行里保存的原 seq，
        阶段事件允许跳号，但同一 attempt 内必须严格递增。
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._execute(
                    "INSERT INTO event_counters (task_id, attempt_id, seq, updated_at)"
                    " VALUES (?, ?, 1, ?)"
                    " ON CONFLICT(task_id, attempt_id) DO UPDATE SET"
                    " seq = event_counters.seq + 1, updated_at = excluded.updated_at",
                    (str(task_id), str(attempt_id), self.now_iso()),
                )
                row = self._execute(
                    "SELECT seq FROM event_counters WHERE task_id = ? AND attempt_id = ?",
                    (str(task_id), str(attempt_id)),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return int((row["seq"] if row else 1) or 1)

    # ------------------------------------------------------------------ 阶段日志
    def record_phase(
        self,
        *,
        task_id: str,
        attempt_id: str,
        phase: str,
        step_no: int | None = None,
        step_name: str = "",
        status: str = STATUS_RUNNING,
        message: str = "",
        pid: int | None = None,
        command: list[str] | None = None,
        exit_code: int | None = None,
        event_seq: int | None = None,
    ) -> None:
        now = self.now_iso()
        finished = now if status in {"succeeded", "failed", "skipped"} else None
        # SQLite 唯一约束中 NULL 互不冲突，用 -1 表示「无步骤号」的记录，保证同阶段只有一条
        step_value = int(step_no) if step_no is not None else -1
        with self._lock:
            self._execute(
                "INSERT INTO job_phases (task_id, attempt_id, phase, step_no, step_name, status,"
                " message, pid, command_json, exit_code, event_seq, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(task_id, attempt_id, phase, step_no) DO UPDATE SET"
                " status = excluded.status, message = excluded.message, pid = excluded.pid,"
                " command_json = excluded.command_json, exit_code = excluded.exit_code,"
                " event_seq = COALESCE(excluded.event_seq, job_phases.event_seq),"
                " finished_at = excluded.finished_at",
                (
                    str(task_id),
                    str(attempt_id),
                    str(phase),
                    step_value,
                    str(step_name)[:64],
                    str(status)[:16],
                    str(message)[:950],
                    pid,
                    json.dumps(command or [], ensure_ascii=False),
                    exit_code,
                    int(event_seq) if event_seq is not None else None,
                    now,
                    finished,
                ),
            )
            self._commit()

    def record_subprocess(
        self,
        *,
        task_id: str,
        attempt_id: str,
        phase: str,
        command: list[str],
        pid: int | None = None,
        exit_code: int | None = None,
        output_tail: str = "",
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        with self._lock:
            self._execute(
                "INSERT INTO subprocess_runs (task_id, attempt_id, phase, command_json, pid,"
                " exit_code, output_tail, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(task_id),
                    str(attempt_id),
                    str(phase),
                    json.dumps(command, ensure_ascii=False),
                    pid,
                    exit_code,
                    str(output_tail)[-4000:],
                    started_at or self.now_iso(),
                    finished_at,
                ),
            )
            self._commit()

    def list_phases(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._execute(
                "SELECT * FROM job_phases WHERE task_id = ? ORDER BY id ASC", (str(task_id),)
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def list_subprocesses(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._execute(
                "SELECT * FROM subprocess_runs WHERE task_id = ? ORDER BY id ASC", (str(task_id),)
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    # ------------------------------------------------------------------ 结果持久化/补报
    def persist_result(
        self,
        *,
        task_id: str,
        attempt_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
        lease_token: str = "",
        event_seq: int | None = None,
    ) -> dict[str, Any]:
        """先落库后上报；同一 (task_id, attempt_id) 重复写入返回首次结果（终态幂等）。

        ``event_seq`` 为上报用的事件序号：首次写入时分配，重放时沿用原序号。
        """
        now = self.now_iso()
        seq = int(event_seq) if event_seq is not None else self.next_event_seq(task_id, attempt_id)
        with self._lock:
            self._execute(
                "INSERT INTO job_results (task_id, attempt_id, lease_token, status, result_json,"
                " error, terminal, event_seq, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)"
                " ON CONFLICT(task_id, attempt_id) DO NOTHING",
                (
                    str(task_id),
                    str(attempt_id),
                    str(lease_token),
                    str(status)[:16],
                    json.dumps(result or {}, ensure_ascii=False, sort_keys=True),
                    str(error)[:500],
                    seq,
                    now,
                    now,
                ),
            )
            self._commit()
            row = self._execute(
                "SELECT * FROM job_results WHERE task_id = ? AND attempt_id = ?",
                (str(task_id), str(attempt_id)),
            ).fetchone()
        return self._row(row) or {}

    def pending_results(self, limit: int = 20, *, ignore_backoff: bool = False) -> list[dict[str, Any]]:
        """待上报结果（含上报失败待重试的，默认遵守退避时间）。

        ``ignore_backoff=True`` 用于网络恢复/启动补报时立即重试。
        """
        if ignore_backoff:
            with self._lock:
                rows = self._execute(
                    "SELECT * FROM job_results WHERE reported_at IS NULL"
                    " ORDER BY id ASC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [self._row(row) or {} for row in rows]
        now = self.now_iso()
        with self._lock:
            rows = self._execute(
                "SELECT * FROM job_results WHERE reported_at IS NULL"
                " AND (next_report_at IS NULL OR next_report_at <= ?)"
                " ORDER BY id ASC LIMIT ?",
                (now, int(limit)),
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def mark_result_reported(self, result_id: int) -> None:
        now = self.now_iso()
        with self._lock:
            self._execute(
                "UPDATE job_results SET reported_at = ?, updated_at = ?,"
                " last_report_error = '' WHERE id = ?",
                (now, now, int(result_id)),
            )
            self._commit()

    def mark_result_report_failed(self, result_id: int, error: str, *, backoff_seconds: int) -> int:
        """上报失败：累加次数、记录错误、按退避推迟下次重试，返回累计次数。"""
        now = self._now()
        with self._lock:
            row = self._execute(
                "SELECT report_attempts FROM job_results WHERE id = ?", (int(result_id),)
            ).fetchone()
            attempts = int((row["report_attempts"] if row else 0) or 0) + 1
            next_at = iso(now + timedelta(seconds=max(1, int(backoff_seconds))))
            self._execute(
                "UPDATE job_results SET report_attempts = ?, last_report_error = ?,"
                " next_report_at = ?, updated_at = ? WHERE id = ?",
                (attempts, str(error)[:500], next_at, iso(now), int(result_id)),
            )
            self._commit()
        return attempts

    def override_result(
        self,
        *,
        task_id: str,
        status: str,
        error: str = "",
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """人工核对后修正结果并重置上报状态（下一次补报使用修正后的终态）。

        用于 ``reconcile``：运维人员已确认任务实际结果时，覆盖本地待发送结果，
        保证中心收到的是核对后的终态，而不是 unknown。
        """
        now = self.now_iso()
        with self._lock:
            row = self._execute(
                "SELECT * FROM job_results WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                (str(task_id),),
            ).fetchone()
            if row is None:
                return None
            self._execute(
                "UPDATE job_results SET status = ?, error = ?, result_json = ?,"
                " reported_at = NULL, report_attempts = 0, last_report_error = '',"
                " next_report_at = NULL, updated_at = ? WHERE id = ?",
                (
                    str(status)[:16],
                    str(error)[:500],
                    json.dumps(result or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    int(row["id"]),
                ),
            )
            self._commit()
            updated = self._execute(
                "SELECT * FROM job_results WHERE id = ?", (int(row["id"]),)
            ).fetchone()
        return self._row(updated)

    def result_of(self, task_id: str, attempt_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if attempt_id is None:
                row = self._execute(
                    "SELECT * FROM job_results WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (str(task_id),),
                ).fetchone()
            else:
                row = self._execute(
                    "SELECT * FROM job_results WHERE task_id = ? AND attempt_id = ?",
                    (str(task_id), str(attempt_id)),
                ).fetchone()
        return self._row(row)

    def reported_result_exists(self, task_id: str, attempt_id: str) -> bool:
        with self._lock:
            row = self._execute(
                "SELECT 1 FROM job_results WHERE task_id = ? AND attempt_id = ?"
                " AND reported_at IS NOT NULL",
                (str(task_id), str(attempt_id)),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------ 运行状态
    def kv_get(self, key: str) -> str | None:
        with self._lock:
            row = self._execute("SELECT v FROM kv WHERE k = ?", (str(key),)).fetchone()
        return str(row["v"]) if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self._lock:
            self._execute(
                "INSERT INTO kv (k, v, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
                (str(key), str(value), self.now_iso()),
            )
            self._commit()

    def counts(self) -> dict[str, int]:
        with self._lock:
            pending = self._execute(
                "SELECT COUNT(*) AS c FROM job_results WHERE reported_at IS NULL"
            ).fetchone()
            results = self._execute("SELECT COUNT(*) AS c FROM job_results").fetchone()
            phases = self._execute("SELECT COUNT(*) AS c FROM job_phases").fetchone()
            leases = self._execute("SELECT COUNT(*) AS c FROM job_leases").fetchone()
        return {
            "pending_results": int((pending["c"] if pending else 0) or 0),
            "results": int((results["c"] if results else 0) or 0),
            "phases": int((phases["c"] if phases else 0) or 0),
            "leases": int((leases["c"] if leases else 0) or 0),
        }

    def status_summary(self) -> dict[str, Any]:
        lease = self.active_lease()
        counts = self.counts()
        return {
            "path": str(self.path),
            "active_lease": lease,
            "has_unknown": bool(lease and lease.get("phase") == STATUS_UNKNOWN),
            "unknown_reason": (lease or {}).get("unknown_reason", ""),
            "lease_expired": self.lease_expired(lease),
            "counts": counts,
            "max_report_attempts": RESULT_REPORT_MAX_ATTEMPTS,
        }


__all__ = [
    "ActiveJobExists",
    "Journal",
    "iso",
    "parse_iso",
    "utc_now",
]
