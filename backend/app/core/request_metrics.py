"""HTTP 请求性能指标：延迟直方图、路由归一化与轻量中间件。

设计目标（plan 批次3「请求性能与告警通知」）：

* **真实请求量 / 5xx 比例**：中间件按真实响应状态码计数，不依赖日志采样；
* **路由级延迟直方图**：固定边界桶（毫秒）累计请求数，P95/P99 由直方图累积计数
  插值计算，比保存每条样本更省内存，也不受慢请求采样偏差影响；
* **业务耗时与公网探测耗时分离**：本模块只统计应用内业务请求处理耗时；
  公网探测耗时来自 ``node_checks``（运维模块），两者在页面分开展示；
* **不落业务库、不新增迁移**：按分钟桶写缓存 Redis（默认保留 24h），Redis 异常时
  fail-open：请求照常处理，指标降级为进程内最近窗口样本并标记 ``degraded``；
* **不阻塞请求路径**：中间件只做 O(1) 内存累加 + 入有界队列，由后台守护线程批量
  写 Redis；队列满时丢弃指标而不是拖慢业务请求。

本模块只依赖标准库、``app.core.config`` 与 ``app.core.redis``（惰性连接），
导入不产生网络/数据库副作用，便于单元测试。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from app.core.config import settings

logger = logging.getLogger("ventasync.metrics.http")

# --------------------------------------------------------------------------- 常量
# 固定延迟直方图边界（毫秒）：最后一档为 +Inf。边界选择偏向 Web 接口关注区间。
HISTOGRAM_BUCKET_BOUNDS_MS: tuple[float, ...] = (
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    200.0,
    300.0,
    500.0,
    800.0,
    1200.0,
    2000.0,
    3000.0,
    5000.0,
    10000.0,
)
INF_BUCKET_INDEX = len(HISTOGRAM_BUCKET_BOUNDS_MS)
MINUTE_SECONDS = 60
METRICS_KEY_PREFIX = "vs:metrics:http"
# 健康检查 / 运行时探针永远不计入业务请求
ALWAYS_EXCLUDED_PATHS = frozenset(
    {"/healthz", "/readyz", "/livez", "/metrics", "/openapi.json", "/docs", "/redoc"}
)
STATUS_CLASSES: tuple[str, ...] = ("1xx", "2xx", "3xx", "4xx", "5xx")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HEX_ID_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")
_SAFE_ROUTE_MAX_LENGTH = 200


# --------------------------------------------------------------------------- 纯函数
def utc_now() -> datetime:
    """当前 UTC 时间（naive，与项目其他模块保持一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def minute_epoch(moment: datetime | None = None) -> int:
    """把时间归一到「分钟起点」的 Unix 秒，作为 Redis 分钟桶键。"""
    value = moment or utc_now()
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return int(value.replace(second=0, microsecond=0).timestamp())


def http_metrics_key(epoch_second: int) -> str:
    return f"{METRICS_KEY_PREFIX}:{int(epoch_second)}"


def status_class(status_code: int | None) -> str:
    """HTTP 状态码分组：1xx/2xx/3xx/4xx/5xx；异常按 5xx 计。"""
    try:
        code = int(status_code or 0)
    except (TypeError, ValueError):
        code = 0
    if code <= 0:
        return "5xx"
    if 100 <= code < 200:
        return "1xx"
    if 200 <= code < 300:
        return "2xx"
    if 300 <= code < 400:
        return "3xx"
    if 400 <= code < 500:
        return "4xx"
    return "5xx"


def is_error_status(status_code: int | None) -> bool:
    try:
        return int(status_code or 0) >= 400
    except (TypeError, ValueError):
        return True


def is_server_error(status_code: int | None) -> bool:
    try:
        return int(status_code or 0) >= 500
    except (TypeError, ValueError):
        return True


def is_business_route(route: str) -> bool:
    """业务路由：排除平台管理、Agent 上报与监控自身接口。"""
    path = route or ""
    for prefix in (
        f"{settings.api_prefix}/platform",
        f"{settings.api_prefix}/agent",
        f"{settings.api_prefix}/monitoring",
    ):
        if path.startswith(prefix):
            return False
    return True


def route_is_recorded(path: str) -> bool:
    """是否统计该请求：仅业务 API 前缀，排除健康检查 / Agent / 监控自身。"""
    if not settings.request_metrics_enabled:
        return False
    candidate = path or ""
    if candidate in ALWAYS_EXCLUDED_PATHS:
        return False
    prefix = settings.api_prefix
    if not candidate.startswith(prefix):
        return False
    excluded = [
        item.strip()
        for item in (settings.request_metrics_exclude_prefixes or "").split(",")
        if item.strip()
    ]
    return not any(candidate.startswith(item) for item in excluded)


def _is_identifier_segment(segment: str) -> bool:
    if not segment:
        return False
    if segment.isdigit():
        return True
    if _UUID_RE.match(segment):
        return True
    if _HEX_ID_RE.match(segment):
        return True
    # 很长的 token（会话 / 签名 / 文件名）也归并为 {id}，避免路由爆炸
    return len(segment) >= 24 and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", segment))


def fallback_route(path: str) -> str:
    """无法取得 FastAPI 路由模板时的降级归一化：数字/UUID/长 token → ``{id}``。"""
    segments = (path or "/").split("/")
    normalized = ["{id}" if _is_identifier_segment(segment) else segment for segment in segments]
    route = "/".join(normalized) or "/"
    return route[:_SAFE_ROUTE_MAX_LENGTH]


def normalize_route(scope: Mapping[str, Any], path: str) -> str:
    """优先使用命中的路由模板（如 ``/api/v1/tenants/{tenant_id}/stores``）。"""
    route = scope.get("route") if isinstance(scope, Mapping) else None
    template = getattr(route, "path", None)
    if isinstance(template, str) and template.startswith("/"):
        return template[:_SAFE_ROUTE_MAX_LENGTH]
    return fallback_route(path)


def bucket_index(duration_ms: float) -> int:
    """返回延迟落在的直方图桶下标；超出最大边界进入 +Inf 桶。"""
    try:
        value = max(0.0, float(duration_ms))
    except (TypeError, ValueError):
        value = 0.0
    for index, bound in enumerate(HISTOGRAM_BUCKET_BOUNDS_MS):
        if value <= bound:
            return index
    return INF_BUCKET_INDEX


def bucket_counts_from_values(values: Iterable[float]) -> list[int]:
    """把一组耗时映射为每桶计数（长度为 ``len(bounds)+1``）。"""
    counts = [0] * (len(HISTOGRAM_BUCKET_BOUNDS_MS) + 1)
    for value in values:
        counts[bucket_index(value)] += 1
    return counts


def histogram_quantile(
    quantile: float, bucket_counts: Iterable[int], bounds: tuple[float, ...] | None = None
) -> float | None:
    """由固定直方图桶计数计算分位数（Prometheus ``histogram_quantile`` 同款插值）。

    * 入参是**每桶**（非累积）计数，最后一个桶为 +Inf；
    * 落在 +Inf 桶时返回最高有限边界（无法得知真实上界，不夸大也不伪造）；
    * 无样本返回 ``None``。
    """
    edges = bounds or HISTOGRAM_BUCKET_BOUNDS_MS
    counts = [max(0, int(value or 0)) for value in bucket_counts]
    total = sum(counts)
    if total <= 0:
        return None
    ratio = min(1.0, max(0.0, float(quantile)))
    rank = ratio * total
    cumulative = 0
    for index, count in enumerate(counts):
        if count <= 0:
            continue
        cumulative += count
        if cumulative < rank:
            continue
        lower = 0.0 if index == 0 else float(edges[index - 1])
        if index >= len(edges):
            return round(lower, 3)
        upper = float(edges[index])
        previous = cumulative - count
        fraction = (rank - previous) / count if count else 0.0
        return round(lower + (upper - lower) * fraction, 3)
    return round(float(edges[-1]), 3) if edges else None


def percentile(values: Iterable[float], quantile: float) -> float | None:
    """精确分位数（少量样本 / 公网探测历史用），最近秩插值。"""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    ratio = min(1.0, max(0.0, float(quantile)))
    position = ratio * (len(ordered) - 1)
    lower_index = int(position)
    upper_index = min(len(ordered) - 1, lower_index + 1)
    fraction = position - lower_index
    return round(ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction, 3)


def _safe_route(route: str) -> str:
    """字段编码不允许出现 ``|``，统一替换为 ``:``。"""
    return (route or "/").replace("|", ":")[:_SAFE_ROUTE_MAX_LENGTH]


def encode_field(method: str, route: str, status: str, bucket: int) -> str:
    return f"c|{method.upper()}|{status}|{_safe_route(route)}|{int(bucket)}"


def encode_sum_field(method: str, route: str) -> str:
    return f"ms|{method.upper()}|{_safe_route(route)}"


def encode_slow_field(method: str, route: str) -> str:
    return f"s|{method.upper()}|{_safe_route(route)}"


def decode_field(field: str) -> tuple[str, tuple[str, ...]] | None:
    """解析 Redis 哈希字段：``c|GET|2xx|/api/...|3`` / ``ms|GET|/api/...``。"""
    parts = str(field or "").split("|")
    if len(parts) < 3:
        return None
    kind = parts[0]
    if kind == "c" and len(parts) >= 5:
        try:
            bucket = int(parts[-1])
        except ValueError:
            return None
        return "count", (parts[1], parts[2], "|".join(parts[3:-1]), str(bucket))
    if kind in {"ms", "s"} and len(parts) >= 3:
        return ("sum" if kind == "ms" else "slow"), (parts[1], "|".join(parts[2:]))
    return None


def build_increments(
    samples: Iterable[Mapping[str, Any]], *, slow_threshold_ms: int | None = None
) -> dict[int, dict[str, int]]:
    """把一批请求样本聚合为「分钟 → 哈希字段增量」，便于 Redis pipeline 写入。"""
    threshold = int(
        slow_threshold_ms
        if slow_threshold_ms is not None
        else getattr(settings, "request_metrics_slow_ms", 1000)
    )
    increments: dict[int, dict[str, int]] = {}
    for sample in samples:
        try:
            moment = sample.get("at")
            if isinstance(moment, (int, float)):
                minute = int(moment) // MINUTE_SECONDS * MINUTE_SECONDS
            else:
                minute = minute_epoch(moment if isinstance(moment, datetime) else None)
            method = str(sample.get("method") or "GET").upper()[:16]
            route = _safe_route(str(sample.get("route") or "/"))
            code = int(sample.get("status_code") or 500)
            duration = max(0.0, float(sample.get("duration_ms") or 0.0))
        except (TypeError, ValueError):
            continue
        fields = increments.setdefault(minute, {})
        count_field = encode_field(method, route, status_class(code), bucket_index(duration))
        fields[count_field] = fields.get(count_field, 0) + 1
        fields[encode_sum_field(method, route)] = fields.get(encode_sum_field(method, route), 0) + int(
            round(duration)
        )
        if duration >= threshold:
            fields[encode_slow_field(method, route)] = fields.get(encode_slow_field(method, route), 0) + 1
    return increments


def merge_increments(
    target: dict[int, dict[str, int]], source: Mapping[int, Mapping[str, int]]
) -> None:
    for minute, fields in source.items():
        bucket = target.setdefault(int(minute), {})
        for field, delta in fields.items():
            bucket[field] = bucket.get(field, 0) + int(delta)


# --------------------------------------------------------------------------- Redis 读写
def write_increments(
    redis_client: Any,
    increments: Mapping[int, Mapping[str, int]],
    *,
    ttl_seconds: int | None = None,
) -> int:
    """批量写 Redis（pipeline HINCRBY + EXPIRE）；返回写入的字段数。"""
    if not increments:
        return 0
    ttl = int(ttl_seconds or max(3600, settings.request_metrics_retention_hours * 3600))
    pipeline = redis_client.pipeline(transaction=False)
    written = 0
    for minute, fields in increments.items():
        if not fields:
            continue
        key = http_metrics_key(int(minute))
        for field, delta in fields.items():
            if not delta:
                continue
            pipeline.hincrby(key, field, int(delta))
            written += 1
        pipeline.expire(key, ttl)
    pipeline.execute()
    return written


def read_increments(
    redis_client: Any, *, window_seconds: int, now: datetime | None = None
) -> dict[int, dict[str, int]]:
    """读取窗口内所有分钟桶（pipeline HGETALL），键不存在视为空桶。"""
    window = max(MINUTE_SECONDS, min(int(window_seconds), settings.request_metrics_retention_hours * 3600))
    end = minute_epoch(now)
    start = minute_epoch((now or utc_now()) - timedelta(seconds=window))
    minutes = list(range(start, end + 1, MINUTE_SECONDS))
    if not minutes:
        return {}
    pipeline = redis_client.pipeline(transaction=False)
    for minute in minutes:
        pipeline.hgetall(http_metrics_key(minute))
    raw_results = pipeline.execute()
    result: dict[int, dict[str, int]] = {}
    for minute, raw in zip(minutes, raw_results):
        if not raw:
            continue
        fields: dict[str, int] = {}
        items = raw.items() if isinstance(raw, Mapping) else raw
        for field, value in items:
            try:
                fields[str(field)] = int(value)
            except (TypeError, ValueError):
                continue
        if fields:
            result[int(minute)] = fields
    return result


# --------------------------------------------------------------------------- 采集器
class RequestMetricsCollector:
    """有界队列 + 后台批写线程；队列满/Pipeline 失败都只丢指标，绝不影响请求。"""

    def __init__(
        self,
        *,
        max_queue: int | None = None,
        flush_interval_seconds: float = 1.0,
        auto_start: bool = True,
    ) -> None:
        self.max_queue = int(max_queue or 20000)
        self.flush_interval_seconds = max(0.2, float(flush_interval_seconds))
        self._queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=self.max_queue)
        self._memory: "OrderedDict[int, dict[str, int]]" = OrderedDict()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._auto_start = auto_start
        self.dropped = 0
        self.written_fields = 0
        self.flush_failures = 0
        self.last_error = ""
        self._last_error_log_at = 0.0

    # ------------------------------------------------------------------ 记录
    def record(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_ms: float,
        at: datetime | None = None,
    ) -> None:
        sample = {
            "method": method,
            "route": route,
            "status_code": status_code,
            "duration_ms": duration_ms,
            "at": at or utc_now(),
        }
        try:
            self._increment_memory(sample)
        except Exception:  # noqa: BLE001 - 指标失败绝不抛出
            pass
        try:
            self._queue.put_nowait(sample)
        except queue.Full:
            self.dropped += 1
            return
        if self._auto_start and self._thread is None:
            self._start()

    def _increment_memory(self, sample: Mapping[str, Any]) -> None:
        increments = build_increments([sample])
        with self._lock:
            merge_increments(self._memory, increments)
            keep = max(2, int(settings.request_metrics_memory_minutes) + 2)
            while len(self._memory) > keep:
                self._memory.popitem(last=False)

    # ------------------------------------------------------------------ 线程
    def _start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="http-metrics-writer", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        batch: list[dict[str, Any]] = []
        while not self._stop.is_set():
            try:
                sample = self._queue.get(timeout=self.flush_interval_seconds)
                batch.append(sample)
                while len(batch) < 500:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass
            except Exception:  # noqa: BLE001
                pass
            if batch:
                self._flush_batch(batch)
                batch = []

    def _flush_batch(self, batch: list[dict[str, Any]]) -> None:
        try:
            increments = build_increments(batch)
            written = self._write(increments)
            self.written_fields += written
        except Exception as exc:  # noqa: BLE001 - Redis 异常 fail-open
            self.flush_failures += 1
            self.last_error = str(exc)[:200]
            now = time.monotonic()
            if now - self._last_error_log_at > 60:
                self._last_error_log_at = now
                logger.warning("请求指标写 Redis 失败（fail-open）：%s", self.last_error)

    def _write(self, increments: Mapping[int, Mapping[str, int]]) -> int:
        from app.core.redis import cache_redis

        return write_increments(cache_redis(), increments)

    def flush(self) -> int:
        """同步清空队列（测试 / 进程退出 / 基准用），返回写入字段数。"""
        batch: list[dict[str, Any]] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        increments = build_increments(batch)
        return self._write(increments)

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 内存降级快照
    def memory_increments(self, *, minutes: int | None = None, now: datetime | None = None) -> dict[int, dict[str, int]]:
        keep_minutes = max(1, int(minutes or settings.request_metrics_memory_minutes))
        cutoff = minute_epoch((now or utc_now()) - timedelta(minutes=keep_minutes))
        with self._lock:
            return {
                minute: dict(fields)
                for minute, fields in self._memory.items()
                if minute >= cutoff
            }

    def stats(self) -> dict[str, int]:
        return {
            "dropped": int(self.dropped),
            "written_fields": int(self.written_fields),
            "flush_failures": int(self.flush_failures),
            "pending": int(self._queue.qsize()),
        }


_collector_lock = threading.Lock()
_collector: RequestMetricsCollector | None = None


def get_collector() -> RequestMetricsCollector:
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = RequestMetricsCollector()
    return _collector


def reset_collector() -> None:
    """测试用：丢弃全局采集器（不停止线程，守护线程会自然退出）。"""
    global _collector
    with _collector_lock:
        if _collector is not None:
            _collector.stop()
        _collector = None


# --------------------------------------------------------------------------- 中间件
class RequestMetricsMiddleware:
    """轻量 **纯 ASGI** 请求指标中间件（不额外创建 task，开销最小）。

    * 在 ``http.response.start`` 时记录路由模板、状态码与处理耗时；
    * 端点异常未产生响应时补记 5xx 后原样抛出；
    * 健康检查 / Agent / 指标自身接口不统计；
    * 指标记录任何异常都被吞掉（fail-open），绝不影响业务响应。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if not route_is_recorded(path):
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        recorded = False

        def _record(status_code: int) -> None:
            nonlocal recorded
            if recorded:
                return
            recorded = True
            try:
                duration_ms = (time.perf_counter() - started) * 1000.0
                route = normalize_route(scope, path)
                get_collector().record(
                    method=str(scope.get("method") or "GET"),
                    route=route,
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
            except Exception:  # noqa: BLE001 - fail-open
                pass

        async def send_wrapper(message: Any) -> None:
            if isinstance(message, Mapping) and message.get("type") == "http.response.start":
                try:
                    status_code = int(message.get("status") or 500)
                except (TypeError, ValueError):
                    status_code = 500
                _record(status_code)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            _record(500)
            raise


__all__ = [
    "HISTOGRAM_BUCKET_BOUNDS_MS",
    "INF_BUCKET_INDEX",
    "RequestMetricsCollector",
    "RequestMetricsMiddleware",
    "bucket_counts_from_values",
    "bucket_index",
    "build_increments",
    "decode_field",
    "fallback_route",
    "get_collector",
    "histogram_quantile",
    "http_metrics_key",
    "is_business_route",
    "is_error_status",
    "is_server_error",
    "merge_increments",
    "minute_epoch",
    "normalize_route",
    "percentile",
    "read_increments",
    "reset_collector",
    "route_is_recorded",
    "status_class",
    "write_increments",
]
