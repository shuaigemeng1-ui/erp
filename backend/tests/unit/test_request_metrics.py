"""task-30：请求性能指标、监控聚合修复、告警通知纯逻辑测试。

覆盖：

* 固定直方图分位数（P95/P99）计算、桶边界与 +Inf 处理；
* 路由归一化（路由模板优先 / 数字与 UUID 降级）、业务路由分类、排除规则；
* 中间件真实记录 2xx/5xx 与路由模板，指标代码异常 fail-open；
* 采集器有界队列 + 批量 Redis 写入（FakeRedis），写失败不影响请求；
* ``/platform/metrics/http`` 响应聚合口径（error_count=4xx+5xx、rate_5xx=0~1）；
* 告警维护窗口 / 重复抑制 / 恢复邮件字段计算（纯逻辑）；
* 历史补报 5m 补算判定、小时桶加权平均、过期清理参数；
* 公网探测耗时按 target 分组（公网 vs 业务耗时分离）。

不连接 MySQL / 真实 Redis / 外网；Redis 使用内存替身。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import request_metrics as core_metrics
from app.modules.ops import alerting, metrics_aggregation, performance


# --------------------------------------------------------------------------- 直方图 / 分位数
def test_histogram_quantile_from_fixed_buckets() -> None:
    counts = core_metrics.bucket_counts_from_values([3, 4, 150, 180])
    assert counts[0] == 2  # <=5ms
    assert counts[5] == 2  # 100~200ms
    assert core_metrics.histogram_quantile(0.5, counts) == pytest.approx(5.0)
    assert core_metrics.histogram_quantile(0.95, counts) == pytest.approx(190.0)
    assert core_metrics.histogram_quantile(0.99, counts) == pytest.approx(198.0)


def test_histogram_quantile_empty_and_inf_bucket() -> None:
    assert core_metrics.histogram_quantile(0.95, [0] * 15) is None
    # 全部落在 +Inf 桶：返回最高有限边界，不伪造更大值
    counts = [0] * (len(core_metrics.HISTOGRAM_BUCKET_BOUNDS_MS) + 1)
    counts[-1] = 10
    assert core_metrics.histogram_quantile(0.99, counts) == pytest.approx(
        core_metrics.HISTOGRAM_BUCKET_BOUNDS_MS[-1]
    )


def test_percentile_exact_values() -> None:
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert core_metrics.percentile(values, 0.5) == pytest.approx(55.0)
    assert core_metrics.percentile(values, 0.95) == pytest.approx(95.5)
    assert core_metrics.percentile([], 0.95) is None


# --------------------------------------------------------------------------- 路由 / 状态分类
def test_fallback_route_normalizes_ids() -> None:
    assert (
        core_metrics.fallback_route("/api/v1/tenants/12/stores/abc")
        == "/api/v1/tenants/{id}/stores/abc"
    )
    uuid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    assert core_metrics.fallback_route(f"/api/v1/orders/{uuid}") == "/api/v1/orders/{id}"
    assert core_metrics.fallback_route("/api/v1/stores") == "/api/v1/stores"


def test_normalize_route_prefers_route_template() -> None:
    route = SimpleNamespace(path="/api/v1/items/{item_id}")
    assert (
        core_metrics.normalize_route({"route": route}, "/api/v1/items/123")
        == "/api/v1/items/{item_id}"
    )


def test_status_class_and_error_flags() -> None:
    assert core_metrics.status_class(200) == "2xx"
    assert core_metrics.status_class(301) == "3xx"
    assert core_metrics.status_class(404) == "4xx"
    assert core_metrics.status_class(503) == "5xx"
    assert core_metrics.status_class(None) == "5xx"
    assert core_metrics.is_error_status(404) is True
    assert core_metrics.is_error_status(200) is False
    assert core_metrics.is_server_error(500) is True


def test_route_recording_and_business_classification(monkeypatch) -> None:
    assert core_metrics.route_is_recorded("/api/v1/tenants/1/stores") is True
    assert core_metrics.route_is_recorded("/healthz") is False
    assert core_metrics.route_is_recorded("/api/v1/agent/telemetry") is False
    assert core_metrics.route_is_recorded("/api/v1/platform/metrics/http") is False
    assert core_metrics.is_business_route("/api/v1/tenants/1/stores") is True
    assert core_metrics.is_business_route("/api/v1/platform/tenants") is False
    monkeypatch.setattr(core_metrics.settings, "request_metrics_enabled", False)
    assert core_metrics.route_is_recorded("/api/v1/tenants/1/stores") is False
    monkeypatch.setattr(core_metrics.settings, "request_metrics_enabled", True)


# --------------------------------------------------------------------------- 字段编码 / 增量
def test_increment_encoding_roundtrip_and_slow_count() -> None:
    moment = datetime(2026, 1, 1, 10, 0, 5)
    samples = [
        {"method": "get", "route": "/api/v1/x", "status_code": 200, "duration_ms": 42, "at": moment},
        {"method": "GET", "route": "/api/v1/x", "status_code": 500, "duration_ms": 1500, "at": moment},
    ]
    increments = core_metrics.build_increments(samples, slow_threshold_ms=1000)
    minute = core_metrics.minute_epoch(moment)
    fields = increments[minute]
    assert fields[core_metrics.encode_field("GET", "/api/v1/x", "2xx", 3)] == 1
    assert fields[core_metrics.encode_field("GET", "/api/v1/x", "5xx", 10)] == 1
    assert fields[core_metrics.encode_sum_field("GET", "/api/v1/x")] == 1542
    assert fields[core_metrics.encode_slow_field("GET", "/api/v1/x")] == 1

    decoded = core_metrics.decode_field(core_metrics.encode_field("GET", "/api/v1/x", "2xx", 3))
    assert decoded == ("count", ("GET", "2xx", "/api/v1/x", "3"))
    assert core_metrics.decode_field("bad-field") is None


def test_merge_increments_adds_buckets() -> None:
    target = {1: {"a": 1}}
    core_metrics.merge_increments(target, {1: {"a": 2, "b": 1}, 2: {"c": 1}})
    assert target == {1: {"a": 3, "b": 1}, 2: {"c": 1}}


# --------------------------------------------------------------------------- Redis 替身
class _FakePipeline:
    def __init__(self, redis: "_FakeRedis") -> None:
        self.redis = redis
        self.ops: list[tuple] = []

    def hincrby(self, key: str, field: str, delta: int) -> None:
        self.ops.append(("hincrby", key, field, int(delta)))

    def expire(self, key: str, ttl: int) -> None:
        self.ops.append(("expire", key, int(ttl)))

    def execute(self) -> list:
        for op in self.ops:
            if op[0] == "hincrby":
                _, key, field, delta = op
                bucket = self.redis.store.setdefault(key, {})
                bucket[field] = int(bucket.get(field, 0)) + delta
            else:
                _, key, ttl = op
                self.redis.ttl[key] = ttl
        return []

    def hgetall(self, key: str):
        raise AssertionError("hgetall 应直接调用 redis")


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, int]] = {}
        self.ttl: dict[str, int] = {}

    def pipeline(self, transaction: bool = False) -> _FakePipeline:  # noqa: ARG002
        return _FakePipeline(self)

    def hgetall(self, key: str) -> dict[str, int]:
        return dict(self.store.get(key, {}))


def test_collector_flush_writes_redis_and_read_window(monkeypatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr("app.core.redis.cache_redis", lambda: fake)
    collector = core_metrics.RequestMetricsCollector(auto_start=False)
    moment = datetime(2026, 1, 1, 10, 0, 5)
    collector.record(method="GET", route="/api/v1/x", status_code=200, duration_ms=30, at=moment)
    collector.record(method="GET", route="/api/v1/x", status_code=500, duration_ms=1500, at=moment)
    written = collector.flush()
    assert written > 0
    key = core_metrics.http_metrics_key(core_metrics.minute_epoch(moment))
    assert fake.store[key]
    assert any(field.startswith("c|GET|5xx|/api/v1/x|") for field in fake.store[key])
    assert fake.ttl[key] >= 3600


def test_read_increments_returns_window_buckets() -> None:
    fake = _FakeRedis()
    moment = datetime(2026, 1, 1, 10, 0, 30)
    minute = core_metrics.minute_epoch(moment)
    fake.store[core_metrics.http_metrics_key(minute)] = {"c|GET|2xx|/x|1": 2}

    class _ReadPipeline:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def hgetall(self, key: str) -> None:
            self.keys.append(key)

        def execute(self) -> list:
            return [dict(fake.store.get(key, {})) for key in self.keys]

    fake.pipeline = lambda transaction=False: _ReadPipeline()  # type: ignore[assignment]
    increments = core_metrics.read_increments(fake, window_seconds=300, now=moment)
    assert increments[minute]["c|GET|2xx|/x|1"] == 2
    # 窗口外（更早）的桶不会被读取
    old = core_metrics.http_metrics_key(minute - 3600)
    fake.store[old] = {"c|GET|2xx|/old|1": 1}
    increments = core_metrics.read_increments(fake, window_seconds=300, now=moment)
    assert old not in increments


# --------------------------------------------------------------------------- 中间件
def _build_app(monkeypatch) -> tuple[FastAPI, core_metrics.RequestMetricsCollector]:
    collector = core_metrics.RequestMetricsCollector(auto_start=False)
    monkeypatch.setattr(core_metrics, "get_collector", lambda: collector)
    app = FastAPI()
    app.add_middleware(core_metrics.RequestMetricsMiddleware)

    @app.get("/api/v1/demo")
    def demo() -> dict:
        return {"ok": True}

    @app.get("/api/v1/items/{item_id}")
    def item(item_id: int) -> dict:
        return {"id": item_id}

    @app.get("/api/v1/boom")
    def boom() -> dict:
        raise RuntimeError("boom")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app, collector


def test_middleware_records_route_template_and_status(monkeypatch) -> None:
    app, collector = _build_app(monkeypatch)
    client = TestClient(app)
    assert client.get("/api/v1/demo").status_code == 200
    assert client.get("/api/v1/items/123").status_code == 200
    assert client.get("/healthz").status_code == 200
    fields = [field for bucket in collector.memory_increments(minutes=5).values() for field in bucket]
    assert any("c|GET|2xx|/api/v1/demo|" in field for field in fields)
    # 路径参数使用路由模板，避免每个 ID 生成一条路由
    assert any("c|GET|2xx|/api/v1/items/{item_id}|" in field for field in fields)
    assert not any("/healthz" in field for field in fields)


def test_middleware_records_5xx_and_fail_open(monkeypatch) -> None:
    app, collector = _build_app(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/v1/boom").status_code == 500
    fields = [field for bucket in collector.memory_increments(minutes=5).values() for field in bucket]
    assert any("c|GET|5xx|/api/v1/boom|" in field for field in fields)
    # 指标写 Redis 失败不得影响业务响应
    fake_redis = _FakeRedis()

    def broken_pipeline(transaction: bool = False):  # noqa: ARG001
        raise RuntimeError("redis down")

    fake_redis.pipeline = broken_pipeline  # type: ignore[assignment]
    monkeypatch.setattr("app.core.redis.cache_redis", lambda: fake_redis)
    assert client.get("/api/v1/demo").status_code == 200


def test_metrics_middleware_installed_in_main_app() -> None:
    from app.main import app

    names = [getattr(item.cls, "__name__", "") for item in app.user_middleware]
    assert "RequestMetricsMiddleware" in names


# --------------------------------------------------------------------------- performance 聚合口径
def test_collect_route_stats_and_payload_thresholds() -> None:
    increments = {
        1: {
            core_metrics.encode_field("GET", "/api/v1/a", "2xx", 3): 8,
            core_metrics.encode_field("GET", "/api/v1/a", "5xx", 10): 2,
            core_metrics.encode_sum_field("GET", "/api/v1/a"): 2000,
            core_metrics.encode_slow_field("GET", "/api/v1/a"): 1,
        }
    }
    stats = performance._collect_route_stats(increments)
    payload = performance._stats_payload(stats[("GET", "/api/v1/a")], slow_threshold_ms=500)
    assert payload["request_count"] == 10
    assert payload["error_count"] == 2
    assert payload["error_5xx"] == 2
    assert payload["rate_5xx"] == pytest.approx(0.2)
    assert payload["avg_ms"] == pytest.approx(200.0)
    assert payload["p95_ms"] is not None
    assert payload["slow_count"] == 1


def test_performance_overview_contract_and_degraded_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        performance, "_load_window_increments", lambda window: ({}, True, "metrics_store_unavailable")
    )
    data = performance.performance_overview(900)
    for key in (
        "request_count",
        "error_count",
        "error_5xx",
        "rate_5xx",
        "p95_ms",
        "p99_ms",
        "window_seconds",
        "routes",
        "degraded",
        "enabled",
    ):
        assert key in data
    assert data["window_seconds"] == 900
    assert data["degraded"] is True
    assert data["request_count"] == 0
    assert data["routes"] == []
    assert data["status"] == "unknown"
    assert set(alerting.default_thresholds()) >= {"cpu", "memory", "disk", "agent", "public", "request"}


def test_monitoring_overview_parts_shape(monkeypatch) -> None:
    monkeypatch.setattr(performance, "_load_window_increments", lambda window: ({}, False, ""))
    parts = performance.monitoring_overview_parts()
    assert set(parts) == {"performance", "business_requests"}
    assert parts["performance"]["window_seconds"] >= 60
    assert parts["business_requests"]["request_count"] == 0


def test_probe_latency_payload_groups_targets_and_series() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    rows = [
        SimpleNamespace(target="root", url="https://x/", ok=True, latency_ms=100, checked_at=now),
        SimpleNamespace(target="root", url="https://x/", ok=False, latency_ms=900, checked_at=now + timedelta(minutes=1)),
        SimpleNamespace(target="api", url="https://x/api/v1/runtime", ok=True, latency_ms=20, checked_at=now),
    ]
    data = performance._probe_payload(
        rows, domain="x.example.com", node_id=1, instance_id=2, hours=24
    )
    assert data["node_id"] == "1" and data["domain"] == "x.example.com"
    assert {item["target"] for item in data["targets"]} == {"root", "api"}
    root = next(item for item in data["targets"] if item["target"] == "root")
    assert root["samples"] == 2
    assert root["ok_count"] == 1
    assert root["ok_rate"] == pytest.approx(0.5)
    assert root["avg_ms"] == pytest.approx(500.0)
    assert len(data["series"]) == 3


# --------------------------------------------------------------------------- 告警通知纯逻辑
def test_alert_suppression_in_maintenance_and_remind(monkeypatch) -> None:
    monkeypatch.setattr(alerting.settings, "alert_remind_seconds", 1800)
    now = datetime(2026, 1, 1, 12, 0, 0)
    maintenance = SimpleNamespace(
        starts_at=now - timedelta(minutes=10),
        ends_at=now + timedelta(minutes=30),
        reason="升级维护",
    )
    alert = SimpleNamespace(
        status="open",
        instance_id=1,
        last_notified_at=now - timedelta(minutes=5),
        resolved_at=None,
        first_seen_at=now - timedelta(minutes=10),
    )
    fields = alerting.alert_suppression_fields(alert, maintenance=maintenance, now=now)
    assert fields["in_maintenance"] is True
    assert fields["maintenance_reason"] == "升级维护"
    assert fields["suppressed"] is True
    assert fields["suppress_until"] is not None  # max(维护结束, 提醒窗口)
    assert fields["recovery_notified_at"] is None

    # 无维护窗口但仍在 30 分钟提醒抑制内
    remind_only = alerting.alert_suppression_fields(alert, maintenance=None, now=now)
    assert remind_only["in_maintenance"] is False
    assert remind_only["suppressed"] is True
    assert remind_only["suppress_until"] is not None

    # 超过提醒间隔后允许再次通知
    later = alerting.alert_suppression_fields(
        alert, maintenance=None, now=now + timedelta(hours=2)
    )
    assert later["suppressed"] is False
    assert later["suppress_until"] is None


def test_alert_recovery_notified_fields() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0)
    resolved = SimpleNamespace(
        status="resolved",
        instance_id=1,
        first_seen_at=now - timedelta(hours=1),
        resolved_at=now - timedelta(minutes=2),
        last_notified_at=now - timedelta(minutes=1),
        notify_count=2,
    )
    fields = alerting.alert_suppression_fields(resolved, maintenance=None, now=now)
    assert fields["suppressed"] is False
    assert fields["recovery_notified_at"] is not None
    assert alerting.recovery_notified_at(resolved) is not None

    not_sent = SimpleNamespace(
        status="resolved",
        instance_id=1,
        resolved_at=now,
        last_notified_at=now - timedelta(minutes=30),
        notify_count=1,
    )
    assert alerting.recovery_notified_at(not_sent) is None


# --------------------------------------------------------------------------- 聚合修复
def test_bucket_helpers() -> None:
    moment = datetime(2026, 1, 1, 10, 7, 31)
    assert metrics_aggregation.bucket_start_5m(moment) == datetime(2026, 1, 1, 10, 5, 0)
    assert metrics_aggregation.hourly_bucket_start(moment) == datetime(2026, 1, 1, 10, 0, 0)


def test_bucket_needs_recompute_for_backfilled_samples() -> None:
    assert metrics_aggregation.bucket_needs_recompute(None, 3) is True
    assert metrics_aggregation.bucket_needs_recompute(2, 5) is True  # 历史补报
    assert metrics_aggregation.bucket_needs_recompute(5, 5) is False
    assert metrics_aggregation.bucket_needs_recompute(5, 0) is False
    assert metrics_aggregation.bucket_needs_recompute(5, 5, force=True) is True


def test_aggregate_sample_rows_includes_disk_max() -> None:
    now = datetime(2026, 1, 1, 10, 0, 0)
    rows = [
        SimpleNamespace(
            cpu_percent=10,
            memory_percent=40,
            load1=1.0,
            disks=[{"percent": 60}],
            sampled_at=now,
        ),
        SimpleNamespace(
            cpu_percent=30,
            memory_percent=50,
            load1=3.0,
            disks=[{"percent": 90}],
            sampled_at=now + timedelta(seconds=30),
        ),
    ]
    values = metrics_aggregation.aggregate_sample_rows(rows)
    assert values["sample_count"] == 2
    assert values["cpu_avg"] == pytest.approx(20.0)
    assert values["cpu_max"] == pytest.approx(30.0)
    assert values["memory_avg"] == pytest.approx(45.0)
    assert values["load_avg"] == pytest.approx(2.0)
    assert values["disk_max"] == pytest.approx(90.0)


def test_weighted_average_and_hourly_combination() -> None:
    assert metrics_aggregation.weighted_average([10, 20], [1, 3]) == pytest.approx(17.5)
    assert metrics_aggregation.weighted_average([10, 20], [0, 0]) == pytest.approx(15.0)
    assert metrics_aggregation.weighted_average([], []) == 0.0

    rows = [
        SimpleNamespace(
            sample_count=1,
            cpu_avg=10.0,
            cpu_max=10.0,
            memory_avg=30.0,
            memory_max=30.0,
            load_avg=1.0,
            disk_max=50.0,
        ),
        SimpleNamespace(
            sample_count=3,
            cpu_avg=30.0,
            cpu_max=40.0,
            memory_avg=50.0,
            memory_max=60.0,
            load_avg=3.0,
            disk_max=90.0,
        ),
    ]
    values = metrics_aggregation.combine_5m_rows(rows)
    assert values["sample_count"] == 4
    assert values["cpu_avg"] == pytest.approx(25.0)  # (10*1 + 30*3) / 4
    assert values["cpu_max"] == pytest.approx(40.0)
    assert values["memory_avg"] == pytest.approx(45.0)
    assert values["disk_max"] == pytest.approx(90.0)
