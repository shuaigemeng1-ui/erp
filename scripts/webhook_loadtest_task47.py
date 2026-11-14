#!/usr/bin/env python3
"""Task-47：隔离环境百店 Webhook 长稳压测与故障演练。

独立编排器，复用 ``scripts/webhook_loadtest.py`` 发送 100 店/20 events/s 签名事件；
本地临时 Gateway SQLite + 本地 mock 租户 sink，不连接生产、不启动生产容器。

场景（默认 60 分钟）：
* 0..30min：100 店 20/s，热店/重复/乱序/部分订单；
* 期间注入 relay 429、sink 回执丢失、短时 worker/Redis 不可用；
* 30..60min：租户离线（sink 503），新流量持续写入 Gateway；
* 60min 恢复 sink，等待 Gateway 追平（最长 90min drain），计算追平吞吐。

所有指标写入 ``scripts/reports/task47-<run_id>/``（可改），最终报告由
``docs/webhook-loadtest-task47.md`` 汇总。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
LOADTEST = SCRIPTS_DIR / "webhook_loadtest.py"
sys.path.insert(0, str(SCRIPTS_DIR))
import webhook_loadtest as lt  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SinkState:
    def __init__(self, relay_secret: str) -> None:
        self.relay_secret = relay_secret
        self.lock = threading.Lock()
        self.down = False
        self.records: dict[str, dict[str, Any]] = {}
        self.requests = 0
        self.down_responses = 0
        self.rate_limit_responses = 0
        self.receipt_loss_responses = 0
        self.signature_failures = 0
        self.rate_limit_remaining = 0
        self.receipt_loss_remaining = 0
        self.timeline: list[dict[str, Any]] = []
        self.started_at = time.time()

    def set_down(self, value: bool, label: str = "") -> None:
        with self.lock:
            self.down = value
            self.timeline.append(
                {
                    "at_seconds": round(time.time() - self.started_at, 3),
                    "action": "sink_down" if value else "sink_up",
                    "label": label,
                }
            )

    def set_rate_limit(self, count: int, label: str = "") -> None:
        with self.lock:
            self.rate_limit_remaining = max(0, int(count))
            self.timeline.append(
                {
                    "at_seconds": round(time.time() - self.started_at, 3),
                    "action": "inject_429",
                    "count": int(count),
                    "label": label,
                }
            )

    def set_receipt_loss(self, count: int, label: str = "") -> None:
        with self.lock:
            self.receipt_loss_remaining = max(0, int(count))
            self.timeline.append(
                {
                    "at_seconds": round(time.time() - self.started_at, 3),
                    "action": "inject_receipt_loss",
                    "count": int(count),
                    "label": label,
                }
            )

    def record_success(self, notification_id: str, order_ids: list[str], partial: bool) -> bool:
        now = utc_now()
        with self.lock:
            self.requests += 1
            existing = self.records.get(notification_id)
            if existing is not None:
                existing["delivery_count"] = int(existing["delivery_count"]) + 1
                existing["duplicate_delivery_count"] = (
                    int(existing["duplicate_delivery_count"]) + 1
                )
                return True
            self.records[notification_id] = {
                "notification_id": notification_id,
                "order_ids": order_ids,
                "partial": bool(partial),
                "first_processed_at": now,
                "processed_at": now,
                "delivery_count": 1,
                "duplicate_delivery_count": 0,
                "inventory_movements": 1,
            }
            return False

    def summary(self) -> dict[str, Any]:
        with self.lock:
            items = list(self.records.values())
            return {
                "unique_processed": len(items),
                "delivery_count": sum(int(item["delivery_count"]) for item in items),
                "duplicate_delivery_count": sum(
                    int(item["duplicate_delivery_count"]) for item in items
                ),
                "partial_events": sum(1 for item in items if item["partial"]),
                "requests": self.requests,
                "down_responses": self.down_responses,
                "rate_limit_responses": self.rate_limit_responses,
                "receipt_loss_responses": self.receipt_loss_responses,
                "signature_failures": self.signature_failures,
                "timeline": list(self.timeline),
            }


def make_sink_handler(state: SinkState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "erp-task47-sink/1.0"

        def log_message(self, *args: Any) -> None:
            return

        def _json(
            self, code: int, payload: dict[str, Any], headers: dict[str, str] | None = None
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/healthz":
                self._json(200, {"ok": True})
                return
            if parsed.path == "/summary":
                self._json(200, state.summary())
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path != "/api/v1/integrations/tiktok/webhook/relay":
                self._json(404, {"error": "not found"})
                return
            raw_body = self.rfile.read(int(self.headers.get("content-length", "0")))
            with state.lock:
                down = state.down
            if down:
                with state.lock:
                    state.down_responses += 1
                self._json(503, {"error": "tenant offline"})
                return
            timestamp = self.headers.get("x-gateway-timestamp", "").strip()
            signature = self.headers.get("x-gateway-signature", "").strip()
            expected = hmac.new(
                state.relay_secret.encode("utf-8"),
                timestamp.encode("utf-8") + b"." + raw_body,
                hashlib.sha256,
            ).hexdigest()
            if not timestamp or not signature or not hmac.compare_digest(expected, signature):
                with state.lock:
                    state.signature_failures += 1
                self._json(401, {"error": "relay signature invalid"})
                return
            with state.lock:
                rate_limit = state.rate_limit_remaining > 0
                if rate_limit:
                    state.rate_limit_remaining -= 1
                    state.rate_limit_responses += 1
                receipt_loss = state.receipt_loss_remaining > 0
                if receipt_loss:
                    state.receipt_loss_remaining -= 1
                    state.receipt_loss_responses += 1
            if rate_limit:
                self._json(429, {"error": "rate limited"}, {"Retry-After": "1"})
                return
            if receipt_loss:
                # HTTP 200 + 非严格回执：当前 Gateway 会重试，用于模拟回执丢失。
                self._json(200, {"ok": True})
                return
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid json"})
                return
            notification_id = str(
                payload.get("tts_notification_id") or payload.get("notification_id") or ""
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            order_ids: list[str] = []
            for key in ("order_id", "main_order_id"):
                if data.get(key):
                    order_ids.append(str(data[key]))
            orders = data.get("orders")
            if isinstance(orders, list):
                for item in orders:
                    if isinstance(item, dict):
                        value = item.get("order_id") or item.get("main_order_id")
                        if value and str(value) not in order_ids:
                            order_ids.append(str(value))
            partial = bool((data.get("_loadtest") or {}).get("partial")) if isinstance(
                data.get("_loadtest"), dict
            ) else False
            duplicate = state.record_success(notification_id, order_ids, partial)
            self._json(
                200,
                {
                    "received": True,
                    "duplicate": duplicate,
                    "mapping_missing": False,
                    "reason": "ok",
                },
            )

    return Handler


class SinkServer:
    def __init__(self, relay_secret: str) -> None:
        self.state = SinkState(relay_secret)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_sink_handler(self.state))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_http_ok(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"等待 {url} 超时：{last_error}")


def start_gateway(
    db_path: str,
    port: int,
    relay_secret: str,
    admin_token: str,
    log_path: Path,
    *,
    retry_max_seconds: int = 300,
    retry_base_seconds: int = 15,
    dispatch_concurrency: int = 16,
    sync_mode: str = "NORMAL",
) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "GATEWAY_DB_PATH": db_path,
            "GATEWAY_SECRET_KEY": "task47-gateway-secret-key-32-chars",
            "GATEWAY_ADMIN_TOKEN": admin_token,
            "GATEWAY_CHANNELS": "tiktok",
            "GATEWAY_ENVIRONMENTS": "production",
            "GATEWAY_RATE_LIMIT_PER_MINUTE": "100000",
            "GATEWAY_WEBHOOK_RELAY_SECRET": relay_secret,
            "GATEWAY_WEBHOOK_DISPATCH_ENABLED": "true",
            "GATEWAY_WEBHOOK_DISPATCH_INTERVAL_SECONDS": "1",
            "GATEWAY_WEBHOOK_DISPATCH_GLOBAL_CONCURRENCY": str(dispatch_concurrency),
            "GATEWAY_WEBHOOK_DISPATCH_TENANT_CONCURRENCY": "8",
            "GATEWAY_WEBHOOK_DISPATCH_STORE_CONCURRENCY": "1",
            "GATEWAY_WEBHOOK_CLAIM_BATCH_SIZE": "200",
            "GATEWAY_WEBHOOK_LEASE_SECONDS": "60",
            "GATEWAY_WEBHOOK_DELIVERY_TIMEOUT_SECONDS": "10",
            "GATEWAY_WEBHOOK_RETRY_BASE_SECONDS": str(retry_base_seconds),
            "GATEWAY_WEBHOOK_RETRY_MAX_SECONDS": str(retry_max_seconds),
            "GATEWAY_WEBHOOK_RETRY_JITTER_PERCENT": "20",
            "GATEWAY_SQLITE_SYNCHRONOUS": sync_mode,
            "GATEWAY_SQLITE_BUSY_TIMEOUT_MS": "10000",
            "GATEWAY_PURGE_EXPIRED_ON_HEALTH": "true",
        }
    )
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "gateway_app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(REPO_ROOT / "gateway"),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    process._task47_log_file = log_file  # type: ignore[attr-defined]
    return process


def stop_gateway(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
    log_file = getattr(process, "_task47_log_file", None)
    if log_file:
        log_file.close()


def seed_gateway(
    gateway_url: str,
    *,
    admin_token: str,
    app_key: str,
    app_secret: str,
    sink_callback_url: str,
    shop_ids: list[str],
) -> dict[str, Any]:
    headers = {"X-Gateway-Token": admin_token}
    with httpx.Client(base_url=gateway_url, timeout=20) as client:
        response = client.put(
            "/api/v1/gateway/channel-apps/tiktok/production",
            headers=headers,
            json={"app_key": app_key, "app_secret": app_secret},
        )
        response.raise_for_status()
        response = client.post(
            "/api/v1/gateway/admin/instances",
            headers=headers,
            json={
                "tenant_id": 999047,
                "name": "task47-loadtest",
                "channels": ["tiktok"],
                "environments": ["production"],
                "store_ids": [int(item) for item in shop_ids],
                "callback_url": sink_callback_url,
                "notes": "task-47 isolated load test; delete with temp db",
            },
        )
        response.raise_for_status()
        payload = response.json()
        instance = payload["instance"]
        token = payload["instance_token"]
        instance_headers = {"Authorization": f"Bearer {token}"}
        for shop_id in shop_ids:
            response = client.post(
                "/api/v1/gateway/instance/webhook-shops",
                headers=instance_headers,
                json={"store_id": int(shop_id), "platform_shop_id": shop_id},
            )
            response.raise_for_status()
    return {"instance": instance, "instance_token": token}


class FaultScheduler(threading.Thread):
    def __init__(
        self,
        sink: SinkState,
        *,
        start_monotonic: float,
        outage_start: float,
        outage_duration: float,
        fault_429_at: float,
        worker_fault_at: float,
        receipt_fault_at: float,
        duration: float,
    ) -> None:
        super().__init__(daemon=True)
        self.sink = sink
        self.start_monotonic = start_monotonic
        self.outage_start = outage_start
        self.outage_duration = outage_duration
        self.fault_429_at = fault_429_at
        self.worker_fault_at = worker_fault_at
        self.receipt_fault_at = receipt_fault_at
        self.duration = duration
        self.stop_event = threading.Event()
        self.actions = self._build_actions()

    def _build_actions(self) -> list[tuple[float, str, Any]]:
        actions: list[tuple[float, str, Any]] = []
        if 0 <= self.fault_429_at < self.duration:
            actions.append((self.fault_429_at, "429", 20))
        if 0 <= self.worker_fault_at < self.duration:
            actions.append((self.worker_fault_at, "worker_down", None))
            actions.append((self.worker_fault_at + 60, "worker_up", None))
        if 0 <= self.receipt_fault_at < self.duration:
            actions.append((self.receipt_fault_at, "receipt_loss", 20))
        if self.outage_duration > 0 and self.outage_start < self.duration:
            actions.append((self.outage_start, "tenant_down", None))
            actions.append(
                (
                    min(self.duration, self.outage_start + self.outage_duration),
                    "tenant_up",
                    None,
                )
            )
        return sorted(actions, key=lambda item: item[0])

    def run(self) -> None:
        for due, action, value in self.actions:
            wait = self.start_monotonic + due - time.monotonic()
            if wait > 0 and self.stop_event.wait(wait):
                return
            if action == "429":
                self.sink.set_rate_limit(int(value), label="relay_429")
            elif action == "receipt_loss":
                self.sink.set_receipt_loss(int(value), label="http_receipt_loss")
            elif action == "worker_down":
                self.sink.set_down(True, label="worker_redis_outage_sim")
            elif action == "worker_up":
                self.sink.set_down(False, label="worker_redis_recovery_sim")
            elif action == "tenant_down":
                self.sink.set_down(True, label="tenant_offline_30m")
            elif action == "tenant_up":
                self.sink.set_down(False, label="tenant_online")

    def stop(self) -> None:
        self.stop_event.set()


def read_snapshot(gateway_db: str, prefix: str) -> dict[str, Any]:
    connection = lt.open_gateway_db_readonly(gateway_db)
    try:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM gateway_webhook_events "
            "WHERE notification_id LIKE ? GROUP BY status",
            (f"{prefix}-%",),
        ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        pending = connection.execute(
            "SELECT COUNT(*) FROM gateway_webhook_events "
            "WHERE notification_id LIKE ? AND status='pending'",
            (f"{prefix}-%",),
        ).fetchone()[0]
        oldest = connection.execute(
            "SELECT MIN(created_at) FROM gateway_webhook_events "
            "WHERE notification_id LIKE ? AND status='pending'",
            (f"{prefix}-%",),
        ).fetchone()[0]
        return {
            "at": utc_now(),
            "counts": counts,
            "pending": int(pending),
            "oldest_pending": oldest,
        }
    finally:
        connection.close()


def monitor_snapshots(
    gateway_db: str,
    prefix: str,
    stop_event: threading.Event,
    snapshots: list[dict[str, Any]],
    sink: SinkState,
) -> None:
    while not stop_event.is_set():
        try:
            snapshot = read_snapshot(gateway_db, prefix)
            snapshot["sink_unique"] = len(sink.records)
            snapshot["sink_down"] = sink.down
            snapshots.append(snapshot)
        except Exception as exc:  # noqa: BLE001 - monitor must not stop load
            snapshots.append({"at": utc_now(), "error": str(exc)[:200]})
        stop_event.wait(30)


def compute_task_metrics(
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    sink: SinkState,
    timeline: list[dict[str, Any]],
    outage_start_epoch: float,
    outage_end_epoch: float,
) -> dict[str, Any]:
    by_notification = {str(row.get("notification_id")): row for row in rows}
    e2e_all: list[float] = []
    e2e_baseline: list[float] = []
    e2e_outage: list[float] = []
    delivered_after_recovery = 0
    delivered_times: list[float] = []
    missing_in_sink: list[str] = []
    with sink.lock:
        sink_records = dict(sink.records)
    for notification_id, row in by_notification.items():
        created = lt.epoch_from_iso(str(row.get("created_at") or ""))
        delivered = lt.epoch_from_iso(str(row.get("delivered_at") or ""))
        record = sink_records.get(notification_id)
        processed = lt.epoch_from_iso(str((record or {}).get("processed_at") or ""))
        if created is not None and processed is not None and processed >= created:
            value = (processed - created) * 1000.0
            e2e_all.append(value)
            if created < outage_start_epoch:
                e2e_baseline.append(value)
            else:
                e2e_outage.append(value)
        if delivered is not None:
            delivered_times.append(delivered)
            if delivered >= outage_end_epoch:
                delivered_after_recovery += 1
        if str(row.get("status")) in {"delivered", "unroutable"} and record is None:
            # unroutable 不应出现在这里；只把预期 delivered 的缺失列出
            if str(row.get("status")) == "delivered":
                missing_in_sink.append(notification_id)
    catchup_seconds = None
    catchup_throughput = None
    if delivered_times:
        catchup_seconds = max(0.0, max(delivered_times) - outage_end_epoch)
        if catchup_seconds > 0:
            catchup_throughput = delivered_after_recovery / catchup_seconds
    status_counts = report.get("persistence", {}).get("status_counts", {})
    pending = int(status_counts.get("pending", 0) or 0)
    dead = int(status_counts.get("dead", 0) or 0)
    unroutable = int(status_counts.get("unroutable", 0) or 0)
    duplicate_count = int(report.get("persistence", {}).get("duplicate_count", 0))
    sink_summary = sink.summary()
    ack = report.get("gateway_ack", {})
    baseline_p95 = lt.percentile(e2e_baseline, 0.95)
    conditions = {
        "p99_le_1s": bool(ack.get("latency", {}).get("p99_ms") is not None and ack["latency"]["p99_ms"] <= 1000),
        "e2e_95_within_120s": bool(baseline_p95 is not None and baseline_p95 <= 120000),
        "no_event_loss": (
            int(report.get("persistence", {}).get("accepted_without_row", 0)) == 0
            and not missing_in_sink
            and pending == 0
            and dead == 0
            and unroutable == 0
        ),
        "duplicate_inventory_zero": int(sink_summary["duplicate_delivery_count"]) == 0,
        "catchup_ge_30_per_s_within_60min": bool(
            catchup_seconds is not None
            and catchup_seconds <= 3600
            and catchup_throughput is not None
            and catchup_throughput >= 30
            and pending == 0
        ),
    }
    return {
        "gateway_ack": ack,
        "persistence": report.get("persistence"),
        "gateway_delivery": report.get("delivery"),
        "e2e_all": lt.latency_summary(e2e_all),
        "e2e_baseline_before_outage": lt.latency_summary(e2e_baseline),
        "e2e_outage_created": lt.latency_summary(e2e_outage),
        "sink": sink_summary,
        "gateway_status_counts": status_counts,
        "gateway_duplicate_count": duplicate_count,
        "missing_in_sink": missing_in_sink[:50],
        "missing_in_sink_count": len(missing_in_sink),
        "catchup_seconds_after_recovery": round(catchup_seconds, 3) if catchup_seconds is not None else None,
        "delivered_after_recovery": delivered_after_recovery,
        "catchup_throughput_events_per_second": round(catchup_throughput, 3) if catchup_throughput is not None else None,
        "conditions": conditions,
        "timeline": timeline,
    }


def write_gateway_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "notification_id",
        "platform_shop_id",
        "status",
        "attempts",
        "duplicate_count",
        "created_at",
        "delivered_at",
        "last_attempt_at",
        "last_status_code",
        "failure_class",
        "last_error",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def main() -> int:
    parser = argparse.ArgumentParser(description="Task-47 百店长稳/故障演练编排器")
    parser.add_argument("--shops", type=int, default=100)
    parser.add_argument("--shop-id-base", type=int, default=990000000)
    parser.add_argument("--rate", type=float, default=20.0)
    parser.add_argument("--duration", type=float, default=3600.0)
    parser.add_argument("--outage-start", type=float, default=1800.0)
    parser.add_argument("--outage-duration", type=float, default=1800.0)
    parser.add_argument("--fault-429-at", type=float, default=300.0)
    parser.add_argument("--worker-fault-at", type=float, default=600.0)
    parser.add_argument("--receipt-fault-at", type=float, default=900.0)
    parser.add_argument("--drain-timeout", type=float, default=5400.0)
    parser.add_argument("--report-dir", default="scripts/reports/task47")
    parser.add_argument("--no-outage", action="store_true", help="仅基线，不执行租户离线")
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()

    if args.duration <= 0:
        raise SystemExit("--duration 必须 > 0")
    if not args.no_outage and args.outage_start + args.outage_duration > args.duration + 1:
        raise SystemExit("outage 窗口超出 --duration；请缩短 outage 或增加 duration")

    run_id = f"lt-task47-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    output_dir = (REPO_ROOT / args.report_dir / run_id).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="webhook-task47-"))
    gateway_db = work_dir / "gateway.db"
    gateway_log = work_dir / "gateway.log"
    main_log = output_dir / "main_loadtest.log"
    admin_token = "task47-admin-token"
    relay_secret = "task47-relay-secret-32-characters"
    app_key = "task47-app-key"
    app_secret = "task47-app-secret"
    shop_ids = [str(args.shop_id_base + index) for index in range(args.shops)]
    sink = SinkServer(relay_secret)
    gateway_process: subprocess.Popen | None = None
    scheduler: FaultScheduler | None = None
    snapshot_stop = threading.Event()
    snapshots: list[dict[str, Any]] = []
    monitor: threading.Thread | None = None
    gateway_port = find_free_port()
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    sink_callback_url = f"http://127.0.0.1:{sink.port}/tenant/callback"
    outage_start_epoch = 0.0
    outage_end_epoch = 0.0
    try:
        sink.start()
        gateway_process = start_gateway(
            str(gateway_db),
            gateway_port,
            relay_secret,
            admin_token,
            gateway_log,
            retry_max_seconds=300,
            retry_base_seconds=15,
            dispatch_concurrency=16,
            sync_mode="NORMAL",
        )
        wait_http_ok(f"{gateway_url}/healthz")
        print(f"[task47] gateway={gateway_url} sink={sink.port} db={gateway_db}", flush=True)
        seed_gateway(
            gateway_url,
            admin_token=admin_token,
            app_key=app_key,
            app_secret=app_secret,
            sink_callback_url=sink_callback_url,
            shop_ids=shop_ids,
        )
        print(f"[task47] seeded shops={len(shop_ids)}", flush=True)
        main_start = time.time()
        scheduler = FaultScheduler(
            sink.state,
            start_monotonic=time.monotonic(),
            outage_start=args.outage_start,
            outage_duration=0 if args.no_outage else args.outage_duration,
            fault_429_at=args.fault_429_at,
            worker_fault_at=args.worker_fault_at,
            receipt_fault_at=args.receipt_fault_at,
            duration=args.duration,
        )
        scheduler.start()
        monitor = threading.Thread(
            target=monitor_snapshots,
            args=(str(gateway_db), run_id, snapshot_stop, snapshots, sink.state),
            daemon=True,
        )
        monitor.start()
        command = [
            sys.executable,
            str(LOADTEST),
            "run",
            "--gateway-url",
            gateway_url,
            "--app-key",
            app_key,
            "--app-secret",
            app_secret,
            "--shop-ids",
            ",".join(shop_ids),
            "--rate",
            str(args.rate),
            "--duration",
            str(args.duration),
            "--concurrency",
            "64",
            "--hot-shops",
            "3",
            "--hot-share",
            "0.35",
            "--duplicate-rate",
            "0.02",
            "--duplicate-delay-min",
            "0.05",
            "--duplicate-delay-max",
            "0.8",
            "--out-of-order-rate",
            "0.05",
            "--partial-rate",
            "0.05",
            "--run-id",
            run_id,
            "--mode",
            "isolated",
            "--gateway-db",
            str(gateway_db),
            "--drain-grace",
            "5",
            "--drain-timeout",
            str(args.drain_timeout),
            "--output-dir",
            str(output_dir / "loadtest"),
        ]
        print(f"[task47] start load {args.shops} shops rate={args.rate} duration={args.duration}s", flush=True)
        with main_log.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=str(REPO_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            while process.poll() is None:
                time.sleep(5)
                print(
                    f"[task47] elapsed={round(time.time() - main_start, 1)}s "
                    f"sink_down={sink.state.down} "
                    f"unique={len(sink.state.records)} "
                    f"gateway_groups={snapshots[-1].get('counts') if snapshots else {}}",
                    flush=True,
                )
        snapshot_stop.set()
        if monitor is not None:
            monitor.join(timeout=2)
        report_path = output_dir / "loadtest" / f"webhook-loadtest-{run_id}.json"
        if not report_path.exists():
            print(f"[task47] ERROR main report missing: {report_path}", file=sys.stderr)
            print(main_log.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
            return 2
        report = json.loads(report_path.read_text(encoding="utf-8"))
        rows = lt.query_gateway_events(str(gateway_db), run_id)
        # 按 scheduler 的实际时间线修正恢复时间。
        for event in sink.state.timeline:
            label = str(event.get("label") or "")
            if label == "tenant_offline_30m":
                outage_start_epoch = sink.state.started_at + float(event.get("at_seconds", 0))
            elif label == "tenant_online":
                outage_end_epoch = sink.state.started_at + float(event.get("at_seconds", 0))
        if args.no_outage:
            outage_start_epoch = main_start + args.duration
            outage_end_epoch = main_start + args.duration
        if not outage_start_epoch:
            outage_start_epoch = main_start + args.duration
        if not outage_end_epoch:
            outage_end_epoch = main_start + args.duration
        metrics = compute_task_metrics(
            report,
            rows,
            sink.state,
            sink.state.timeline,
            outage_start_epoch,
            outage_end_epoch,
        )
        raw = {
            "run_id": run_id,
            "generated_at": utc_now(),
            "started_at": datetime.fromtimestamp(main_start, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "finished_at": utc_now(),
            "environment": {
                "gateway_url": gateway_url,
                "gateway_db": str(gateway_db),
                "gateway_count": 1,
                "sink": "local ThreadingHTTPServer",
                "production_connection": False,
                "production_container_started": False,
                "gateway_retry_base_seconds": 15,
                "gateway_retry_max_seconds": 300,
                "dispatch_concurrency": 16,
                "sqlite_synchronous": "NORMAL",
                "repo_head": subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip(),
            },
            "arguments": vars(args),
            "loadtest_report": report,
            "computed": metrics,
            "snapshots": snapshots,
            "main_log_tail": main_log.read_text(encoding="utf-8", errors="replace")[-12000:],
            "gateway_log_tail": gateway_log.read_text(encoding="utf-8", errors="replace")[-8000:],
        }
        raw_path = output_dir / "raw_metrics.json"
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        write_gateway_csv(output_dir / "gateway_events.csv", rows)
        summary_path = output_dir / "summary.json"
        summary_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "generated_at": raw["generated_at"],
                    "environment": raw["environment"],
                    "arguments": raw["arguments"],
                    "computed": metrics,
                    "loadtest_window": report.get("window"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[task47] raw_metrics={raw_path}", flush=True)
        print(f"[task47] summary={summary_path}", flush=True)
        print(f"[task47] gateway_events_csv={output_dir / 'gateway_events.csv'}", flush=True)
        print(json.dumps(metrics["conditions"], ensure_ascii=False), flush=True)
        print(
            f"[task47] ack_p99={metrics['gateway_ack'].get('latency', {}).get('p99_ms')}ms "
            f"e2e_baseline_p95={metrics['e2e_baseline_before_outage'].get('p95_ms')}ms "
            f"catchup={metrics['catchup_seconds_after_recovery']}s "
            f"catchup_rate={metrics['catchup_throughput_events_per_second']}/s "
            f"pending={metrics['gateway_status_counts'].get('pending')}",
            flush=True,
        )
        return 0
    finally:
        snapshot_stop.set()
        if scheduler is not None:
            scheduler.stop()
        if gateway_process is not None:
            stop_gateway(gateway_process)
        sink.stop()
        if not args.keep_temp:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"[task47] kept temp={work_dir}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
