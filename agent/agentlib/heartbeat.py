"""心跳与任务租约续租（仅标准库）。

协议（详见 ``agent/PROTOCOL.md``，服务端由 backend-catalog 的 agent_protocol 实现）：

* **Agent 心跳**：采集进程每 30 秒独立调用 ``POST /api/v1/agent/heartbeat``，
  与指标上报完全解耦；返回的 ``heartbeat_seconds`` / ``job_lease_seconds`` /
  ``job_poll_seconds`` / ``telemetry_seconds`` 会被客户端采用。
* **任务租约续租**：执行器领取任务后每 30 秒调用
  ``POST /api/v1/agent/jobs/{id}/heartbeat``，租约 120 秒。
* **409（租约丢失/旧 attempt/终态）**：立即停止续租，本地结果转 ``unknown``，
  继续占用实例直到 reconcile，不做自动重试。
* 连续网络失败达到阈值同样判定租约丢失。
* 后端尚未上线新接口（404/405/501）时降级：不续租，沿用后端旧租约语义，只记录一次。
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any, Callable

from .constants import (
    AGENT_VERSION,
    CAPABILITIES,
    EXECUTOR_VERSION,
    HEARTBEAT_SECONDS,
    JOB_LEASE_RENEW_SECONDS,
    JOB_LEASE_SECONDS,
    PROTOCOL_VERSION,
    STATUS_RUNNING,
    STATUS_UNKNOWN,
)
from .httpc import CenterClient, CenterError, EndpointUnsupported, data_of
from .journal import Journal
from .telemetry import detect_app_version, utc_now


class HeartbeatSender:
    """Agent 独立心跳发送器（可单次调用，也可在后台线程循环）。"""

    def __init__(
        self,
        client: CenterClient,
        *,
        role: str = "collector",
        config: dict[str, Any] | None = None,
        boot_id: str = "",
        state_provider: Callable[[], dict[str, Any]] | None = None,
        logger: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        interval: int = HEARTBEAT_SECONDS,
    ) -> None:
        self.client = client
        self.role = role
        self.config = config or {}
        self.boot_id = boot_id
        self.state_provider = state_provider or (lambda: {})
        self.log = logger or (lambda message: None)
        self.clock = clock
        self.started_at = clock()
        self.interval = max(5, int(interval))
        self.seq = 0
        self.endpoint_supported: bool | None = None
        self.last_sent_at: float | None = None
        self.last_error = ""
        self.remote: dict[str, Any] = {}
        self._warned = False

    def payload(self) -> dict[str, Any]:
        self.seq += 1
        state = self.state_provider() or {}
        return {
            "hostname": socket.gethostname()[:255],
            "app_version": detect_app_version(self.config),
            "agent_version": AGENT_VERSION,
            "executor_version": EXECUTOR_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": list(CAPABILITIES),
            "boot_id": self.boot_id,
            "seq": self.seq,
            "role": self.role,
            "db_version": str(self.config.get("db_version") or ""),
            "uptime_seconds": int(max(0.0, self.clock() - self.started_at)),
            "sent_at": utc_now(),
            "state": state,
        }

    def _apply_remote_timings(self, data: dict[str, Any]) -> None:
        """采用服务端下发的时间参数（缺失时保持本地默认）。"""
        self.remote = {
            key: data.get(key)
            for key in (
                "server_time",
                "protocol_version",
                "min_protocol_version",
                "upgrade_required",
                "mutation_allowed",
                "heartbeat_seconds",
                "job_lease_seconds",
                "job_poll_seconds",
                "telemetry_seconds",
                "seq_ack",
                "stale",
            )
            if key in data
        }
        heartbeat = data.get("heartbeat_seconds")
        if isinstance(heartbeat, (int, float)) and heartbeat >= 5:
            self.interval = int(heartbeat)

    def send_once(self) -> dict[str, Any]:
        """发送一次心跳；返回 ``{ok, supported, interval, remote}``。"""
        try:
            response = self.client.heartbeat(self.payload())
        except EndpointUnsupported:
            self.endpoint_supported = False
            if not self._warned:
                self._warned = True
                self.log(
                    "中心未实现 /agent/heartbeat，降级为仅指标上报（心跳能力暂不可用）"
                )
            self.last_error = "endpoint_unsupported"
            return {"ok": False, "supported": False, "interval": self.interval}
        except Exception as exc:  # noqa: BLE001 - 心跳失败不得影响主循环
            self.last_error = str(exc)[:300]
            self.log(f"心跳发送失败：{exc}")
            return {"ok": False, "supported": self.endpoint_supported is not False}
        self.endpoint_supported = True
        self.last_sent_at = self.clock()
        self.last_error = ""
        data = data_of(response)
        self._apply_remote_timings(data)
        return {
            "ok": True,
            "supported": True,
            "interval": self.interval,
            "server_time": data.get("server_time"),
            "remote": self.remote,
        }

    def run_forever(
        self, stop_event: threading.Event, interval: int | None = None
    ) -> None:
        while not stop_event.is_set():
            result = self.send_once()
            wait_seconds = int(result.get("interval") or interval or self.interval)
            stop_event.wait(max(5, wait_seconds))


class JobLeaseRenewer:
    """任务租约续租：120 秒租约、每 30 秒续租，409 视为租约丢失。"""

    def __init__(
        self,
        client: CenterClient,
        journal: Journal,
        *,
        task_id: str,
        attempt_id: str,
        lease_token: str,
        lease_seconds: int = JOB_LEASE_SECONDS,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.journal = journal
        self.task_id = str(task_id)
        self.attempt_id = str(attempt_id)
        self.lease_token = str(lease_token)
        self.lease_seconds = int(lease_seconds)
        self.log = logger or (lambda message: None)
        self.endpoint_supported: bool | None = None
        self.lease_lost = False
        self.last_status = ""
        self.last_lease_expires_at: str | None = None
        self._warned = False

    def payload(self, phase: str = STATUS_RUNNING) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "lease_token": self.lease_token,
            "phase": phase,
            "lease_seconds": self.lease_seconds,
            "agent_version": AGENT_VERSION,
            "executor_version": EXECUTOR_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "sent_at": utc_now(),
        }

    def _mark_lost(self, reason: str) -> None:
        self.lease_lost = True
        self.journal.mark_unknown(
            task_id=self.task_id, attempt_id=self.attempt_id, reason=reason[:500]
        )
        self.log(f"任务租约丢失（{reason}），结果标记为 unknown，等待人工核对（reconcile）")

    def renew_once(self, phase: str = STATUS_RUNNING) -> dict[str, Any]:
        """续租一次；返回 ``{ok, supported, lease_expires_at, lease_lost, conflict}``。"""
        if self.lease_lost:
            return {"ok": False, "supported": self.endpoint_supported, "lease_lost": True}
        try:
            response = self.client.job_heartbeat(self.task_id, self.payload(phase))
        except EndpointUnsupported:
            self.endpoint_supported = False
            if not self._warned:
                self._warned = True
                self.log(
                    "中心未实现 /agent/jobs/{id}/heartbeat，任务租约不续租"
                    "（沿用后端 10 分钟租约）"
                )
            return {"ok": False, "supported": False, "lease_lost": False}
        except CenterError as exc:
            # 409：旧 attempt / 终态 / 租约过期 —— 不再续租，直接判定丢失
            if exc.status == 409:
                self._mark_lost(f"服务端返回 409：{exc.message}")
                return {"ok": False, "supported": True, "lease_lost": True, "conflict": True}
            failures = self.journal.record_renew_failure(
                task_id=self.task_id, attempt_id=self.attempt_id
            )
            self.log(f"任务租约续租失败（第 {failures} 次）：{exc}")
            if self.journal.lease_lost(failures):
                self._mark_lost(f"租约续租连续失败 {failures} 次")
            return {"ok": False, "supported": True, "lease_lost": self.lease_lost}
        except Exception as exc:  # noqa: BLE001 - 网络/解析异常按失败次数累计
            failures = self.journal.record_renew_failure(
                task_id=self.task_id, attempt_id=self.attempt_id
            )
            self.log(f"任务租约续租失败（第 {failures} 次）：{exc}")
            if self.journal.lease_lost(failures):
                self._mark_lost(f"租约续租连续失败 {failures} 次")
            return {"ok": False, "supported": True, "lease_lost": self.lease_lost}

        self.endpoint_supported = True
        self.journal.reset_renew_failures(task_id=self.task_id, attempt_id=self.attempt_id)
        data = data_of(response)
        self.last_status = str(data.get("status") or "")
        expires = data.get("lease_expires_at")
        if expires:
            self.last_lease_expires_at = str(expires)
            # 用服务端返回的到期时间校准本地租约，避免时钟漂移误判
            self.journal.update_lease(
                task_id=self.task_id,
                attempt_id=self.attempt_id,
                lease_seconds=int(data.get("lease_seconds") or self.lease_seconds),
            )
        else:
            self.journal.touch_lease(task_id=self.task_id, attempt_id=self.attempt_id)
        if bool(data.get("lease_lost") or data.get("cancel_requested")):
            self._mark_lost("服务端要求停止或租约已失效")
        return {
            "ok": True,
            "supported": True,
            "lease_expires_at": expires,
            "lease_lost": self.lease_lost,
            "status": self.last_status,
        }

    def run_forever(
        self,
        stop_event: threading.Event,
        interval: int = JOB_LEASE_RENEW_SECONDS,
        *,
        on_lost: Callable[[], None] | None = None,
    ) -> None:
        while not stop_event.wait(max(5, int(interval))):
            if stop_event.is_set():
                return
            result = self.renew_once()
            if result.get("lease_lost"):
                if on_lost is not None:
                    on_lost()
                return


class UnknownLeaseKeeper:
    """unknown 占用期间的心跳：持续告知中心「结果待核对，勿重派任务」。"""

    def __init__(
        self,
        client: CenterClient,
        journal: Journal,
        *,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.journal = journal
        self.log = logger or (lambda message: None)

    def tick(self) -> dict[str, Any]:
        lease = self.journal.active_lease()
        if not lease or lease.get("phase") != STATUS_UNKNOWN:
            return {"occupied": False}
        renewer = JobLeaseRenewer(
            self.client,
            self.journal,
            task_id=str(lease["task_id"]),
            attempt_id=str(lease["attempt_id"]),
            lease_token=str(lease.get("lease_token") or ""),
            logger=self.log,
        )
        result = renewer.renew_once(phase=STATUS_UNKNOWN)
        return {"occupied": True, "renew": result}


__all__ = ["HeartbeatSender", "JobLeaseRenewer", "UnknownLeaseKeeper"]
