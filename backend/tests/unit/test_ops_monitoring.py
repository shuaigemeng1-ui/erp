"""task-29 监控判定与采集准确性测试（SQLite 内存中心库，不连 MySQL/Redis/公网）。"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, MetaData, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import CenterBase
from app.modules.ops import monitoring
from app.modules.ops.models_center import (
    AlertEvent,
    NodeCheck,
    NodeMetricSample,
    ServerNode,
    TenantInstance,
)
from app.modules.ops.schemas import TelemetryPayload
from app.workers import monitor as worker_monitor

CENTER_TABLES = (
    "server_nodes",
    "tenant_instances",
    "node_checks",
    "node_metric_samples",
    "alert_events",
)


def _sqlite_metadata() -> MetaData:
    clone = MetaData()
    for name in CENTER_TABLES:
        table = CenterBase.metadata.tables[name].to_metadata(clone)
        id_column = table.columns.get("id")
        if id_column is not None:
            id_column.type = Integer()
            id_column.autoincrement = True
    return clone


@pytest.fixture()
def center():
    import app.modules.ops.models_center  # noqa: F401

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _sqlite_metadata().create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    session.add(
        TenantInstance(
            id=1,
            tenant_id=1,
            name="演示实例",
            domain="demo.example.com",
            status="active",
        )
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def seed_node(center, *, last_seen: datetime | None = None) -> ServerNode:
    node = ServerNode(
        id=1,
        instance_id=1,
        tenant_id=1,
        node_uuid="11111111-1111-1111-1111-111111111111",
        hostname="node-1",
        token_hash="hash",
        status="online",
        last_seen_at=last_seen or datetime.now(timezone.utc).replace(tzinfo=None),
        enrolled_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    center.add(node)
    center.commit()
    return node


def telemetry(
    *,
    sampled_at: datetime | None = None,
    cpu: float = 10,
    memory: float = 20,
    disks: list[dict] | None = None,
    network: dict | None = None,
) -> TelemetryPayload:
    return TelemetryPayload(
        sampled_at=sampled_at or datetime.now(timezone.utc).replace(tzinfo=None),
        hostname="node-1",
        os_name="Linux",
        arch="x86_64",
        cpu_cores=8,
        memory_total_mb=16000,
        memory_used_mb=3200,
        memory_percent=memory,
        cpu_percent=cpu,
        disks=disks or [],
        network=network or {},
    )


def alerts(center, alert_type: str) -> list[AlertEvent]:
    return list(
        center.execute(
            select(AlertEvent)
            .where(AlertEvent.alert_type == alert_type)
            .order_by(AlertEvent.id.asc())
        ).scalars().all()
    )


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- 公网探测
def test_probe_targets_root_and_api_are_separate():
    assert monitoring.PROBE_TARGETS == (("root", "/"), ("api", "/readyz"))
    assert monitoring.PROBE_ALERT_CONSECUTIVE_FAILURES == 3


def test_api_consecutive_failures_alert_independent_of_home(center):
    seed_node(center)

    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    def fake_get(url: str, *, timeout: float, follow_redirects: bool = False):
        del timeout, follow_redirects
        return Response(200 if url.endswith("/") else 503)

    last = None
    for _ in range(3):
        last = monitoring.probe_node(center, 1, http_get=fake_get, cert_days=lambda *a, **k: None)

    assert last is not None
    assert last["home_ok"] is True
    assert last["api_ok"] is False
    assert last["api_consecutive_failures"] == 3
    assert last["targets"]["root"]["ok"] is True
    assert last["targets"]["api"]["ok"] is False
    assert last["targets"]["api"]["consecutive_failures"] == 3

    api_rows = alerts(center, monitoring.ALERT_API)
    assert len(api_rows) == 1
    assert api_rows[0].status == "open"
    assert "首页正常" in api_rows[0].message
    assert alerts(center, monitoring.ALERT_ROOT) == []

    # 首页一直正常也不会消除 API 告警
    monitoring.probe_node(center, 1, http_get=fake_get, cert_days=lambda *a, **k: None)
    assert alerts(center, monitoring.ALERT_API)[0].status == "open"
    assert alerts(center, monitoring.ALERT_ROOT) == []


def test_root_failures_alert_separately_from_api(center):
    seed_node(center)

    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    def fake_get(url: str, *, timeout: float, follow_redirects: bool = False):
        del timeout, follow_redirects
        return Response(500 if url.endswith("/") else 200)

    for _ in range(3):
        result = monitoring.probe_node(center, 1, http_get=fake_get, cert_days=lambda *a, **k: None)
    assert result["home_ok"] is False
    assert result["api_ok"] is True
    root_rows = alerts(center, monitoring.ALERT_ROOT)
    assert len(root_rows) == 1
    assert root_rows[0].status == "open"
    assert alerts(center, monitoring.ALERT_API) == []


def test_probe_records_budget_exhaustion_without_calling_network(center):
    seed_node(center)
    called = []

    def fake_get(*args, **kwargs):
        called.append(args)
        raise AssertionError("预算耗尽时不应发起网络请求")

    result = monitoring.probe_node(
        center, 1, http_get=fake_get, cert_days=lambda *a, **k: None, budget_seconds=0.01
    )
    assert called == []
    assert all(item["ok"] is False for item in result["checks"])
    assert all("预算耗尽" in item["error"] for item in result["checks"])


def test_public_probe_summary_groups_latest_per_target(center):
    seed_node(center)
    now = _now()
    center.add_all(
        [
            NodeCheck(node_id=1, checked_at=now - timedelta(seconds=60), target="root", url="u1", ok=True, status_code=200, latency_ms=120, error=""),
            NodeCheck(node_id=1, checked_at=now, target="root", url="u2", ok=True, status_code=200, latency_ms=80, error=""),
            NodeCheck(node_id=1, checked_at=now, target="api", url="u3", ok=False, status_code=503, latency_ms=30, error="HTTP 503"),
        ]
    )
    center.commit()
    summary = {item["target"]: item for item in monitoring.public_probe_summary(center)}
    assert summary["root"]["status"] == "ok"
    assert summary["root"]["latency_ms"] == 80
    assert summary["api"]["status"] == "failed"
    assert summary["api"]["failed_nodes"] == 1


# --------------------------------------------------------------------------- 持续判定
def test_sustained_40s_sampling_still_triggers():
    now = _now()
    samples = [(now - timedelta(seconds=600 - index * 40), 90.0) for index in range(16)]
    result = monitoring.evaluate_sustained(
        samples, threshold=85, duration_seconds=600, now=now
    )
    assert result["active"] is True
    assert result["sample_count"] == 16
    assert result["run_seconds"] >= 600


def test_sustained_gap_breaks_continuity():
    now = _now()
    samples = [
        (now - timedelta(seconds=900), 95.0),
        (now - timedelta(seconds=860), 95.0),
        # 5 分钟断档
        (now - timedelta(seconds=300), 95.0),
        (now - timedelta(seconds=260), 95.0),
    ]
    result = monitoring.evaluate_sustained(
        samples, threshold=85, duration_seconds=600, now=now
    )
    assert result["active"] is False
    assert result["run_seconds"] < 600


def test_sustained_history_is_not_active():
    now = _now()
    samples = [(now - timedelta(seconds=3600 - index * 40), 95.0) for index in range(16)]
    result = monitoring.evaluate_sustained(
        samples, threshold=85, duration_seconds=600, now=now
    )
    assert result["active"] is False


def test_cpu_alert_uses_duration_not_fixed_sample_count(center):
    node = seed_node(center)
    now = _now()
    for index in range(15):
        center.add(
            NodeMetricSample(
                node_id=node.id,
                sampled_at=now - timedelta(seconds=600 - index * 40),
                received_at=now,
                cpu_percent=90,
                memory_percent=30,
            )
        )
    center.commit()
    result = monitoring.ingest_telemetry(center, node, telemetry(cpu=90, sampled_at=now))
    assert result["live"] is True
    cpu_rows = alerts(center, monitoring.ALERT_CPU)
    assert len(cpu_rows) == 1
    assert cpu_rows[0].status == "open"
    assert "持续 10 分钟" in cpu_rows[0].message


def test_memory_alert_uses_duration_and_continuity(center):
    node = seed_node(center)
    now = _now()
    for index in range(8):
        center.add(
            NodeMetricSample(
                node_id=node.id,
                sampled_at=now - timedelta(seconds=320 - index * 40),
                received_at=now,
                cpu_percent=10,
                memory_percent=95,
            )
        )
    center.commit()
    monitoring.ingest_telemetry(center, node, telemetry(cpu=10, memory=95, sampled_at=now))
    memory_rows = alerts(center, monitoring.ALERT_MEMORY)
    assert len(memory_rows) == 1
    assert memory_rows[0].status == "open"


# --------------------------------------------------------------------------- 历史样本
def test_historical_sample_does_not_fake_health(center):
    old_seen = _now() - timedelta(seconds=3600)
    node = seed_node(center, last_seen=old_seen)
    node.last_metrics_at = old_seen
    center.commit()
    historical_at = _now() - timedelta(minutes=30)

    result = monitoring.ingest_telemetry(
        center,
        node,
        telemetry(
            sampled_at=historical_at,
            cpu=99,
            memory=99,
            disks=[{"mount": "/", "total_gb": 100, "percent": 99}],
        ),
    )
    assert result["historical"] is True
    assert result["live"] is False
    assert node.status == "online"
    assert node.last_seen_at == old_seen
    assert node.last_metrics_at == old_seen
    assert alerts(center, monitoring.ALERT_CPU) == []
    assert alerts(center, monitoring.ALERT_MEMORY) == []
    assert alerts(center, monitoring.ALERT_DISK) == []


def test_historical_sample_does_not_resolve_existing_alerts(center):
    node = seed_node(center)
    now = _now()
    center.add(
        AlertEvent(
            node_id=node.id,
            instance_id=node.instance_id,
            alert_type=monitoring.ALERT_CPU,
            severity="warning",
            status="open",
            message="CPU 高",
            metric_value="95%",
            threshold="85%",
            first_seen_at=now - timedelta(minutes=20),
            last_seen_at=now - timedelta(minutes=20),
            created_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=20),
        )
    )
    center.commit()
    monitoring.ingest_telemetry(
        center,
        node,
        telemetry(sampled_at=now - timedelta(minutes=30), cpu=1, memory=1),
    )
    assert alerts(center, monitoring.ALERT_CPU)[0].status == "open"


# --------------------------------------------------------------------------- 数据盘/网络
def test_data_disk_and_network_use_actual_sampled_delta(center):
    node = seed_node(center)
    now = _now()
    disks = [
        {"mount": "/boot", "total_gb": 1.0, "percent": 80.0},
        {"mount": "/", "total_gb": 20.0, "percent": 30.0},
        {"mount": "/data", "total_gb": 100.0, "percent": 40.0},
    ]
    monitoring.ingest_telemetry(
        center,
        node,
        telemetry(
            sampled_at=now - timedelta(seconds=40),
            disks=disks,
            network={"rx_bytes": 1000, "tx_bytes": 500, "rx_bytes_per_sec": 1, "tx_bytes_per_sec": 1},
        ),
    )
    monitoring.ingest_telemetry(
        center,
        node,
        telemetry(
            sampled_at=now,
            disks=disks,
            network={"rx_bytes": 3000, "tx_bytes": 1500, "rx_bytes_per_sec": 1, "tx_bytes_per_sec": 1},
        ),
    )
    latest = center.execute(
        select(NodeMetricSample)
        .where(NodeMetricSample.node_id == node.id)
        .order_by(NodeMetricSample.sampled_at.desc())
        .limit(1)
    ).scalar_one()
    assert latest.network["rx_bytes_per_sec"] == 50.0
    assert latest.network["tx_bytes_per_sec"] == 25.0
    assert latest.network["interval_seconds"] == 40.0
    assert node.disk_total_gb == 100
    disk = monitoring.data_disk_summary(latest.disks)
    assert disk is not None
    assert disk["mount"] == "/data"
    assert disk["percent"] == 40.0


def test_network_rate_handles_counter_reset(center):
    summary = monitoring.network_with_actual_rates(
        {"rx_bytes": 10, "tx_bytes": 10}, {"rx_bytes": 1000, "tx_bytes": 1000}, 30
    )
    assert summary["rx_bytes_per_sec"] == 0.0
    assert summary["tx_bytes_per_sec"] == 0.0


# --------------------------------------------------------------------------- 分项状态
def test_components_status(center, monkeypatch):
    monkeypatch.setattr(monitoring, "_ping_center", lambda c: None)
    monkeypatch.setattr(monitoring, "_ping_redis", lambda: None)
    monkeypatch.setattr(monitoring, "_business_db_status", lambda c: ("ok", "业务库连接正常"))
    monkeypatch.setattr(
        monitoring,
        "_component_heartbeat",
        lambda name: {
            "worker": {"status": "ok", "message": "最近心跳 5 秒前", "last_heartbeat_at": "2026-11-13T07:00:00Z"},
            "scheduler": {"status": "ok", "message": "最近心跳 6 秒前", "last_heartbeat_at": "2026-11-13T07:00:01Z"},
        }[name],
    )
    components = monitoring.components_status(center)
    assert [item["name"] for item in components] == [
        "identity_db",
        "business_db",
        "redis",
        "worker",
        "scheduler",
    ]
    assert all(item["status"] == "ok" for item in components)
    assert monitoring.overall_status(components) == "healthy"
    assert all("message" in item and "latency_ms" in item for item in components)

    monkeypatch.setattr(
        monitoring,
        "_component_heartbeat",
        lambda name: {"status": "stale", "message": "心跳已超过 300 秒", "last_heartbeat_at": None},
    )
    components = monitoring.components_status(center)
    assert monitoring.overall_status(components) == "degraded"

    def broken_ping(center_arg):
        raise RuntimeError("db down")

    monkeypatch.setattr(monitoring, "_ping_center", broken_ping)
    components = monitoring.components_status(center)
    statuses = {item["name"]: item["status"] for item in components}
    assert statuses["identity_db"] == "error"
    assert monitoring.overall_status(components) == "unhealthy"


def test_monitoring_overview_contract(center, monkeypatch):
    monkeypatch.setattr(monitoring, "_ping_center", lambda c: None)
    monkeypatch.setattr(monitoring, "_ping_redis", lambda: None)
    monkeypatch.setattr(monitoring, "_business_db_status", lambda c: ("unknown", "中心模式"))
    monkeypatch.setattr(
        monitoring,
        "_component_heartbeat",
        lambda name: {"status": "unknown", "message": "无心跳", "last_heartbeat_at": None},
    )
    monkeypatch.setattr(
        monitoring,
        "performance_parts",
        lambda: {
            "performance": {"request_count": 10, "error_count": 1, "error_5xx": 0, "rate_5xx": 0.0, "p95_ms": 12.0, "p99_ms": 30.0, "window_seconds": 900, "routes": []},
            "business_requests": {"request_count": 8, "error_count": 0, "error_5xx": 0, "rate_5xx": 0.0, "p95_ms": 9.0, "p99_ms": 20.0, "window_seconds": 900},
        },
    )
    overview = monitoring.monitoring_overview(center)
    assert overview["overall"] in {"healthy", "degraded", "unhealthy"}
    assert isinstance(overview["components"], list)
    assert overview["performance"]["request_count"] == 10
    assert overview["business_requests"]["request_count"] == 8
    assert [item["target"] for item in overview["public_probes"]] == ["root", "api"]
    assert overview["checked_at"].endswith("Z")


# --------------------------------------------------------------------------- 心跳 / 队列
class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.data[key] = value

    def get(self, key: str):
        return self.data.get(key)


def test_component_heartbeat_status_ok_stale_unknown():
    fake = FakeRedis()
    now = _now()
    assert worker_monitor.record_component_heartbeat("worker", redis_client=fake, now=now) is True
    status = worker_monitor.component_heartbeat_status(
        "worker", redis_client=fake, now=now + timedelta(seconds=20)
    )
    assert status["status"] == "ok"
    assert status["age_seconds"] == 20
    assert status["last_heartbeat_at"].endswith("Z")

    stale = worker_monitor.component_heartbeat_status(
        "worker", redis_client=fake, now=now + timedelta(seconds=300)
    )
    assert stale["status"] == "stale"
    expired = worker_monitor.component_heartbeat_status(
        "worker", redis_client=fake, now=now + timedelta(seconds=700)
    )
    assert expired["status"] == "unknown"
    missing = worker_monitor.component_heartbeat_status("scheduler", redis_client=fake)
    assert missing["status"] == "unknown"


def test_probe_queue_bounded_and_non_blocking():
    queue = worker_monitor.ProbeQueue(max_concurrency=3, queue_size=5)
    release = threading.Event()
    active = 0
    lock = threading.Lock()

    def runner():
        nonlocal active
        with lock:
            active += 1
        release.wait(timeout=5)
        with lock:
            active -= 1

    for _ in range(5):
        assert queue.submit(runner, label="test")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and active < 3:
        time.sleep(0.02)
    assert active == 3
    assert queue.snapshot()["pending"] == 5
    assert queue.submit(runner, label="overflow") is False
    release.set()
    queue.shutdown(wait=True)
    snapshot = queue.snapshot()
    assert snapshot["completed"] == 5
    assert snapshot["pending"] == 0


def test_enqueue_probe_node_ids_returns_immediately(monkeypatch):
    calls: list[int] = []

    def slow_probe(node_id: int):
        calls.append(node_id)
        time.sleep(0.2)
        return {"node_id": node_id}

    monkeypatch.setattr(worker_monitor, "_probe_node_task", slow_probe)
    queue = worker_monitor.ProbeQueue(max_concurrency=2, queue_size=10)
    started = time.monotonic()
    result = worker_monitor.enqueue_probe_node_ids([1, 2, 3, 4, 5, 5], queue=queue)
    elapsed = time.monotonic() - started
    assert elapsed < 0.2
    assert result["submitted"] == 5
    assert result["deferred"] == 0
    queue.shutdown(wait=True)
    assert sorted(calls) == [1, 2, 3, 4, 5]


def test_probe_queue_max_concurrency_default_is_ten():
    assert worker_monitor.PROBE_MAX_CONCURRENCY == 10
    assert worker_monitor.PROBE_NODE_BUDGET_SECONDS == 10


# --------------------------------------------------------------------------- service / router 接线
def test_service_delegates_to_monitoring(center, monkeypatch):
    from app.core.auth import AuthenticatedUser
    from app.modules.ops import service
    from app.modules.ops import monitoring_router

    node = seed_node(center)
    called: dict[str, object] = {}

    def fake_ingest(center_arg, node_arg, payload_arg):
        called["ingest"] = (center_arg, node_arg, payload_arg)
        return {"ok": True, "live": True}

    def fake_probe(center_arg, node_id):
        called["probe"] = node_id
        return {"node_id": str(node_id), "targets": {}}

    monkeypatch.setattr(monitoring, "ingest_telemetry", fake_ingest)
    monkeypatch.setattr(monitoring, "probe_node", fake_probe)
    assert service.ingest_telemetry(center, node, telemetry())["ok"] is True
    assert called["ingest"][1] is node
    assert service.probe_node(center, node.id)["node_id"] == "1"
    assert called["probe"] == node.id

    admin = AuthenticatedUser(
        user_id=1,
        email="admin@example.com",
        display_name="Admin",
        status="active",
        is_platform_admin=True,
    )
    monkeypatch.setattr(
        monitoring,
        "components_status",
        lambda c, **kwargs: [
            {"name": "identity_db", "label": "身份库", "status": "ok", "message": "", "latency_ms": 1}
        ],
    )
    monkeypatch.setattr(monitoring, "public_probe_summary", lambda c, **kwargs: [])
    monkeypatch.setattr(
        monitoring,
        "performance_parts",
        lambda: {"performance": {"request_count": 0}, "business_requests": {"request_count": 0}},
    )
    overview = monitoring_router.monitoring_overview(center, admin)["data"]
    assert set(overview) == {
        "overall",
        "checked_at",
        "components",
        "performance",
        "public_probes",
        "business_requests",
    }
    status = monitoring_router.monitoring_status(center, admin)["data"]
    assert status["components"][0]["name"] == "identity_db"


def test_metric_payload_includes_data_disk(center):
    from app.modules.ops import service

    node = seed_node(center)
    now = _now()
    monitoring.ingest_telemetry(
        center,
        node,
        telemetry(
            sampled_at=now,
            disks=[
                {"mount": "/boot", "total_gb": 1, "percent": 90},
                {"mount": "/data", "total_gb": 200, "percent": 55},
            ],
        ),
    )
    payload = service._metric_payload(service._latest_metric(center, node.id))
    assert payload["data_disk"]["mount"] == "/data"
    assert payload["data_disk"]["percent"] == 55.0


def test_run_center_maintenance_enqueues_probe_without_blocking(center, monkeypatch):
    from app.modules.ops import service

    seed_node(center)
    captured: dict[str, object] = {}

    def fake_enqueue(node_ids):
        captured["ids"] = list(node_ids)
        return {"submitted": 1, "deferred": 0, "queue": {"pending": 1}}

    monkeypatch.setattr(worker_monitor, "enqueue_probe_node_ids", fake_enqueue)
    monkeypatch.setattr(service, "mark_stale_nodes_offline", lambda c: 0)
    monkeypatch.setattr(service, "aggregate_metrics", lambda c, **k: 0)
    monkeypatch.setattr(service, "mark_stale_operations", lambda c: 0)
    monkeypatch.setattr(service, "check_backup_alerts", lambda c: 0)
    monkeypatch.setattr(service, "notify_open_alerts", lambda c: 0)
    monkeypatch.setattr(service, "cleanup_old_monitoring_data", lambda c: {"metrics": 0, "checks": 0})
    service._last_probe_at = None
    result = service.run_center_maintenance(center, probe_interval_seconds=0)
    assert result["probed"] == 1
    assert result["probe_deferred"] == 0
    assert captured["ids"] == [1]
    service._last_probe_at = None
