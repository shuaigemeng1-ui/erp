#!/usr/bin/env python3
"""Webhook 压测本地 smoke harness。

启动真实 Gateway（当前仓库 backend/app/modules/gateway）+ 本地模拟租户 relay：
1. 在隔离临时目录创建 Gateway SQLite；
2. 通过 admin/instance API 注册测试实例与 5 个测试店铺；
3. 启动本地 sink，验证 Gateway→tenant relay 签名、记录重复投递/部分订单；
4. 在短窗口内制造 sink 中断，再用 scripts/webhook_loadtest.py 跑小流量；
5. 输出 ACK/持久化/端到端/积压/重复指标，并清理临时数据。

不会写生产，也不修改 Gateway 核心代码。默认事件全部路由到本地临时实例。
"""

from __future__ import annotations

import argparse
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
from urllib.parse import parse_qs, urlsplit

import httpx

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
LOADTEST = SCRIPTS_DIR / "webhook_loadtest.py"


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
        self.items: dict[str, dict[str, Any]] = {}
        self.requests = 0
        self.signature_failures = 0
        self.down_responses = 0

    def set_down(self, value: bool) -> None:
        with self.lock:
            self.down = value

    def record(self, notification_id: str, order_ids: list[str], partial: bool, event_id: str) -> None:
        now = utc_now()
        with self.lock:
            self.requests += 1
            existing = self.items.get(notification_id)
            if existing is None:
                self.items[notification_id] = {
                    "notification_id": notification_id,
                    "order_ids": order_ids,
                    "partial": partial,
                    "first_received_at": now,
                    "processed_at": now,
                    "delivery_count": 1,
                    "duplicate_delivery_count": 0,
                    "inventory_movements": 1,
                    "event_id": event_id,
                }
            else:
                existing["delivery_count"] += 1
                existing["duplicate_delivery_count"] += 1

    def stats(self, prefix: str = "") -> dict[str, Any]:
        with self.lock:
            items = [dict(item) for key, item in self.items.items() if key.startswith(prefix)]
        return {
            "items": items,
            "summary": {
                "unique_events": len(items),
                "delivery_count": sum(int(item["delivery_count"]) for item in items),
                "duplicate_delivery_count": sum(
                    int(item["duplicate_delivery_count"]) for item in items
                ),
                "partial_events": sum(1 for item in items if item["partial"]),
                "requests": self.requests,
                "signature_failures": self.signature_failures,
                "down_responses": self.down_responses,
            },
        }


def make_sink_handler(state: SinkState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "erp-webhook-smoke-sink/1.0"

        def log_message(self, *args: Any) -> None:  # silence
            return

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/stats":
                prefix = parse_qs(parsed.query).get("prefix", [""])[0]
                self._json(200, state.stats(prefix))
                return
            if parsed.path == "/healthz":
                self._json(200, {"ok": True})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path != "/api/v1/integrations/tiktok/webhook/relay":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("content-length", "0"))
            raw_body = self.rfile.read(length)
            with state.lock:
                down = state.down
            if down:
                with state.lock:
                    state.down_responses += 1
                self._json(503, {"error": "temporary sink outage"})
                return
            timestamp = self.headers.get("x-gateway-timestamp", "").strip()
            signature = self.headers.get("x-gateway-signature", "").strip()
            # relay 签名格式：HMAC(secret, timestamp + "." + raw_body)
            import hashlib
            import hmac

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
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid json"})
                return
            notification_id = str(payload.get("tts_notification_id") or payload.get("notification_id") or "")
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
            partial = bool((data.get("_loadtest") or {}).get("partial")) if isinstance(data.get("_loadtest"), dict) else False
            state.record(
                notification_id,
                order_ids,
                partial,
                self.headers.get("x-gateway-event-id", ""),
            )
            # 当前 Gateway 要求租户 relay 返回严格 JSON 回执：
            # received=true 或 duplicate=true 才视为已交付（HTTP 200 但回执无效会重试）。
            self._json(
                200,
                {
                    "received": True,
                    "duplicate": False,
                    "mapping_missing": False,
                    "reason": "ok",
                },
            )

    return Handler


class SinkServer:
    def __init__(self, relay_secret: str) -> None:
        self.state = SinkState(relay_secret)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_sink_handler(self.state))
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


def start_gateway(db_path: str, port: int, relay_secret: str, admin_token: str, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(
        {
            "GATEWAY_DB_PATH": db_path,
            "GATEWAY_SECRET_KEY": "smoke-gateway-secret-key-32-characters",
            "GATEWAY_ADMIN_TOKEN": admin_token,
            "GATEWAY_CHANNELS": "tiktok",
            "GATEWAY_ENVIRONMENTS": "production",
            "GATEWAY_RATE_LIMIT_PER_MINUTE": "100000",
            "GATEWAY_WEBHOOK_RELAY_SECRET": relay_secret,
            "GATEWAY_WEBHOOK_DISPATCH_ENABLED": "true",
            "GATEWAY_WEBHOOK_DISPATCH_INTERVAL_SECONDS": "1",
            "GATEWAY_WEBHOOK_MAX_ATTEMPTS": "10",
            "GATEWAY_SQLITE_SYNCHRONOUS": "NORMAL",
            "GATEWAY_PURGE_EXPIRED_ON_HEALTH": "true",
        }
    )
    gateway_dir = REPO_ROOT / "gateway"
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
        cwd=str(gateway_dir),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    process._smoke_log_file = log_file  # type: ignore[attr-defined]
    return process


def stop_gateway(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    log_file = getattr(process, "_smoke_log_file", None)
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
    with httpx.Client(base_url=gateway_url, timeout=10) as client:
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
                "tenant_id": 999001,
                "name": "loadtest-smoke",
                "channels": ["tiktok"],
                "environments": ["production"],
                "store_ids": [int(item) for item in shop_ids],
                "callback_url": sink_callback_url,
                "notes": "isolated webhook smoke; safe to delete",
            },
        )
        response.raise_for_status()
        instance_payload = response.json()
        instance = instance_payload["instance"]
        token = instance_payload["instance_token"]
        instance_headers = {"Authorization": f"Bearer {token}"}
        for index, shop_id in enumerate(shop_ids, start=1):
            response = client.post(
                "/api/v1/gateway/instance/webhook-shops",
                headers=instance_headers,
                json={"store_id": int(shop_id), "platform_shop_id": shop_id},
            )
            response.raise_for_status()
    return {"instance": instance, "instance_token": token}


def main() -> int:
    parser = argparse.ArgumentParser(description="Webhook 压测本地 smoke")
    parser.add_argument("--rate", type=float, default=5.0)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--shops", type=int, default=5)
    parser.add_argument("--hot-shops", type=int, default=1)
    parser.add_argument("--hot-share", type=float, default=0.4)
    parser.add_argument("--outage-start", type=float, default=3.0)
    parser.add_argument("--outage-duration", type=float, default=3.0)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--output-dir", default="scripts/reports")
    args = parser.parse_args()

    run_id = f"lt-smoke-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    output_dir = (REPO_ROOT / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="webhook-smoke-"))
    gateway_db = work_dir / "gateway.db"
    gateway_log = work_dir / "gateway.log"
    admin_token = "smoke-admin-token"
    relay_secret = "smoke-relay-secret-32-characters"
    app_key = "smoke-app-key"
    app_secret = "smoke-app-secret"
    shop_id_base = 990_000_000
    shop_ids = [str(shop_id_base + index) for index in range(args.shops)]
    sink = SinkServer(relay_secret)
    gateway_process: subprocess.Popen | None = None
    sink_port = sink.port
    gateway_port = find_free_port()
    gateway_url = f"http://127.0.0.1:{gateway_port}"
    sink_callback_url = f"http://127.0.0.1:{sink_port}/tenant/callback"
    outage_thread: threading.Thread | None = None

    try:
        sink.start()
        gateway_process = start_gateway(
            str(gateway_db), gateway_port, relay_secret, admin_token, gateway_log
        )
        wait_http_ok(f"{gateway_url}/healthz")
        seed_gateway(
            gateway_url,
            admin_token=admin_token,
            app_key=app_key,
            app_secret=app_secret,
            sink_callback_url=sink_callback_url,
            shop_ids=shop_ids,
        )

        def outage() -> None:
            time.sleep(max(0.0, args.outage_start))
            sink.state.set_down(True)
            time.sleep(max(0.1, args.outage_duration))
            sink.state.set_down(False)

        if args.outage_duration > 0:
            outage_thread = threading.Thread(target=outage, daemon=True)
            outage_thread.start()

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
            "12",
            "--hot-shops",
            str(args.hot_shops),
            "--hot-share",
            str(args.hot_share),
            "--duplicate-rate",
            "0.10",
            "--out-of-order-rate",
            "0.10",
            "--partial-rate",
            "0.15",
            "--run-id",
            run_id,
            "--mode",
            "isolated",
            "--gateway-db",
            str(gateway_db),
            "--observer-stats-url",
            f"http://127.0.0.1:{sink_port}/stats",
            "--drain-grace",
            "0.5",
            "--drain-timeout",
            "120",
            "--output-dir",
            str(output_dir),
        ]
        print("[smoke] 启动真实 Gateway + 本地 sink，开始小流量...")
        result = subprocess.run(command, cwd=str(REPO_ROOT), text=True, capture_output=True)
        if result.returncode != 0:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            raise RuntimeError(f"loadtest exit={result.returncode}")
        print(result.stdout.strip())
        report_path = output_dir / f"webhook-loadtest-{run_id}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        sink_stats = sink.state.stats(run_id)
        smoke = {
            "run_id": run_id,
            "generated_at": utc_now(),
            "gateway_url": gateway_url,
            "gateway_db": str(gateway_db),
            "shops": shop_ids,
            "outage": {
                "start_seconds": args.outage_start,
                "duration_seconds": args.outage_duration,
            },
            "loadtest_report": report,
            "sink": sink_stats,
            "safety": {
                "isolated_temp_db": str(gateway_db),
                "production_events": 0,
                "note": "真实 Gateway 验签/持久化/relay；sink 仅本地临时，不写业务库",
            },
            "gateway_log_tail": gateway_log.read_text(encoding="utf-8", errors="replace")[-4000:],
        }
        smoke_path = output_dir / f"webhook-smoke-{run_id}.json"
        smoke_path.write_text(json.dumps(smoke, ensure_ascii=False, indent=2), encoding="utf-8")
        ack = report["gateway_ack"]
        delivery = report["delivery"]
        persistence = report["persistence"]
        print("[smoke] ===== 结果 =====")
        print(
            f"ACK success={ack['success_count']}/{report['window']['primary_sent']} "
            f"p99={ack['latency']['p99_ms']}ms"
        )
        print(
            f"persisted={persistence['rows']} duplicate_count={persistence['duplicate_count']} "
            f"status={persistence['status_counts']}"
        )
        print(
            f"e2e(observer)={report.get('observer_metrics', {}).get('e2e')} "
            f"backlog={delivery['backlog']}"
        )
        print(
            f"sink unique={sink_stats['summary']['unique_events']} "
            f"duplicate_deliveries={sink_stats['summary']['duplicate_delivery_count']} "
            f"partial={sink_stats['summary']['partial_events']} "
            f"signature_failures={sink_stats['summary']['signature_failures']}"
        )
        print(f"[smoke] report={smoke_path}")
        if not args.keep:
            shutil.rmtree(work_dir, ignore_errors=True)
        return 0
    finally:
        if gateway_process is not None:
            stop_gateway(gateway_process)
        sink.stop()
        if args.keep:
            print(f"[smoke] kept={work_dir}")
        elif work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
