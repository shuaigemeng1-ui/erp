#!/usr/bin/env python3
"""Gateway SQLite → PostgreSQL 迁移与对账（双实例 HA 切换工具）。

设计目标：切换时旧 SQLite 入口已停写、旧文件保留只读；客户端在维护窗口内切换
``GATEWAY_DATABASE_URL`` 到 PostgreSQL，先复制全量/最后增量，再核对事件数量与状态后开放新入口。

**不自动双写/双确认**：迁移脚本只写 PostgreSQL 目标；旧 SQLite 保持原样用于回滚。
--apply 前必须停掉所有旧网关实例（脚本用确认令牌防止误操作）。

用法：
    # 1) 只读预检（默认）
    python scripts/gateway_ha_migrate.py \
      --sqlite /data/gateway.db \
      --postgres postgresql://gateway:...@pg-ha:5432/gateway

    # 2) 维护窗口：停旧入口 → 迁移 → 对账
    python scripts/gateway_ha_migrate.py ... --apply --confirm STOP-OLD-GATEWAY

    # 3) 启动两个新网关（见 deploy/gateway/ha），用相同 GATEWAY_SECRET_KEY。

依赖：psycopg（网关 HA 镜像已安装；本机可 `pip install 'psycopg[binary]'`）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

TABLES = (
    "gateway_instances",
    "gateway_channel_apps",
    "gateway_oauth_states",
    "gateway_refresh_locks",
    "gateway_deliveries",
    "gateway_rate_limits",
    "gateway_settings",
    "gateway_audit_logs",
    "gateway_webhook_shops",
    "gateway_webhook_events",
)
SERIAL_TABLES = (
    "gateway_instances",
    "gateway_deliveries",
    "gateway_audit_logs",
    "gateway_webhook_shops",
    "gateway_webhook_events",
)
EVENT_KEY_COLUMNS = (
    "id",
    "notification_id",
    "status",
    "attempts",
    "next_attempt_at",
    "duplicate_count",
    "payload_sha256",
    "lease_token",
    "lease_expires_at",
)


def _source_counts(sqlite_path: str) -> dict[str, int]:
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        counts: dict[str, int] = {}
        for table in TABLES:
            try:
                counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.OperationalError:
                counts[table] = 0
        statuses = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM gateway_webhook_events GROUP BY status"
            )
        }
        pending = connection.execute(
            "SELECT COUNT(*) FROM gateway_webhook_events WHERE status='pending'"
        ).fetchone()[0]
        duplicate_total = connection.execute(
            "SELECT COALESCE(SUM(duplicate_count),0) FROM gateway_webhook_events"
        ).fetchone()[0]
    return {
        "counts": counts,
        "webhook_statuses": statuses,
        "pending": int(pending),
        "duplicate_total": int(duplicate_total),
    }


def _event_columns(sqlite_path: str) -> list[str]:
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as connection:
        rows = connection.execute("PRAGMA table_info(gateway_webhook_events)").fetchall()
    available = {str(row[1]) for row in rows}
    return [column for column in EVENT_KEY_COLUMNS if column in available]


def _event_checksum(sqlite_path: str, columns: list[str] | None = None) -> str:
    digest = hashlib.sha256()
    selected = columns or _event_columns(sqlite_path)
    if not selected:
        return digest.hexdigest()
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM gateway_webhook_events ORDER BY id"
        ).fetchall()
        for row in rows:
            payload = {key: row[key] for key in selected}
            digest.update(
                json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            )
    return digest.hexdigest()


def _target_counts(pg_url: str) -> dict[str, int] | None:
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(pg_url, autocommit=True) as connection:
            counts: dict[str, int] = {}
            for table in TABLES:
                try:
                    counts[table] = int(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                except Exception:  # noqa: BLE001 - target schema not initialized
                    return None
            return counts
    except Exception:  # noqa: BLE001
        return None


def _init_target(pg_url: str) -> None:
    from app.modules.gateway.config import GatewayConfig
    from app.modules.gateway.store import GatewayStore

    cfg = GatewayConfig(
        secret_key="migration-only",
        admin_token="migration-only",
        database_url=pg_url,
        webhook_dispatch_enabled=False,
    )
    GatewayStore(cfg).init()


def _copy(sqlite_path: str, pg_url: str) -> dict[str, int]:
    import psycopg
    from psycopg.rows import dict_row

    copied: dict[str, int] = {}
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        with psycopg.connect(pg_url, row_factory=dict_row, autocommit=False) as target:
            for table in TABLES:
                try:
                    columns = [
                        str(row[1])
                        for row in source.execute(f"PRAGMA table_info({table})").fetchall()
                    ]
                except sqlite3.OperationalError:
                    continue
                if not columns:
                    continue
                rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                if not rows:
                    copied[table] = 0
                    continue
                placeholders = ", ".join(["%s"] * len(columns))
                column_sql = ", ".join(columns)
                conflict = (
                    "ON CONFLICT DO NOTHING"
                    if table != "gateway_settings"
                    else "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at"
                )
                sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) {conflict}"
                for row in rows:
                    target.execute(sql, tuple(row[column] for column in columns))
                copied[table] = len(rows)
            # 复制后修正 BIGSERIAL 序列；先取目标 max(id)，再 setval。
            for table in SERIAL_TABLES:
                seq = f"pg_get_serial_sequence('{table}', 'id')"
                max_id = target.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()["coalesce"]
                if int(max_id or 0) > 0:
                    target.execute(
                        f"SELECT setval({seq}, (SELECT MAX(id) FROM {table}), true)"
                    )
            target.commit()
    return copied


def _reconcile(sqlite_path: str, pg_url: str) -> dict:
    import psycopg
    from psycopg.rows import dict_row

    source = _source_counts(sqlite_path)
    event_columns = _event_columns(sqlite_path)
    source_checksum = _event_checksum(sqlite_path, event_columns)
    with psycopg.connect(pg_url, row_factory=dict_row, autocommit=True) as target:
        target_counts = {
            table: int(target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()["count"])
            for table in TABLES
        }
        target_statuses = {
            str(row["status"]): int(row["count"])
            for row in target.execute(
                "SELECT status, COUNT(*) AS count FROM gateway_webhook_events GROUP BY status"
            ).fetchall()
        }
        target_pending = int(
            target.execute(
                "SELECT COUNT(*) AS count FROM gateway_webhook_events WHERE status='pending'"
            ).fetchone()["count"]
        )
        target_checksum = hashlib.sha256()
        if event_columns:
            for row in target.execute(
                f"SELECT {', '.join(event_columns)} FROM gateway_webhook_events ORDER BY id"
            ).fetchall():
                payload = {key: row[key] for key in event_columns}
                target_checksum.update(
                    json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
                )
    report = {
        "source_counts": source["counts"],
        "target_counts": target_counts,
        "source_webhook_statuses": source["webhook_statuses"],
        "target_webhook_statuses": target_statuses,
        "source_pending": source["pending"],
        "target_pending": target_pending,
        "source_event_checksum": source_checksum,
        "target_event_checksum": target_checksum.hexdigest(),
        "counts_match": source["counts"] == target_counts,
        "statuses_match": source["webhook_statuses"] == target_statuses,
        "pending_match": source["pending"] == target_pending,
        "checksum_match": source_checksum == target_checksum.hexdigest(),
    }
    report["ok"] = all(
        report[key] for key in ("counts_match", "statuses_match", "pending_match", "checksum_match")
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gateway SQLite → PostgreSQL 迁移/对账")
    parser.add_argument("--sqlite", required=True, help="旧 Gateway SQLite 文件（只读）")
    parser.add_argument("--postgres", required=True, help="目标 PostgreSQL DSN")
    parser.add_argument("--apply", action="store_true", help="执行迁移（默认只读预检）")
    parser.add_argument("--confirm", default="", help="--apply 确认令牌 STOP-OLD-GATEWAY")
    parser.add_argument("--allow-nonempty", action="store_true", help="允许目标已有数据（幂等重跑）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    args = parser.parse_args(argv)

    if not Path(args.sqlite).is_file():
        print(f"[FAIL] SQLite 文件不存在：{args.sqlite}", file=sys.stderr)
        return 2
    source = _source_counts(args.sqlite)
    target = _target_counts(args.postgres)
    preflight = {
        "sqlite": args.sqlite,
        "postgres_host": args.postgres.split("@")[-1].split("/")[0],
        "source_counts": source["counts"],
        "source_webhook_statuses": source["webhook_statuses"],
        "source_pending": source["pending"],
        "target_initialized": target is not None,
        "target_counts": target or {},
        "apply": bool(args.apply),
    }
    print(json.dumps(preflight, ensure_ascii=False, indent=2))
    if not args.apply:
        print("[plan] 只读预检完成；确认维护窗口后加 --apply --confirm STOP-OLD-GATEWAY 执行。")
        return 0
    if args.confirm != "STOP-OLD-GATEWAY":
        print("[FAIL] --apply 需要 --confirm STOP-OLD-GATEWAY（确认旧入口已停写）", file=sys.stderr)
        return 2
    if target is not None and not args.allow_nonempty:
        nonempty = {table: count for table, count in target.items() if count > 0 and table != "gateway_settings"}
        if nonempty:
            print(
                f"[FAIL] 目标 PostgreSQL 非空：{nonempty}；如需幂等重跑加 --allow-nonempty",
                file=sys.stderr,
            )
            return 2

    try:
        _init_target(args.postgres)
        copied = _copy(args.sqlite, args.postgres)
        report = {"copied": copied, **_reconcile(args.sqlite, args.postgres)}
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 迁移异常：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        print("[FAIL] 迁移后对账不一致；不要切换流量，旧 SQLite 保留回滚。", file=sys.stderr)
        return 1
    print("[OK] 迁移与对账通过；双 Gateway 使用相同 GATEWAY_SECRET_KEY 和 GATEWAY_DATABASE_URL 启动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
