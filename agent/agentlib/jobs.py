"""任务执行协议（租约 / unknown 占用 / 结果先落库后上报）。

可靠性规则（对应任务要求）：

1. **同一实例只允许一个变更任务**：本地 SQLite 只允许一条活动租约；执行器单线程领取。
2. **租约 120 秒、每 30 秒续租**：续租失败累计到阈值即判定租约丢失，结果转 ``unknown``。
3. **unknown 继续占用**：结果无法确认（进程被杀、执行器重启、租约丢失）时标记 unknown，
   持续心跳告知中心「待核对」，**reconcile 之前不领取新任务、不自动重试**。
4. **结果先持久化再上报**：执行结束先写 SQLite（``(task_id, attempt_id)`` 唯一、先写先得），
   再尝试上报；上报失败由后台补报线程按退避重试，网络恢复后补发，终态幂等。
5. **协议协商**：任务要求的最低 Agent 协议高于当前版本时拒绝执行并上报失败，
   旧 Agent 不会执行变更任务（中心也会按上报的协议版本过滤）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable

from .constants import (
    CAPABILITIES,
    EXECUTOR_VERSION,
    JOB_LEASE_RENEW_SECONDS,
    JOB_LEASE_SECONDS,
    MUTATION_JOB_TYPES,
    PROTOCOL_VERSION,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    STATUS_UNKNOWN,
)
from .heartbeat import JobLeaseRenewer
from .httpc import CenterClient, CenterError, EndpointUnsupported, NetworkError, data_of
from .journal import ActiveJobExists, Journal
from .telemetry import utc_now

# 结果上报退避：5s、10s、20s … 上限 300s
REPORT_BACKOFF_BASE = 5
REPORT_BACKOFF_MAX = 300


def report_backoff_seconds(attempts: int) -> int:
    attempt = max(1, int(attempts))
    return int(min(REPORT_BACKOFF_MAX, REPORT_BACKOFF_BASE * (2 ** (attempt - 1))))


class JobRunner:
    """执行器主体：轮询 → 加租约 → 执行 → 落库 → 上报。"""

    def __init__(
        self,
        client: CenterClient,
        journal: Journal,
        config: dict[str, Any],
        *,
        executor: Callable[[dict[str, Any], dict[str, Any], Callable[..., None]], dict[str, Any]],
        logger: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        lease_seconds: int = JOB_LEASE_SECONDS,
        renew_seconds: int = JOB_LEASE_RENEW_SECONDS,
    ) -> None:
        self.client = client
        self.journal = journal
        self.config = config
        self.executor = executor
        self.log = logger or (lambda message: None)
        self.sleep = sleep
        self.clock = clock
        self.lease_seconds = int(lease_seconds)
        self.renew_seconds = int(renew_seconds)
        self._blocked_logged = False
        # 服务端能力协商结果（poll 返回），供 status/心跳上报
        self.remote_state: dict[str, Any] = {}

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        lease = self.journal.active_lease()
        if lease is None:
            return {"role": "executor", "busy": False, "phase": "idle"}
        return {
            "role": "executor",
            "busy": True,
            "task_id": str(lease.get("task_id") or ""),
            "attempt_id": str(lease.get("attempt_id") or ""),
            "phase": str(lease.get("phase") or ""),
            "lease_expires_at": lease.get("lease_expires_at"),
            "unknown": str(lease.get("phase") or "") == STATUS_UNKNOWN,
        }

    # ------------------------------------------------------------------ 启动恢复
    def recover_on_start(self) -> dict[str, Any] | None:
        """执行器重启：把未完成租约迁移为 unknown 并上报，避免中心误判成功。

        同时调用 ``/agent/jobs/{id}/reconcile`` 让服务端核对（若服务端已判定终态，
        以服务端为准覆盖本地结果并解除占用）。
        """
        lease = self.journal.recover_interrupted()
        if lease is None:
            return None
        if str(lease.get("phase")) != STATUS_UNKNOWN:
            return None
        self.log(
            f"发现未完成租约 task_id={lease.get('task_id')}，标记 unknown 并等待人工核对"
        )
        self._persist_unknown(
            task_id=str(lease["task_id"]),
            attempt_id=str(lease["attempt_id"]),
            lease_token=str(lease.get("lease_token") or ""),
            reason=str(lease.get("unknown_reason") or "执行器重启，结果未知"),
        )
        self.flush_results(force=True)
        self._apply_remote_reconcile(lease)
        return self.journal.get_lease(str(lease["task_id"])) or lease

    def _apply_remote_reconcile(self, lease: dict[str, Any]) -> dict[str, Any] | None:
        """服务端核对：终态以服务端为准覆盖本地并解除占用，其余保持 unknown 占用。"""
        data = self.reconcile_remote(lease)
        if not data or not data.get("known"):
            return data
        action = str(data.get("action") or "")
        server_status = str(data.get("server_status") or "")
        terminal = {STATUS_SUCCEEDED, STATUS_FAILED}
        if action in {"marked_succeeded", "marked_failed"} and server_status in terminal:
            self.journal.override_result(
                task_id=str(lease["task_id"]),
                status=server_status,
                error="与服务端核对后确认的终态",
            )
            self.journal.update_lease(
                task_id=str(lease["task_id"]),
                attempt_id=str(lease["attempt_id"]),
                phase=server_status,
            )
            self.flush_results(force=True)
            self.log(
                f"服务端已确认任务 {lease['task_id']} 终态为 {server_status}，解除本地 unknown 占用"
            )
        elif action in {"already_terminal"} and server_status in terminal:
            self.journal.override_result(
                task_id=str(lease["task_id"]),
                status=server_status,
                error="服务端已存在终态，忽略重复上报",
            )
            self.journal.update_lease(
                task_id=str(lease["task_id"]),
                attempt_id=str(lease["attempt_id"]),
                phase=server_status,
            )
            self.flush_results(force=True)
        return data

    # ------------------------------------------------------------------ 单轮
    def run_once(self) -> dict[str, Any]:
        """执行一轮：有占用则保持占用；否则领取并执行一个任务。"""
        lease = self.journal.active_lease()
        if lease is not None:
            return self._keep_occupied(lease)
        job = self._poll()
        if not job:
            self._blocked_logged = False
            return {"status": "idle"}
        return self.execute(job)

    def _keep_occupied(self, lease: dict[str, Any]) -> dict[str, Any]:
        phase = str(lease.get("phase") or "")
        if phase != STATUS_UNKNOWN:
            # 异常残留（例如进程被 kill 后重启）：先转 unknown，再持续占用
            recovered = self.journal.recover_interrupted()
            if recovered is not None and str(recovered.get("phase")) == STATUS_UNKNOWN:
                self._persist_unknown(
                    task_id=str(recovered["task_id"]),
                    attempt_id=str(recovered["attempt_id"]),
                    lease_token=str(recovered.get("lease_token") or ""),
                    reason=str(recovered.get("unknown_reason") or "执行器重启，结果未知"),
                )
                self.flush_results()
            lease = self.journal.active_lease() or lease
        renewer = JobLeaseRenewer(
            self.client,
            self.journal,
            task_id=str(lease["task_id"]),
            attempt_id=str(lease["attempt_id"]),
            lease_token=str(lease.get("lease_token") or ""),
            lease_seconds=self.lease_seconds,
            logger=self.log,
        )
        renewer.renew_once(phase=STATUS_UNKNOWN)
        if not self._blocked_logged:
            self._blocked_logged = True
            self.log(
                f"任务 {lease.get('task_id')} 结果待核对（unknown）：继续占用实例，"
                "reconcile 前不领取新任务"
            )
        return {
            "status": "blocked_unknown",
            "task_id": str(lease.get("task_id") or ""),
            "phase": str(lease.get("phase") or ""),
        }

    def _poll(self) -> dict[str, Any] | None:
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "agent_version": EXECUTOR_VERSION,
            "executor_version": EXECUTOR_VERSION,
            "capabilities": list(CAPABILITIES),
            "mutation_job_types": sorted(MUTATION_JOB_TYPES),
        }
        response = self.client.poll_job(payload)
        data = data_of(response)
        # 服务端能力协商结果（upgrade_required/mutation_allowed/blocked_job/message）
        self.remote_state = {
            key: data.get(key)
            for key in ("upgrade_required", "mutation_allowed", "blocked_job", "message", "server_time")
            if key in data
        }
        if isinstance(data.get("blocked_job"), dict):
            self.log(f"服务端暂不派发任务：{data.get('message') or data.get('blocked_job')}")
        job = data.get("job")
        if isinstance(job, dict) and job.get("id") is not None:
            return job
        return None

    # ------------------------------------------------------------------ 执行
    def execute(self, job: dict[str, Any]) -> dict[str, Any]:
        task_id = str(job.get("id"))
        attempt_id = str(job.get("attempt_id") or uuid.uuid4().hex)
        lease_token = str(job.get("lease_token") or "")
        job_type = str(job.get("job_type") or "")
        min_protocol = int(job.get("min_agent_protocol") or 0)
        # 以服务端返回的租约时长为准（固定 120s），本地默认仅兜底
        try:
            server_lease = int(job.get("lease_seconds") or self.lease_seconds)
        except (TypeError, ValueError):
            server_lease = self.lease_seconds
        self.lease_seconds = max(30, server_lease)
        if min_protocol > PROTOCOL_VERSION:
            reason = (
                f"任务要求 Agent 协议 {min_protocol}，当前 {PROTOCOL_VERSION}，"
                "请先升级 Agent（旧 Agent 不执行变更任务）"
            )
            self.log(reason)
            self.journal.record_phase(
                task_id=task_id,
                attempt_id=attempt_id,
                phase="protocol_check",
                step_name="协议校验",
                status="failed",
                message=reason,
            )
            self._persist_result(
                task_id=task_id,
                attempt_id=attempt_id,
                lease_token=lease_token,
                status=STATUS_FAILED,
                result={},
                error=reason,
            )
            self.flush_results()
            return {"status": STATUS_FAILED, "task_id": task_id, "error": reason}

        try:
            self.journal.start_attempt(
                task_id=task_id,
                attempt_id=attempt_id,
                job_type=job_type,
                lease_token=lease_token,
                payload=job.get("payload") or {},
                lease_seconds=self.lease_seconds,
            )
        except ActiveJobExists as exc:
            self.log(f"拒绝领取任务 {task_id}：本地已有活动任务（{exc}）")
            return {"status": "blocked_active", "task_id": task_id}

        self.journal.update_lease(
            task_id=task_id, attempt_id=attempt_id, phase=STATUS_RUNNING
        )
        self.journal.record_phase(
            task_id=task_id,
            attempt_id=attempt_id,
            phase="dispatched",
            step_name="接收任务",
            status="succeeded",
            message=f"任务类型：{job_type}；租约 {self.lease_seconds}s",
        )
        self.log(f"开始执行任务 {task_id}（{job_type}，attempt={attempt_id}）")

        lease_lost = threading.Event()
        stop_renew = threading.Event()
        renewer = JobLeaseRenewer(
            self.client,
            self.journal,
            task_id=task_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            lease_seconds=self.lease_seconds,
            logger=self.log,
        )
        renew_thread = threading.Thread(
            target=renewer.run_forever,
            args=(stop_renew, self.renew_seconds),
            kwargs={"on_lost": lambda: lease_lost.set()},
            name=f"lease-renew-{task_id}",
            daemon=True,
        )
        renew_thread.start()

        report = self._make_reporter(
            task_id=task_id, attempt_id=attempt_id, lease_token=lease_token
        )
        recorder = self._make_recorder(task_id=task_id, attempt_id=attempt_id)
        outcome: dict[str, Any]
        try:
            outcome = self.executor(job, self.config, report, recorder)
            if not isinstance(outcome, dict):
                outcome = {"ok": False, "error": "执行器返回值不合法"}
        except Exception as exc:  # noqa: BLE001 - 执行异常必须落库后上报
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            stop_renew.set()
            renew_thread.join(timeout=max(5, self.renew_seconds))

        status = STATUS_SUCCEEDED if outcome.get("ok") else STATUS_FAILED
        error = "" if outcome.get("ok") else str(outcome.get("error") or "执行失败")[:500]
        if lease_lost.is_set() or self.journal.has_unknown():
            status = STATUS_UNKNOWN
            error = error or "任务租约丢失，执行结果待核对"
            self.log(f"任务 {task_id} 租约丢失，结果标记为 unknown")

        result_payload = {
            key: value for key, value in outcome.items() if key != "output"
        }
        self.journal.record_phase(
            task_id=task_id,
            attempt_id=attempt_id,
            phase="finished",
            step_name="执行结束",
            status=status,
            message=error,
            exit_code=int(outcome.get("exit_code") or 0) if outcome.get("exit_code") else None,
        )
        if status == STATUS_UNKNOWN:
            self.journal.mark_unknown(
                task_id=task_id,
                attempt_id=attempt_id,
                reason=error or "租约丢失，结果待核对",
            )
        # 先持久化，再上报（网络失败由 flush_results 补发）
        self._persist_result(
            task_id=task_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            status=status,
            result=result_payload,
            error=error,
        )
        if status != STATUS_UNKNOWN:
            self.journal.update_lease(task_id=task_id, attempt_id=attempt_id, phase=status)
        report_result = self.flush_results()
        return {
            "status": status,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "error": error,
            "report": report_result,
        }

    # ------------------------------------------------------------------ 结果/上报
    def _persist_result(
        self,
        *,
        task_id: str,
        attempt_id: str,
        status: str,
        lease_token: str = "",
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        return self.journal.persist_result(
            task_id=task_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            status=status,
            result=result or {},
            error=error,
        )

    def _persist_unknown(
        self,
        *,
        task_id: str,
        attempt_id: str,
        lease_token: str,
        reason: str,
        status: str = STATUS_UNKNOWN,
    ) -> dict[str, Any]:
        self.journal.mark_unknown(task_id=task_id, attempt_id=attempt_id, reason=reason)
        self.journal.record_phase(
            task_id=task_id,
            attempt_id=attempt_id,
            phase="unknown",
            step_name="结果待核对",
            status=STATUS_UNKNOWN,
            message=reason,
        )
        return self._persist_result(
            task_id=task_id,
            attempt_id=attempt_id,
            lease_token=lease_token,
            status=status,
            result={},
            error=reason,
        )

    def result_event_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        """终态事件负载：带 attempt_id / lease_token / event_seq（服务端幂等水位）。"""
        result = {}
        raw = row.get("result_json")
        if raw:
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                result = {}
        if not isinstance(result, dict):
            result = {}
        payload: dict[str, Any] = {
            "status": str(row.get("status") or ""),
            "result": result,
            "error": str(row.get("error") or "")[:500],
            "attempt_id": str(row.get("attempt_id") or ""),
            "lease_token": str(row.get("lease_token") or ""),
            "event_seq": int(row.get("event_seq") or 1),
        }
        checks = result.get("checks")
        if isinstance(checks, dict):
            payload["checks"] = checks
        return payload

    def reconcile_remote(
        self,
        lease: dict[str, Any],
        *,
        local_status: str = STATUS_UNKNOWN,
        note: str = "",
    ) -> dict[str, Any] | None:
        """把本地 unknown / 重启残留上报给服务端核对（新协议；未上线时降级 None）。

        服务端返回 ``action``：marked_succeeded / marked_failed / kept_unknown /
        already_terminal / not_dispatched / ignored_unknown_task；``known=false`` 表示中心
        没有该任务，此时保留本地占用等人工处理。
        """
        task_id = str(lease.get("task_id") or "")
        if not task_id:
            return None
        phases = [
            {
                "step_no": int(row.get("step_no") or 0),
                "step_name": str(row.get("step_name") or ""),
                "status": str(row.get("status") or ""),
                "message": str(row.get("message") or "")[:500],
            }
            for row in self.journal.list_phases(task_id)
        ]
        payload = {
            "attempt_id": str(lease.get("attempt_id") or ""),
            "lease_token": str(lease.get("lease_token") or ""),
            "local_status": str(local_status),
            "phase": str(lease.get("phase") or STATUS_UNKNOWN),
            "error": str(lease.get("unknown_reason") or note or "")[:500],
            "result": {},
            "checks": {},
            "phases": phases,
        }
        try:
            response = self.client.job_reconcile(task_id, payload)
        except EndpointUnsupported:
            self.log("中心未实现 /agent/jobs/{id}/reconcile，仅本地记录 unknown 占用")
            return None
        except Exception as exc:  # noqa: BLE001 - 核对失败保持本地占用
            self.log(f"与服务端核对 unknown 任务失败（保持本地占用）：{exc}")
            return None
        data = data_of(response)
        action = str(data.get("action") or "")
        server_status = str(data.get("server_status") or "")
        self.log(
            f"服务端核对结果：task_id={task_id} known={data.get('known')} "
            f"action={action} server_status={server_status}"
        )
        return data

    def flush_results(self, limit: int = 20, *, force: bool = False) -> dict[str, int]:
        """补发所有待上报结果；网络错误按退避保留，永久错误转死信（记录后停止重试）。

        ``force=True`` 忽略退避立即重试（启动、网络恢复、人工 reconcile 后使用）。
        服务端 409（旧 attempt/终态冲突）属于永久错误：停止重试、落死信、不改本地状态。
        """
        summary = {"sent": 0, "failed": 0, "dead_letter": 0}
        for row in self.journal.pending_results(limit=limit, ignore_backoff=force):
            task_id = str(row.get("task_id") or "")
            payload = self.result_event_payload(row)
            try:
                self.client.job_event(task_id, payload)
            except CenterError as exc:
                if exc.retryable:
                    attempts = self.journal.mark_result_report_failed(
                        int(row["id"]),
                        str(exc),
                        backoff_seconds=report_backoff_seconds(
                            int(row.get("report_attempts") or 0) + 1
                        ),
                    )
                    self.log(f"结果上报失败（第 {attempts} 次，稍后补发）：{exc}")
                    summary["failed"] += 1
                    continue
                # 4xx：任务已不存在/参数永久不合法，记录后停止重试，避免毒丸无限循环
                self.journal.mark_result_report_failed(
                    int(row["id"]), str(exc), backoff_seconds=REPORT_BACKOFF_MAX
                )
                self.journal.mark_result_reported(int(row["id"]))
                self.log(f"结果上报被拒绝（{exc.status}），转死信停止重试：{exc}")
                summary["dead_letter"] += 1
                continue
            except (NetworkError, EndpointUnsupported) as exc:
                attempts = self.journal.mark_result_report_failed(
                    int(row["id"]),
                    str(exc),
                    backoff_seconds=report_backoff_seconds(
                        int(row.get("report_attempts") or 0) + 1
                    ),
                )
                self.log(f"结果上报网络失败（第 {attempts} 次，网络恢复后补发）：{exc}")
                summary["failed"] += 1
                continue
            self.journal.mark_result_reported(int(row["id"]))
            summary["sent"] += 1
            self.log(f"任务结果已上报：task_id={task_id} status={payload['status']}")
        return summary

    # ------------------------------------------------------------------ 报告回调
    def _make_reporter(
        self, *, task_id: str, attempt_id: str, lease_token: str = ""
    ) -> Callable[..., None]:
        """执行器的阶段回调：先写本地日志，再尽力上报（失败不影响执行）。

        阶段事件带 attempt_id / lease_token / event_seq（同一 attempt 内单调递增）。
        """

        def report(step_no: int, step_name: str, status: str, message: str = "") -> None:
            seq = self.journal.next_event_seq(task_id, attempt_id)
            self.journal.record_phase(
                task_id=task_id,
                attempt_id=attempt_id,
                phase="step",
                step_no=int(step_no or 0),
                step_name=step_name,
                status=status,
                message=message,
                event_seq=seq,
            )
            try:
                self.client.job_event(
                    task_id,
                    {
                        "status": "running",
                        "step_no": int(step_no or 1),
                        "step_name": str(step_name)[:64],
                        "step_status": str(status)[:16],
                        "message": str(message or "")[:950],
                        "attempt_id": attempt_id,
                        "lease_token": lease_token,
                        "event_seq": seq,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - 阶段上报失败只记录
                self.log(f"阶段上报失败（本地已记录）：{exc}")

        return report

    def _make_recorder(self, *, task_id: str, attempt_id: str) -> Callable[..., None]:
        """子进程记录回调：命令、PID、退出码、输出尾部写入 SQLite。"""

        def record(
            phase: str,
            command: list[str],
            exit_code: int | None = None,
            output: str = "",
            pid: int | None = None,
        ) -> None:
            self.journal.record_subprocess(
                task_id=task_id,
                attempt_id=attempt_id,
                phase=str(phase),
                command=[str(item) for item in command],
                pid=pid,
                exit_code=exit_code,
                output_tail=output,
            )

        return record

    # ------------------------------------------------------------------ 循环
    def run_loop(
        self,
        stop_event: threading.Event,
        *,
        poll_seconds: float = 5.0,
    ) -> None:
        while not stop_event.is_set():
            try:
                result = self.run_once()
            except (CenterError, NetworkError) as exc:
                self.log(f"任务轮询失败：{exc}")
                result = {"status": "error", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - 执行器循环不能因单次异常退出
                self.log(f"执行器异常：{exc}")
                result = {"status": "error", "error": str(exc)}
            if result.get("status") in {"idle", "error"}:
                stop_event.wait(max(1.0, float(poll_seconds)))
        self.log("执行器收到停止信号，退出")


def status_payload(journal: Journal) -> dict[str, Any]:
    """供 CLI / 心跳使用的执行器状态摘要。"""
    summary = journal.status_summary()
    lease = summary.get("active_lease") or {}
    return {
        "has_unknown": bool(summary.get("has_unknown")),
        "active_task_id": str(lease.get("task_id") or ""),
        "active_phase": str(lease.get("phase") or ""),
        "pending_results": int((summary.get("counts") or {}).get("pending_results") or 0),
        "journal": str(summary.get("path") or ""),
        "checked_at": utc_now(),
    }


__all__ = [
    "ActiveJobExists",
    "JobRunner",
    "REPORT_BACKOFF_MAX",
    "report_backoff_seconds",
    "status_payload",
]
