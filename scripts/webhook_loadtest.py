#!/usr/bin/env python3
"""TikTok Webhook 百店压测与故障演练工具。

只做客户端与只读观测，不修改 Gateway/channels/sales 核心投递代码。

安全边界：
* 默认 ``--mode isolated``，只用于本地/隔离环境。
* ``--mode production-unroutable`` 仅允许向生产 Gateway 发送未登记店铺的
  ``unroutable`` 事件；必须显式确认、必须提供 Gateway SQLite 只读路径，
  并在发送前确认店铺未在 gateway_webhook_shops 中绑定、发送后确认全部
  测试事件状态为 ``unroutable``，否则脚本失败。
* 故意发送路由到真实店铺的事件会把模拟订单送到租户业务；生产环境严禁使用。

用法示例见 ``docs/webhook-loadtest-batch5.md``。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import math
import os
import random
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

try:
    import httpx
except ImportError as exc:  # pragma: no cover - runtime prerequisite
    raise SystemExit("缺少 httpx：请使用 backend 依赖环境或 pip install httpx") from exc


WEBHOOK_PATH = "/api/v1/gateway/webhooks/tiktok"
ORDER_EVENT_TYPES = (1, 11, 12)  # ORDER_STATUS_CHANGE / CANCELLATION / RETURN
ORDER_STATUSES = ("AWAITING_SHIPMENT", "SHIPPED", "DELIVERED", "CANCEL", "COMPLETED")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_from_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def percentile(values: Iterable[float], p: float) -> float | None:
    ordered = sorted(float(item) for item in values if item is not None)
    if not ordered:
        return None
    index = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return round(ordered[index], 3)


def latency_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    ordered = [float(item) for item in values if item is not None]
    if not ordered:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    return {
        "count": len(ordered),
        "p50_ms": percentile(ordered, 0.50),
        "p95_ms": percentile(ordered, 0.95),
        "p99_ms": percentile(ordered, 0.99),
        "max_ms": round(max(ordered), 3),
    }


def tiktok_signature(*, app_key: str, app_secret: str, raw_body: bytes) -> str:
    """与 Gateway 一致：HMAC-SHA256(app_secret, app_key + raw_body) 十六进制小写。"""
    message = app_key.encode("utf-8") + raw_body
    return hmac.new(app_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


@dataclass
class LoadConfig:
    gateway_url: str
    app_key: str
    app_secret: str
    shop_ids: list[str]
    rate: float = 20.0
    duration_seconds: float = 60.0
    events: int | None = None
    concurrency: int = 32
    timeout_seconds: float = 10.0
    hot_shops: int = 0
    hot_share: float = 0.0
    duplicate_rate: float = 0.0
    duplicate_delay_min: float = 0.05
    duplicate_delay_max: float = 0.5
    out_of_order_rate: float = 0.0
    partial_rate: float = 0.0
    retry_429: int = 3
    max_retry_wait_seconds: float = 10.0
    mode: str = "isolated"
    run_id: str = ""
    events_per_order: int = 4
    auth_scheme: str = "raw"


@dataclass
class EventSpec:
    seq: int
    notification_id: str
    shop_id: str
    order_id: str
    raw_body: bytes
    signature: str
    event_type: int
    partial: bool
    out_of_order: bool


@dataclass
class SendResult:
    seq: int
    notification_id: str
    shop_id: str
    order_id: str
    duplicate: bool
    ok: bool
    status_code: int
    latency_ms: float
    attempts: int
    sent_at: float
    error: str = ""


@dataclass
class RunStats:
    primary: list[SendResult] = field(default_factory=list)
    duplicates: list[SendResult] = field(default_factory=list)
    fault_events: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = 0.0
    ended_at: float = 0.0
    last_send_at: float = 0.0
    scheduled: int = 0
    policy_hot_events: int = 0
    policy_out_of_order_events: int = 0
    policy_partial_events: int = 0
    policy_duplicate_events: int = 0
    retry_429_count: int = 0
    retry_error_count: int = 0


def make_run_id() -> str:
    return f"lt-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()}"


def parse_shops(args: argparse.Namespace) -> list[str]:
    if args.shop_ids:
        shops = [item.strip() for item in args.shop_ids.split(",") if item.strip()]
    else:
        base = int(args.shop_id_base)
        shops = [str(base + index) for index in range(int(args.shops))]
    if not shops:
        raise SystemExit("店铺列表为空")
    if len(set(shops)) != len(shops):
        raise SystemExit("店铺 ID 重复")
    return shops


def choose_shop(config: LoadConfig) -> str:
    if config.hot_shops > 0 and random.random() < config.hot_share:
        return random.choice(config.shop_ids[: config.hot_shops])
    return random.choice(config.shop_ids)


def build_event(
    config: LoadConfig,
    seq: int,
    shop_id: str,
    *,
    status_index: int = 0,
    out_of_order: bool | None = None,
) -> EventSpec:
    notification_id = f"{config.run_id}-{seq:07d}"
    order_index = seq // max(1, int(config.events_per_order))
    order_id = f"{config.run_id}-O-{order_index:07d}"
    event_type = random.choice(ORDER_EVENT_TYPES)
    status = ORDER_STATUSES[status_index % len(ORDER_STATUSES)]
    partial = random.random() < config.partial_rate
    if out_of_order is None:
        out_of_order = random.random() < config.out_of_order_rate
    update_time = int(time.time())
    line_items = [
        {
            "order_id": order_id,
            "sku_id": f"SKU-LT-{shop_id[-3:]}-{seq % 11 + 1:02d}",
            "quantity": 1,
            "sale_price": "9.90",
        }
    ]
    data: dict[str, Any] = {
        "order_id": order_id,
        "order_status": status,
        "update_time": update_time,
        "_loadtest": {
            "run_id": config.run_id,
            "seq": seq,
            "out_of_order": out_of_order,
            "partial": partial,
        },
        "line_items": line_items,
        "orders": [
            {
                "order_id": order_id,
                "order_status": status,
                "line_items": line_items if not partial else line_items[:1],
            }
        ],
    }
    if partial:
        # 只保留部分订单/明细，供测试下游“部分订单返回”兼容处理。
        data["orders"] = data["orders"][:1]
        data["line_items"] = line_items[:1]
    payload = {
        "tts_notification_id": notification_id,
        "notification_id": notification_id,
        "type": event_type,
        "shop_id": shop_id,
        "timestamp": update_time,
        "data": data,
    }
    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = tiktok_signature(
        app_key=config.app_key, app_secret=config.app_secret, raw_body=raw_body
    )
    return EventSpec(
        seq=seq,
        notification_id=notification_id,
        shop_id=shop_id,
        order_id=order_id,
        raw_body=raw_body,
        signature=signature,
        event_type=event_type,
        partial=partial,
        out_of_order=out_of_order,
    )


def auth_headers(config: LoadConfig, event: EventSpec) -> dict[str, str]:
    signature = event.signature
    if config.auth_scheme == "bearer":
        signature = f"Bearer {signature}"
    return {
        "Authorization": signature,
        "Content-Type": "application/json",
        "User-Agent": "ventasync-webhook-loadtest/1.0",
        "X-Loadtest-Run": config.run_id,
    }


async def send_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    config: LoadConfig,
    event: EventSpec,
    *,
    duplicate: bool,
    stats: RunStats,
) -> None:
    async with semaphore:
        start = time.perf_counter()
        sent_at = time.time()
        status_code = 0
        attempts = 0
        error = ""
        for attempt in range(1, config.retry_429 + 2):
            attempts = attempt
            try:
                response = await client.post(
                    f"{config.gateway_url.rstrip('/')}{WEBHOOK_PATH}",
                    content=event.raw_body,
                    headers=auth_headers(config, event),
                    timeout=config.timeout_seconds,
                )
                status_code = int(response.status_code)
                if status_code == 429 and attempt <= config.retry_429:
                    stats.retry_429_count += 1
                    retry_after = response.headers.get("retry-after", "1")
                    try:
                        wait_seconds = float(retry_after)
                    except ValueError:
                        wait_seconds = 1.0
                    await asyncio.sleep(min(config.max_retry_wait_seconds, max(0.0, wait_seconds)))
                    continue
                if status_code >= 500 and attempt == 1:
                    stats.retry_error_count += 1
                    await asyncio.sleep(0.2)
                    continue
                break
            except Exception as exc:  # timeout/connection reset
                error = str(exc)[:300]
                status_code = 0
                if attempt == 1:
                    stats.retry_error_count += 1
                    await asyncio.sleep(0.2)
                    continue
                break
        latency_ms = (time.perf_counter() - start) * 1000.0
        result = SendResult(
            seq=event.seq,
            notification_id=event.notification_id,
            shop_id=event.shop_id,
            order_id=event.order_id,
            duplicate=duplicate,
            ok=200 <= status_code < 300,
            status_code=status_code,
            latency_ms=round(latency_ms, 3),
            attempts=attempts,
            sent_at=sent_at,
            error=error,
        )
        (stats.duplicates if duplicate else stats.primary).append(result)
        stats.last_send_at = max(stats.last_send_at, sent_at)


async def run_fault_plan(
    plan_path: str | None,
    *,
    allow_shell_faults: bool,
    start_mono: float,
    stats: RunStats,
) -> None:
    if not plan_path:
        return
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    if not isinstance(plan, list):
        raise ValueError("fault plan 必须是 JSON 数组")
    if plan and not allow_shell_faults:
        raise ValueError("fault plan 含 shell 命令；必须显式增加 --allow-shell-faults")
    for index, item in enumerate(sorted(plan, key=lambda x: float(x.get("at_seconds", 0)))):
        if not isinstance(item, dict) or not item.get("command"):
            continue
        due = start_mono + max(0.0, float(item.get("at_seconds", 0)))
        await asyncio.sleep(max(0.0, due - asyncio.get_running_loop().time()))
        command = str(item["command"])
        label = str(item.get("label") or f"fault-{index + 1}")
        started = time.time()
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        stats.fault_events.append(
            {
                "label": label,
                "command": command,
                "at_seconds": round(started - stats.started_at, 3),
                "exit_code": int(proc.returncode or 0),
                "duration_seconds": round(time.time() - started, 3),
                "stdout": stdout.decode("utf-8", errors="replace")[-1000:],
                "stderr": stderr.decode("utf-8", errors="replace")[-1000:],
            }
        )


def validate_fault_plan(path: str, *, allow_shell_faults: bool) -> list[dict[str, Any]]:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(plan, list):
        raise SystemExit("fault plan 必须是 JSON 数组")
    for item in plan:
        if not isinstance(item, dict) or not item.get("command"):
            raise SystemExit("fault plan 每项必须包含 command")
        float(item.get("at_seconds", 0))
    if plan and not allow_shell_faults:
        raise SystemExit("fault plan 含 shell 命令；必须显式增加 --allow-shell-faults")
    return plan


async def run_load(args: argparse.Namespace, config: LoadConfig) -> RunStats:
    stats = RunStats(started_at=time.time())
    limit = int(config.events) if config.events is not None else max(
        1, int(round(config.duration_seconds * config.rate))
    )
    stats.scheduled = limit
    semaphore = asyncio.Semaphore(max(1, int(config.concurrency)))
    start_mono = asyncio.get_running_loop().time()
    tasks: list[asyncio.Task[Any]] = []
    fault_task = asyncio.create_task(
        run_fault_plan(
            args.fault_plan,
            allow_shell_faults=bool(args.allow_shell_faults),
            start_mono=start_mono,
            stats=stats,
        )
    )
    interval = 1.0 / config.rate if config.rate > 0 else 0.0
    last_progress = time.monotonic()
    order_status_index: dict[str, int] = {}
    async with httpx.AsyncClient(
        limits=httpx.Limits(
            max_connections=max(4, config.concurrency * 2),
            max_keepalive_connections=max(4, config.concurrency),
        ),
        follow_redirects=False,
    ) as client:
        for seq in range(limit):
            due = start_mono + seq * interval
            await asyncio.sleep(max(0.0, due - asyncio.get_running_loop().time()))
            shop = choose_shop(config)
            order_index = seq // max(1, int(config.events_per_order))
            order_id = f"{config.run_id}-O-{order_index:07d}"
            previous_status = order_status_index.get(order_id, -1)
            out_of_order = (
                previous_status > 0 and random.random() < config.out_of_order_rate
            )
            if out_of_order:
                status_index = random.randint(0, previous_status - 1)
            elif previous_status < 0:
                status_index = 0
            else:
                status_index = min(previous_status + 1, len(ORDER_STATUSES) - 1)
            order_status_index[order_id] = max(previous_status, status_index)
            event = build_event(
                config,
                seq,
                shop,
                status_index=status_index,
                out_of_order=out_of_order,
            )
            if config.hot_shops > 0 and shop in config.shop_ids[: config.hot_shops]:
                stats.policy_hot_events += 1
            if event.partial:
                stats.policy_partial_events += 1
            if event.out_of_order:
                stats.policy_out_of_order_events += 1
            tasks.append(
                asyncio.create_task(
                    send_one(client, semaphore, config, event, duplicate=False, stats=stats)
                )
            )
            if config.duplicate_rate > 0 and random.random() < config.duplicate_rate:
                stats.policy_duplicate_events += 1

                async def duplicate_later(spec: EventSpec = event) -> None:
                    delay = random.uniform(
                        config.duplicate_delay_min, max(config.duplicate_delay_min, config.duplicate_delay_max)
                    )
                    await asyncio.sleep(delay)
                    await send_one(client, semaphore, config, spec, duplicate=True, stats=stats)

                tasks.append(asyncio.create_task(duplicate_later()))
            if (seq + 1) % 100 == 0:
                await asyncio.sleep(0)  # let network tasks progress without blocking scheduler
            if time.monotonic() - last_progress >= 10:
                accepted = sum(1 for item in stats.primary if item.ok)
                print(
                    f"[loadtest] progress scheduled={seq + 1}/{limit} "
                    f"completed={len(stats.primary)} accepted={accepted} "
                    f"duplicates={len(stats.duplicates)}"
                )
                last_progress = time.monotonic()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    stats.ended_at = time.time()
    try:
        await asyncio.wait_for(fault_task, timeout=1.0)
    except Exception:  # noqa: BLE001 - 故障计划失败不应吞掉本次压测报告
        fault_task.cancel()
    return stats


def open_gateway_db_readonly(path: str) -> sqlite3.Connection:
    if not Path(path).exists():
        raise SystemExit(f"Gateway SQLite 不存在：{path}")
    connection = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def gateway_columns(connection: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in connection.execute("PRAGMA table_info(gateway_webhook_events)")}


def query_gateway_events(path: str, run_id: str) -> list[dict[str, Any]]:
    connection = open_gateway_db_readonly(path)
    try:
        columns = gateway_columns(connection)
        wanted = [
            "id",
            "notification_id",
            "platform_shop_id",
            "instance_id",
            "tenant_id",
            "store_id",
            "status",
            "attempts",
            "duplicate_count",
            "created_at",
            "updated_at",
            "delivered_at",
            "last_attempt_at",
            "latency_ms",
            "last_status_code",
            "failure_class",
            "replay_count",
            "lease_token",
            "lease_expires_at",
        ]
        selected = [column for column in wanted if column in columns]
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM gateway_webhook_events "
            "WHERE notification_id LIKE ? ORDER BY id ASC",
            (f"{run_id}-%",),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def check_registered_shops(path: str, shop_ids: list[str]) -> list[dict[str, Any]]:
    connection = open_gateway_db_readonly(path)
    try:
        rows = connection.execute(
            "SELECT platform_shop_id, instance_id, tenant_id, store_id, active "
            "FROM gateway_webhook_shops WHERE channel='tiktok' AND active=1 "
            "AND platform_shop_id IN (%s)"
            % ",".join("?" for _ in shop_ids),
            tuple(shop_ids),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.DatabaseError:
        return []
    finally:
        connection.close()


def persisted_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    duplicate_count = 0
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        duplicate_count += int(row.get("duplicate_count") or 0)
    delivered = [
        row
        for row in rows
        if row.get("delivered_at") and epoch_from_iso(str(row.get("delivered_at")))
    ]
    return {
        "rows": len(rows),
        "status_counts": status_counts,
        "duplicate_count": duplicate_count,
        "delivered": len(delivered),
    }


def delivery_latencies(rows: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in rows:
        created = epoch_from_iso(str(row.get("created_at") or ""))
        delivered = epoch_from_iso(str(row.get("delivered_at") or ""))
        if created is not None and delivered is not None and delivered >= created:
            values.append((delivered - created) * 1000.0)
    return values


async def wait_for_drain(
    db_path: str,
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    started = time.time()
    last_rows: list[dict[str, Any]] = []
    while time.time() - started <= timeout_seconds:
        last_rows = query_gateway_events(db_path, run_id)
        pending = [row for row in last_rows if str(row.get("status")) == "pending"]
        if not pending:
            return {
                "timed_out": False,
                "catchup_seconds": round(time.time() - started, 3),
                "rows": len(last_rows),
                "pending": 0,
            }
        await asyncio.sleep(poll_interval)
    pending = [row for row in last_rows if str(row.get("status")) == "pending"]
    return {
        "timed_out": True,
        "catchup_seconds": round(time.time() - started, 3),
        "rows": len(last_rows),
        "pending": len(pending),
    }


def parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    raw = dsn.strip()
    if "://" not in raw:
        raise ValueError("MySQL DSN 需形如 mysql://user:pass@host:3306/db")
    parsed = urlsplit(raw)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": parsed.path.lstrip("/"),
    }


def query_tenant_metrics(
    dsn: str, run_id: str, send_times: dict[str, float]
) -> dict[str, Any]:
    """可选：读取测试租户库的订单写入时间与重复库存流水数（只读）。"""
    try:
        import pymysql
    except ImportError:
        return {"available": False, "error": "pymysql 未安装"}
    params = parse_mysql_dsn(dsn)
    connection = pymysql.connect(
        host=params["host"],
        port=params["port"],
        user=params["user"],
        password=params["password"],
        database=params["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
        read_timeout=10,
    )
    try:
        prefix = f"{run_id}-O-%"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT external_order_no, created_at FROM orders WHERE external_order_no LIKE %s",
                (prefix,),
            )
            orders = cursor.fetchall()
            e2e_values: list[float] = []
            for order in orders:
                sent_at = send_times.get(str(order.get("external_order_no") or ""))
                created_at = order.get("created_at")
                if sent_at is not None and created_at is not None:
                    if isinstance(created_at, datetime):
                        created_epoch = created_at.replace(tzinfo=timezone.utc).timestamp()
                    else:
                        parsed = epoch_from_iso(str(created_at))
                        created_epoch = parsed
                    if created_epoch is not None and created_epoch >= sent_at:
                        e2e_values.append((created_epoch - sent_at) * 1000.0)
            duplicate_movements: list[dict[str, Any]] = []
            try:
                cursor.execute(
                    """
                    SELECT o.external_order_no, sm.movement_type, COUNT(*) AS movement_count
                    FROM orders o
                    JOIN stock_movements sm
                      ON sm.reference_type = 'order' AND sm.reference_id = o.id
                    WHERE o.external_order_no LIKE %s
                      AND sm.movement_type = 'order_deduct'
                    GROUP BY o.id, o.external_order_no, sm.movement_type
                    HAVING COUNT(*) > 1
                    """,
                    (prefix,),
                )
                duplicate_movements = list(cursor.fetchall())
            except Exception as exc:  # noqa: BLE001 - optional metrics must not crash run
                duplicate_movements = []
                movement_error = str(exc)[:200]
            else:
                movement_error = ""
        return {
            "available": True,
            "orders_seen": len(orders),
            "e2e": latency_summary(e2e_values),
            "duplicate_inventory_movements": len(duplicate_movements),
            "duplicate_inventory_rows": duplicate_movements[:20],
            "duplicate_inventory_error": movement_error,
        }
    finally:
        connection.close()


async def fetch_observer(url: str, run_id: str, send_times: dict[str, float]) -> dict[str, Any]:
    """可选：读取测试租户/模拟 sink 的观测端点（只读 GET）。"""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(url, params={"prefix": run_id})
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)[:300]}
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {"available": False, "error": "observer 返回必须有 items 数组"}
    e2e_values: list[float] = []
    duplicate_deliveries = 0
    duplicate_inventory = 0
    processed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        sent_at = send_times.get(str(item.get("notification_id") or ""))
        processed_at = epoch_from_iso(str(item.get("processed_at") or ""))
        if sent_at is not None and processed_at is not None and processed_at >= sent_at:
            e2e_values.append((processed_at - sent_at) * 1000.0)
        duplicate_deliveries += int(item.get("duplicate_delivery_count") or 0)
        duplicate_inventory += int(item.get("duplicate_inventory_movements") or 0)
        processed += 1
    return {
        "available": True,
        "items": processed,
        "e2e": latency_summary(e2e_values),
        "duplicate_deliveries": duplicate_deliveries,
        "duplicate_inventory_movements": duplicate_inventory,
    }


def build_report(
    config: LoadConfig,
    stats: RunStats,
    args: argparse.Namespace,
    *,
    gateway_rows: list[dict[str, Any]],
    drain: dict[str, Any],
    tenant_metrics: dict[str, Any],
    observer_metrics: dict[str, Any],
) -> dict[str, Any]:
    primary = stats.primary
    duplicates = stats.duplicates
    accepted = [result for result in primary if result.ok]
    ack_latencies = [result.latency_ms for result in accepted]
    all_status_counts: dict[str, int] = {}
    for result in primary:
        all_status_counts[str(result.status_code)] = (
            all_status_counts.get(str(result.status_code), 0) + 1
        )
    persisted = persisted_status(gateway_rows)
    rows_by_notification = {
        str(row.get("notification_id")): row for row in gateway_rows
    }
    missing_persisted = [
        result.notification_id
        for result in accepted
        if str(result.notification_id) not in rows_by_notification
    ]
    delivery_ms = delivery_latencies(gateway_rows)
    post_persist_delay = []
    for result in accepted:
        row = rows_by_notification.get(result.notification_id)
        created = epoch_from_iso(str((row or {}).get("created_at") or ""))
        if created is not None and created >= result.sent_at:
            post_persist_delay.append((created - result.sent_at) * 1000.0)
    if stats.ended_at and stats.last_send_at:
        send_span = max(0.0, stats.last_send_at - stats.started_at)
    else:
        send_span = 0.0
    actual_rate = len(primary) / send_span if send_span > 0 else 0.0
    duplicate_success = sum(1 for result in duplicates if result.ok)
    safety = {
        "mode": config.mode,
        "shop_ids": config.shop_ids,
        "gateway_url": config.gateway_url,
        "notification_prefix": f"{config.run_id}-",
        "unroutable_only": config.mode == "production-unroutable",
        "production_unroutable_verified": (
            config.mode != "production-unroutable"
            or (
                bool(gateway_rows)
                and all(str(row.get("status")) == "unroutable" for row in gateway_rows)
            )
        ),
    }
    report = {
        "meta": {
            "run_id": config.run_id,
            "generated_at": utc_now(),
            "tool": "scripts/webhook_loadtest.py",
            "event_path": WEBHOOK_PATH,
            "signature": "HMAC-SHA256(app_secret, app_key+raw_body)",
        },
        "config": {
            **asdict(config),
            "app_secret": "***",
            "gateway_db": args.gateway_db or "",
            "fault_plan": args.fault_plan or "",
            "observer_url": args.observer_stats_url or "",
            "tenant_db_dsn": "***" if args.tenant_db_dsn else "",
        },
        "window": {
            "started_at": iso_from_epoch(stats.started_at) if stats.started_at else "",
            "ended_at": iso_from_epoch(stats.ended_at) if stats.ended_at else "",
            "scheduled": stats.scheduled,
            "primary_sent": len(primary),
            "duplicates_sent": len(duplicates),
            "actual_rate_events_per_second": round(actual_rate, 3),
            "hot_shop_events": stats.policy_hot_events,
            "out_of_order_events": stats.policy_out_of_order_events,
            "partial_order_events": stats.policy_partial_events,
            "duplicate_plan_events": stats.policy_duplicate_events,
        },
        "gateway_ack": {
            "success_count": len(accepted),
            "failure_count": len(primary) - len(accepted),
            "success_rate": round(len(accepted) / len(primary), 6) if primary else None,
            "status_code_counts": all_status_counts,
            "latency": latency_summary(ack_latencies),
            "retry_429_count": stats.retry_429_count,
            "retry_error_count": stats.retry_error_count,
        },
        "persistence": {
            **persisted,
            "accepted_without_row": len(missing_persisted),
            "accepted_without_row_ids": missing_persisted[:50],
            "persist_delay": latency_summary(post_persist_delay),
        },
        "delivery": {
            "delivery_latency": latency_summary(delivery_ms),
            "pending": persisted["status_counts"].get("pending", 0),
            "dead": persisted["status_counts"].get("dead", 0),
            "unroutable": persisted["status_counts"].get("unroutable", 0),
            "backlog": drain,
        },
        "duplicates": {
            "duplicate_http_success": duplicate_success,
            "gateway_duplicate_count": persisted["duplicate_count"],
            "observer_duplicate_deliveries": observer_metrics.get("duplicate_deliveries"),
        },
        "tenant_metrics": tenant_metrics,
        "observer_metrics": observer_metrics,
        "faults": stats.fault_events,
        "safety": safety,
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    ack = report["gateway_ack"]
    latency = ack["latency"]
    persistence = report["persistence"]
    delivery = report["delivery"]
    duplicates = report["duplicates"]
    window = report["window"]
    tenant = report.get("tenant_metrics") or {}
    observer = report.get("observer_metrics") or {}
    e2e = (tenant.get("e2e") if tenant.get("available") else observer.get("e2e")) or {}
    lines = [
        f"# Webhook 压测报告 {report['meta']['run_id']}",
        "",
        f"- 生成时间：{report['meta']['generated_at']}",
        f"- 模式：`{report['config']['mode']}`；目标：`{report['config']['gateway_url']}`",
        f"- 时间窗：{window['started_at']} → {window['ended_at']}",
        f"- 计划事件：{window['scheduled']}；主事件：{window['primary_sent']}；重复：{window['duplicates_sent']}；实际速率：{window['actual_rate_events_per_second']} events/s",
        "",
        "## 核心指标",
        "",
        "| 指标 | 结果 |",
        "| --- | --- |",
        f"| Gateway ACK 成功率 | {ack['success_rate']} |",
        f"| Gateway ACK P99 | {latency['p99_ms']} ms |",
        f"| 持久化行 / ACK 已接受 | {persistence['rows']} / {ack['success_count']} |",
        f"| ACK 但无持久化行 | {persistence['accepted_without_row']} |",
        f"| 端到端 P50 / P95 | {e2e.get('p50_ms')} / {e2e.get('p95_ms')} ms |",
        f"| 积压追平 | {delivery['backlog'].get('catchup_seconds')} s（timed_out={delivery['backlog'].get('timed_out')}） |",
        f"| Gateway duplicate_count | {duplicates['gateway_duplicate_count']} |",
        f"| 重复库存变动（测试库/观察器） | {(tenant.get('duplicate_inventory_movements') if tenant.get('available') else observer.get('duplicate_inventory_movements'))} |",
        f"| 状态分布 | {persistence['status_counts']} |",
        "",
        "## 写入/重复/乱序策略",
        "",
        f"- 热店事件：{window['hot_shop_events']}；乱序标记事件：{window['out_of_order_events']}；部分订单事件：{window['partial_order_events']}；重复发送计划：{window['duplicate_plan_events']}",
        f"- 429 重试：{ack['retry_429_count']}；网络/5xx 重试：{ack['retry_error_count']}",
        "",
        "## 环境/安全",
        "",
        f"- 安全模式：`{report['safety']['mode']}`",
        f"- 生产 unroutable 已验证：{report['safety']['production_unroutable_verified']}",
        f"- 店铺 ID：{', '.join(report['safety']['shop_ids'][:20])}{' ...' if len(report['safety']['shop_ids']) > 20 else ''}",
    ]
    if tenant:
        lines.extend(["", "## 租户库观测", "", f"```json\n{json.dumps(tenant, ensure_ascii=False, indent=2)}\n```"])
    if observer:
        lines.extend(["", "## Observer 观测", "", f"```json\n{json.dumps(observer, ensure_ascii=False, indent=2)}\n```"])
    if report["faults"]:
        lines.extend(["", "## 故障注入", "", f"```json\n{json.dumps(report['faults'], ensure_ascii=False, indent=2)}\n```"])
    lines.extend(
        [
            "",
            "## 判定",
            "",
            "- 本报告只汇总本次压测证据；通过标准见 `docs/webhook-loadtest-batch5.md`。",
            "- `end_to_end` 默认来自 observer/租户测试库；若两者不可用则只显示 Gateway→租户 relay 的 delivery 指标，不能宣称订单写入完成。",
        ]
    )
    return "\n".join(lines) + "\n"


async def execute_run(args: argparse.Namespace) -> int:
    if args.mode == "production-unroutable":
        if not args.allow_production_unroutable:
            raise SystemExit("production-unroutable 需要 --allow-production-unroutable")
        if not args.gateway_db:
            raise SystemExit("production-unroutable 必须提供 --gateway-db 以证明 unroutable")
    if args.fault_plan and not args.allow_shell_faults:
        raise SystemExit("提供 fault plan 时必须同时显式增加 --allow-shell-faults")
    if args.mode == "production-unroutable" and args.fault_plan:
        raise SystemExit("production-unroutable 禁止注入 shell 故障")
    if args.fault_plan:
        validate_fault_plan(args.fault_plan, allow_shell_faults=bool(args.allow_shell_faults))
    shops = parse_shops(args)
    if args.mode == "production-unroutable":
        if min(int(item) for item in shops) < 900_000_000:
            raise SystemExit("production-unroutable 的 shop_id 必须使用 9 亿以上的保留测试号段")
        if not args.gateway_db:
            raise SystemExit("production-unroutable 需要 --gateway-db")
        registered = check_registered_shops(args.gateway_db, shops)
        if registered:
            raise SystemExit(
                "拒绝发送：下列测试店铺已在 gateway_webhook_shops 绑定到实例，"
                f"会污染业务：{registered}"
            )
    run_id = args.run_id or make_run_id()
    if not run_id.startswith("lt-"):
        run_id = f"lt-{run_id}"
    config = LoadConfig(
        gateway_url=args.gateway_url,
        app_key=args.app_key,
        app_secret=args.app_secret,
        shop_ids=shops,
        rate=float(args.rate),
        duration_seconds=float(args.duration),
        events=args.events,
        concurrency=int(args.concurrency),
        timeout_seconds=float(args.timeout),
        hot_shops=int(args.hot_shops),
        hot_share=float(args.hot_share),
        duplicate_rate=float(args.duplicate_rate),
        duplicate_delay_min=float(args.duplicate_delay_min),
        duplicate_delay_max=float(args.duplicate_delay_max),
        out_of_order_rate=float(args.out_of_order_rate),
        partial_rate=float(args.partial_rate),
        retry_429=int(args.retry_429),
        max_retry_wait_seconds=float(args.max_retry_wait),
        mode=args.mode,
        run_id=run_id,
        events_per_order=int(args.events_per_order),
        auth_scheme=args.auth_scheme,
    )
    print(
        f"[loadtest] run={run_id} target={config.gateway_url} shops={len(shops)} "
        f"rate={config.rate}/s duration={config.duration_seconds}s"
    )
    stats = await run_load(args, config)
    print(
        f"[loadtest] sent primary={len(stats.primary)} duplicate={len(stats.duplicates)} "
        f"ack_ok={sum(1 for item in stats.primary if item.ok)}"
    )
    gateway_rows: list[dict[str, Any]] = []
    if args.gateway_db:
        await asyncio.sleep(max(0.0, float(args.drain_grace)))
        if stats.last_send_at:
            await asyncio.sleep(min(2.0, max(0.0, time.time() - stats.last_send_at)))
        drain = await wait_for_drain(
            args.gateway_db,
            run_id,
            timeout_seconds=float(args.drain_timeout),
        )
        gateway_rows = query_gateway_events(args.gateway_db, run_id)
        if args.mode == "production-unroutable":
            bad = [row for row in gateway_rows if str(row.get("status")) != "unroutable"]
            if bad or len(gateway_rows) < len([item for item in stats.primary if item.ok]):
                raise SystemExit(
                    "安全检查失败：生产事件未全部保持 unroutable，可能有业务路由/丢失；"
                    f"rows={len(gateway_rows)} bad={bad[:3]}"
                )
    else:
        drain = {
            "timed_out": False,
            "catchup_seconds": None,
            "rows": 0,
            "pending": 0,
            "note": "未提供 --gateway-db，无法读取持久化/积压",
        }
    send_times: dict[str, float] = {}
    for result in stats.primary:
        if result.ok:
            send_times.setdefault(result.order_id, result.sent_at)
    tenant_metrics: dict[str, Any] = {"available": False, "error": "未提供 --tenant-db-dsn"}
    if args.tenant_db_dsn:
        try:
            tenant_metrics = query_tenant_metrics(args.tenant_db_dsn, run_id, send_times)
        except Exception as exc:  # noqa: BLE001
            tenant_metrics = {"available": False, "error": str(exc)[:300]}
    observer_metrics: dict[str, Any] = {"available": False, "error": "未提供 --observer-stats-url"}
    if args.observer_stats_url:
        observer_metrics = await fetch_observer(
            args.observer_stats_url, run_id, {result.notification_id: result.sent_at for result in stats.primary if result.ok}
        )
    report = build_report(
        config,
        stats,
        args,
        gateway_rows=gateway_rows,
        drain=drain,
        tenant_metrics=tenant_metrics,
        observer_metrics=observer_metrics,
    )
    report["safety"]["production_unroutable_verified"] = (
        config.mode != "production-unroutable"
        or (
            bool(gateway_rows)
            and all(str(row.get("status")) == "unroutable" for row in gateway_rows)
        )
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"webhook-loadtest-{run_id}.json"
    md_path = output_dir / f"webhook-loadtest-{run_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"[loadtest] report_json={json_path}")
    print(f"[loadtest] report_md={md_path}")
    if args.cleanup_gateway_events:
        deleted = cleanup_gateway_events(
            args.gateway_db,
            run_id,
            apply=True,
            include_audit=False,
        )
        print(f"[loadtest] cleanup gateway rows={deleted}")
    return 0


def cleanup_gateway_events(
    gateway_db: str | None,
    run_id: str,
    *,
    apply: bool,
    include_audit: bool = False,
) -> dict[str, Any]:
    if not gateway_db:
        raise SystemExit("cleanup 需要 --gateway-db")
    connection = open_gateway_db_readonly(gateway_db)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM gateway_webhook_events WHERE notification_id LIKE ?",
            (f"{run_id}-%",),
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT COUNT(*) FROM gateway_audit_logs WHERE payload_json LIKE ?",
            (f'%{run_id}%',),
        ).fetchone()[0]
    finally:
        connection.close()
    result = {"run_id": run_id, "event_rows": int(count), "audit_rows": int(audit_count)}
    if not apply:
        print(json.dumps(result, ensure_ascii=False))
        return result
    connection = sqlite3.connect(gateway_db)
    try:
        connection.execute(
            "DELETE FROM gateway_webhook_events WHERE notification_id LIKE ?",
            (f"{run_id}-%",),
        )
        if include_audit:
            connection.execute(
                "DELETE FROM gateway_audit_logs WHERE payload_json LIKE ?",
                (f'%{run_id}%',),
            )
        connection.commit()
    finally:
        connection.close()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TikTok Webhook 百店压测工具")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="发送签名 Webhook 压测事件")
    run.add_argument("--gateway-url", required=True, help="例如 http://127.0.0.1:8100")
    run.add_argument("--app-key", required=True)
    run.add_argument("--app-secret", required=True)
    run.add_argument("--shop-id-base", type=int, default=990000000)
    run.add_argument("--shops", type=int, default=100)
    run.add_argument("--shop-ids", default="", help="逗号分隔的实际平台店铺 ID；与 --shops 二选一")
    run.add_argument("--rate", type=float, default=20.0)
    run.add_argument("--duration", type=float, default=60.0)
    run.add_argument("--events", type=int, default=None)
    run.add_argument("--concurrency", type=int, default=32)
    run.add_argument("--timeout", type=float, default=10.0)
    run.add_argument("--hot-shops", type=int, default=0)
    run.add_argument("--hot-share", type=float, default=0.0)
    run.add_argument("--duplicate-rate", type=float, default=0.0)
    run.add_argument("--duplicate-delay-min", type=float, default=0.05)
    run.add_argument("--duplicate-delay-max", type=float, default=0.5)
    run.add_argument("--out-of-order-rate", type=float, default=0.0)
    run.add_argument("--partial-rate", type=float, default=0.0)
    run.add_argument("--events-per-order", type=int, default=4)
    run.add_argument("--retry-429", type=int, default=3)
    run.add_argument("--max-retry-wait", type=float, default=10.0)
    run.add_argument("--auth-scheme", choices=("raw", "bearer"), default="raw")
    run.add_argument("--run-id", default="")
    run.add_argument("--mode", choices=("isolated", "staging", "production-unroutable"), default="isolated")
    run.add_argument("--allow-production-unroutable", action="store_true")
    run.add_argument("--gateway-db", default="", help="Gateway SQLite 只读路径，用于持久化/积压/安全验证")
    run.add_argument("--tenant-db-dsn", default="", help="可选：测试租户 MySQL DSN，读取订单写入/重复库存流水")
    run.add_argument("--observer-stats-url", default="", help="可选：测试 sink 的只读统计 URL")
    run.add_argument("--drain-grace", type=float, default=2.0)
    run.add_argument("--drain-timeout", type=float, default=300.0)
    run.add_argument("--fault-plan", default="", help="可选 JSON 故障注入计划；需 --allow-shell-faults")
    run.add_argument("--allow-shell-faults", action="store_true")
    run.add_argument("--output-dir", default="scripts/reports")
    run.add_argument("--cleanup-gateway-events", action="store_true")
    cleanup = sub.add_parser("cleanup", help="清理 Gateway SQLite 中带 run_id 前缀的测试事件")
    cleanup.add_argument("--gateway-db", required=True)
    cleanup.add_argument("--run-id", required=True)
    cleanup.add_argument("--apply", action="store_true", help="真正删除；默认只 dry-run")
    cleanup.add_argument("--include-audit", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "cleanup":
        result = cleanup_gateway_events(
            args.gateway_db,
            args.run_id if args.run_id.startswith("lt-") else f"lt-{args.run_id}",
            apply=bool(args.apply),
            include_audit=bool(args.include_audit),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    try:
        return asyncio.run(execute_run(args))
    except KeyboardInterrupt:
        print("[loadtest] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
