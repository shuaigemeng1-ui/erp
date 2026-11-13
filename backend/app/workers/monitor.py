"""监控后台：公网探测独立队列 + Worker/scheduler 心跳（task-29）。

设计要点：

* 公网探测与调度轮次解耦：``enqueue_probe_node_ids`` 只入队立即返回，
  HTTP/证书探测在独立线程池执行，最大并发 10、单节点预算 10s（由
  ``ops.monitoring.probe_node`` 强制），队列满时返回 ``deferred`` 而不是阻塞；
* Worker / scheduler 进程周期性把心跳写入缓存 Redis（TTL 180s），
  管理中心用 ``component_heartbeat_status`` 判断 ok/stale/unknown；
* 任何 Redis / 网络异常都不影响主进程，只记录日志。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("ventasync.ops.monitor")

# 公网探测独立队列
PROBE_MAX_CONCURRENCY = 10
PROBE_QUEUE_SIZE = 100
PROBE_NODE_BUDGET_SECONDS = 10.0

# Worker / scheduler 心跳
HEARTBEAT_KEY_PREFIX = "vs:ops:component:"
HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_TTL_SECONDS = 180
HEARTBEAT_OK_SECONDS = 90
HEARTBEAT_STALE_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=None).isoformat() + "Z"


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


# --------------------------------------------------------------------------- 心跳
def _heartbeat_key(component: str) -> str:
    return f"{HEARTBEAT_KEY_PREFIX}{component}"


def record_component_heartbeat(
    component: str,
    *,
    redis_client: Any | None = None,
    now: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """写入组件心跳（失败不抛异常）。"""
    payload = {
        "component": str(component)[:32],
        "at": _iso(now or _now()),
        "pid": os.getpid(),
        "host": socket.gethostname()[:128],
    }
    if extra:
        payload.update({str(key)[:32]: value for key, value in extra.items()})
    try:
        if redis_client is None:
            from app.core.redis import cache_redis

            redis_client = cache_redis()
        redis_client.setex(
            _heartbeat_key(component),
            HEARTBEAT_TTL_SECONDS,
            json.dumps(payload, ensure_ascii=False),
        )
        return True
    except Exception as exc:  # noqa: BLE001 - 心跳失败不能影响 Worker/调度
        logger.debug("写入 %s 心跳失败：%s", component, exc)
        return False


def read_component_heartbeat(component: str, *, redis_client: Any | None = None) -> dict[str, Any] | None:
    try:
        if redis_client is None:
            from app.core.redis import cache_redis

            redis_client = cache_redis()
        raw = redis_client.get(_heartbeat_key(component))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 %s 心跳失败：%s", component, exc)
        return None


def component_heartbeat_status(
    component: str,
    *,
    redis_client: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """心跳状态：ok（<=90s）/ stale（<=10min）/ unknown（缺失或过旧）。"""
    moment = now or _now()
    payload = read_component_heartbeat(component, redis_client=redis_client)
    if payload is None:
        return {
            "status": "unknown",
            "message": "无心跳记录（进程可能未启动或 Redis 不可用）",
            "last_heartbeat_at": None,
            "age_seconds": None,
        }
    heartbeat_at = _parse_iso(payload.get("at"))
    last = _iso(heartbeat_at)
    if heartbeat_at is None:
        return {
            "status": "unknown",
            "message": "心跳时间格式不可用",
            "last_heartbeat_at": last,
            "age_seconds": None,
        }
    age = max(0.0, (moment - heartbeat_at).total_seconds())
    if age <= HEARTBEAT_OK_SECONDS:
        status = "ok"
        message = f"最近心跳 {int(age)} 秒前"
    elif age <= HEARTBEAT_STALE_SECONDS:
        status = "stale"
        message = f"心跳已超过 {int(age)} 秒"
    else:
        status = "unknown"
        message = f"心跳过期（{int(age)} 秒前）"
    return {
        "status": status,
        "message": message,
        "last_heartbeat_at": last,
        "age_seconds": int(age),
    }


def start_heartbeat_thread(
    component: str,
    *,
    interval: int = HEARTBEAT_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
    redis_client: Any | None = None,
) -> threading.Thread:
    """后台守护线程：启动立即写一次，此后每 ``interval`` 秒写心跳。"""
    event = stop_event or threading.Event()

    def _loop() -> None:
        while not event.is_set():
            record_component_heartbeat(component, redis_client=redis_client)
            event.wait(max(5, int(interval)))

    thread = threading.Thread(
        target=_loop,
        name=f"ops-heartbeat-{component}",
        daemon=True,
    )
    thread.start()
    return thread


# --------------------------------------------------------------------------- 探测线程池
class ProbeQueue:
    """有界探测线程池：最大并发固定，队列满返回 False（不阻塞调用方）。"""

    def __init__(
        self,
        *,
        max_concurrency: int = PROBE_MAX_CONCURRENCY,
        queue_size: int = PROBE_QUEUE_SIZE,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.queue_size = max(1, int(queue_size))
        self._executor = executor or ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="ops-probe",
        )
        self._lock = threading.Lock()
        self._pending = 0
        self._submitted = 0
        self._completed = 0
        self._failed = 0
        self._closed = False

    # ------------------------------------------------------------------ 提交
    def submit(self, runner: Callable[[], Any], *, label: str = "") -> bool:
        with self._lock:
            if self._closed or self._pending >= self.queue_size:
                return False
            self._pending += 1
            self._submitted += 1
        try:
            future = self._executor.submit(self._run, runner, label)
        except RuntimeError:
            with self._lock:
                self._pending = max(0, self._pending - 1)
            logger.warning("探测线程池已关闭，拒绝任务 %s", label)
            return False
        future.add_done_callback(self._on_done)
        return True

    def _run(self, runner: Callable[[], Any], label: str) -> Any:
        started = time.monotonic()
        try:
            return runner()
        except Exception:  # noqa: BLE001 - 单节点探测失败不影响其他节点
            logger.exception("公网探测任务失败 %s", label)
            raise
        finally:
            elapsed = time.monotonic() - started
            if elapsed > PROBE_NODE_BUDGET_SECONDS:
                logger.warning("公网探测超出单节点预算：%s 用时 %.1fs", label, elapsed)

    def _on_done(self, future: Future) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
            if future.cancelled() or future.exception() is not None:
                self._failed += 1
            else:
                self._completed += 1

    # ------------------------------------------------------------------ 观测/关闭
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_concurrency": self.max_concurrency,
                "queue_size": self.queue_size,
                "pending": self._pending,
                "submitted": self._submitted,
                "completed": self._completed,
                "failed": self._failed,
                "closed": self._closed,
            }

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)


probe_queue = ProbeQueue()


def _probe_node_task(node_id: int) -> dict[str, Any]:
    """线程池任务：独立中心库 Session + 单节点预算 10s。"""
    from app.db.session import center_session
    from app.modules.ops import monitoring

    started = time.monotonic()
    with center_session() as center:
        result = monitoring.probe_node(
            center, int(node_id), budget_seconds=PROBE_NODE_BUDGET_SECONDS
        )
    elapsed = time.monotonic() - started
    if elapsed > PROBE_NODE_BUDGET_SECONDS:
        logger.warning("单节点探测超预算：node=%s 用时 %.1fs", node_id, elapsed)
    return result


def enqueue_probe_node_ids(
    node_ids: Any,
    *,
    queue: ProbeQueue | None = None,
) -> dict[str, Any]:
    """把节点探测入独立队列，立即返回（不等待 HTTP 完成）。"""
    target_queue = queue or probe_queue
    submitted = 0
    deferred = 0
    seen: set[int] = set()
    for raw in node_ids or []:
        try:
            node_id = int(raw)
        except (TypeError, ValueError):
            continue
        if node_id <= 0 or node_id in seen:
            continue
        seen.add(node_id)
        runner = lambda nid=node_id: _probe_node_task(nid)  # noqa: E731
        if target_queue.submit(runner, label=f"node:{node_id}"):
            submitted += 1
        else:
            deferred += 1
    return {
        "submitted": submitted,
        "deferred": deferred,
        "queue": target_queue.snapshot(),
    }


def probe_queue_snapshot() -> dict[str, Any]:
    return probe_queue.snapshot()


__all__ = [
    "PROBE_MAX_CONCURRENCY",
    "PROBE_QUEUE_SIZE",
    "PROBE_NODE_BUDGET_SECONDS",
    "HEARTBEAT_OK_SECONDS",
    "HEARTBEAT_STALE_SECONDS",
    "ProbeQueue",
    "probe_queue",
    "record_component_heartbeat",
    "read_component_heartbeat",
    "component_heartbeat_status",
    "start_heartbeat_thread",
    "enqueue_probe_node_ids",
    "probe_queue_snapshot",
]
