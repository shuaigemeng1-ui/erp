"""网关持久化存储（SQLite，独立于管理中心 MySQL/任务进程）。

表结构（全部本地 SQLite，无外键，网关独占）：

* ``gateway_instances``：租户实例身份、渠道/环境/店铺白名单、回调地址、令牌摘要
* ``gateway_channel_apps``：渠道共享应用配置（App Secret 加密）
* ``gateway_oauth_states``：OAuth 一次性 state（绑定实例/租户/店铺/渠道/环境）
* ``gateway_refresh_locks``：令牌刷新互斥锁（租约）
* ``gateway_deliveries``：幂等交付记录（Idempotency-Key + 尝试次数 + 重试）
* ``gateway_rate_limits``：统一限流计数（实例+路径分钟窗口）
* ``gateway_settings``：迁移/回滚开关与运行期设置
* ``gateway_audit_logs``：网关侧审计（实例发放/轮换/验证/迁移开关）

所有写操作使用 ``BEGIN IMMEDIATE`` 串行化，state 消费/锁抢占/限流计数均为原子操作；
SQLite 仅由网关进程访问，满足独立部署（不依赖中心本地任务进程）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.modules.gateway.config import GatewayConfig
from app.modules.gateway.database import Connection, Row, create_backend


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def epoch_now() -> int:
    return int(time.time())


def _json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_uuid TEXT NOT NULL UNIQUE,
    tenant_id INTEGER NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    channels_json TEXT NOT NULL DEFAULT '[]',
    environments_json TEXT NOT NULL DEFAULT '[]',
    store_ids_json TEXT NOT NULL DEFAULT '[]',
    callback_url TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL DEFAULT '',
    token_version INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_seen_at TEXT,
    verified_at TEXT
);

CREATE TABLE IF NOT EXISTS gateway_channel_apps (
    channel TEXT NOT NULL,
    environment TEXT NOT NULL,
    app_key TEXT NOT NULL DEFAULT '',
    secret_cipher TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, environment)
);

CREATE TABLE IF NOT EXISTS gateway_oauth_states (
    state_hash TEXT PRIMARY KEY,
    instance_id INTEGER NOT NULL,
    tenant_id INTEGER NOT NULL,
    store_id INTEGER NOT NULL,
    channel TEXT NOT NULL,
    environment TEXT NOT NULL,
    redirect_path TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at TEXT,
    resolved_at TEXT,
    used_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_gateway_oauth_states_expires ON gateway_oauth_states(expires_at);

CREATE TABLE IF NOT EXISTS gateway_refresh_locks (
    lock_key TEXT PRIMARY KEY,
    instance_id INTEGER NOT NULL,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    renewed_at TEXT
);

CREATE TABLE IF NOT EXISTS gateway_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    response_json TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(instance_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_gateway_deliveries_status ON gateway_deliveries(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS gateway_rate_limits (
    scope TEXT PRIMARY KEY,
    window_start INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER,
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gateway_audit_created ON gateway_audit_logs(created_at);

CREATE TABLE IF NOT EXISTS gateway_webhook_shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    platform_shop_id TEXT NOT NULL,
    instance_id INTEGER NOT NULL,
    tenant_id INTEGER NOT NULL,
    store_id INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, platform_shop_id)
);
CREATE INDEX IF NOT EXISTS ix_gateway_webhook_shops_instance ON gateway_webhook_shops(instance_id);

CREATE TABLE IF NOT EXISTS gateway_webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    notification_id TEXT NOT NULL,
    event_type_code INTEGER NOT NULL DEFAULT 0,
    event_type_name TEXT NOT NULL DEFAULT '',
    platform_shop_id TEXT NOT NULL DEFAULT '',
    instance_id INTEGER,
    tenant_id INTEGER,
    store_id INTEGER,
    payload_cipher TEXT NOT NULL DEFAULT '',
    payload_sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_status_code INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    replay_count INTEGER NOT NULL DEFAULT 0,
    failure_class TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, notification_id)
);
CREATE INDEX IF NOT EXISTS ix_gateway_webhook_events_status
    ON gateway_webhook_events(status, next_attempt_at);
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "instance_auth_enabled": "true",
    "legacy_tenant_admin_enabled": "true",
    "legacy_oauth_fallback_enabled": "true",
    "legacy_revoked_at": "",
    "migration_switched_at": "",
}


class GatewayStore:
    """Gateway store; SQLite (default) or PostgreSQL when ``GATEWAY_DATABASE_URL`` set."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.backend = create_backend(config)
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> Connection:
        """兼容旧测试/运维探针：返回当前后端连接（调用方负责 close）。"""
        return self.backend.connect()

    @contextmanager
    def _read(self) -> Iterator[Connection]:
        connection = self.backend.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[Connection]:
        connection = self.backend.connect()
        try:
            self.backend.begin(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------------ 初始化
    def init(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self._write() as connection:
                if self.backend.is_postgres:
                    # 双实例同时首次启动时串行化 schema/migration（事务级 advisory lock）。
                    connection.execute("SELECT pg_advisory_xact_lock(?)", (94715041,))
                self.backend.init_schema(connection, SCHEMA)
                # 旧网关 SQLite/Postgres 升级：oauth_states 增加 context_json/resolved_at。
                columns = self.backend.table_columns(connection, "gateway_oauth_states")
                if "context_json" not in columns:
                    connection.execute(
                        "ALTER TABLE gateway_oauth_states ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'"
                    )
                if "resolved_at" not in columns:
                    connection.execute(
                        "ALTER TABLE gateway_oauth_states ADD COLUMN resolved_at TEXT"
                    )
                # 旧网关升级（task-41）：webhook 并发租约/真实状态码/延迟/重放字段。
                webhook_columns = self.backend.table_columns(connection, "gateway_webhook_events")
                webhook_migrations = {
                    "lease_token": "TEXT NOT NULL DEFAULT ''",
                    "lease_expires_at": "BIGINT NOT NULL DEFAULT 0",
                    "last_attempt_at": "BIGINT NOT NULL DEFAULT 0",
                    "last_status_code": "INTEGER NOT NULL DEFAULT 0",
                    "latency_ms": "INTEGER NOT NULL DEFAULT 0",
                    "delivered_at": "TEXT",
                    "replay_count": "INTEGER NOT NULL DEFAULT 0",
                    "failure_class": "TEXT NOT NULL DEFAULT ''",
                }
                for column, definition in webhook_migrations.items():
                    if column not in webhook_columns:
                        connection.execute(
                            f"ALTER TABLE gateway_webhook_events ADD COLUMN {column} {definition}"
                        )
                connection.execute(
                    """CREATE INDEX IF NOT EXISTS ix_gateway_webhook_events_claim
                       ON gateway_webhook_events(status, next_attempt_at, lease_expires_at, last_attempt_at)"""
                )
                for key, value in DEFAULT_SETTINGS.items():
                    row = connection.execute(
                        "SELECT value FROM gateway_settings WHERE key = ?", (key,)
                    ).fetchone()
                    if row is not None:
                        continue
                    if key == "instance_auth_enabled":
                        value = "true" if self.config.instance_auth_enabled else "false"
                    elif key == "legacy_tenant_admin_enabled":
                        value = "true" if self.config.legacy_tenant_admin_enabled else "false"
                    elif key == "legacy_oauth_fallback_enabled":
                        value = "true" if self.config.legacy_oauth_fallback_enabled else "false"
                    connection.execute(
                        "INSERT INTO gateway_settings(key, value, updated_at) VALUES (?, ?, ?)",
                        (key, value, utc_now()),
                    )
            self._initialized = True

    def _ensure(self) -> None:
        if not self._initialized:
            self.init()

    # ------------------------------------------------------------------ 设置
    def get_setting(self, key: str, default: str = "") -> str:
        self._ensure()
        with self._read() as connection:
            row = connection.execute(
                "SELECT value FROM gateway_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self._ensure()
        with self._write() as connection:
            connection.execute(
                """INSERT INTO gateway_settings(key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )

    def setting_bool(self, key: str, default: bool = False) -> bool:
        raw = self.get_setting(key, "true" if default else "false")
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    def instance_auth_enabled(self) -> bool:
        return self.setting_bool("instance_auth_enabled", self.config.instance_auth_enabled)

    def legacy_tenant_admin_enabled(self) -> bool:
        return self.setting_bool(
            "legacy_tenant_admin_enabled", self.config.legacy_tenant_admin_enabled
        )

    def legacy_oauth_fallback_enabled(self) -> bool:
        return self.setting_bool(
            "legacy_oauth_fallback_enabled", self.config.legacy_oauth_fallback_enabled
        )

    # ------------------------------------------------------------------ 实例
    def create_instance(
        self,
        *,
        instance_uuid: str,
        tenant_id: int,
        name: str,
        channels: list[str],
        environments: list[str],
        store_ids: list[int],
        callback_url: str,
        token_hash: str,
        notes: str = "",
    ) -> dict[str, Any]:
        self._ensure()
        now = utc_now()
        with self._write() as connection:
            cursor = connection.execute(
                """INSERT INTO gateway_instances
                   (instance_uuid, tenant_id, name, status, channels_json, environments_json,
                    store_ids_json, callback_url, token_hash, token_version, notes, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, 1, ?, ?, ?)
                   RETURNING id""",
                (
                    instance_uuid,
                    int(tenant_id),
                    name,
                    _json_dumps(sorted(set(channels))),
                    _json_dumps(sorted(set(environments))),
                    _json_dumps(sorted({int(item) for item in store_ids})),
                    callback_url,
                    token_hash,
                    notes,
                    now,
                    now,
                ),
            )
            instance_id = int(cursor.fetchone()[0])
        return self.get_instance(instance_id) or {}

    def update_instance(self, instance_id: int, **fields: Any) -> dict[str, Any] | None:
        self._ensure()
        allowed = {
            "name": "name",
            "status": "status",
            "channels": "channels_json",
            "environments": "environments_json",
            "store_ids": "store_ids_json",
            "callback_url": "callback_url",
            "notes": "notes",
        }
        updates: list[str] = []
        values: list[Any] = []
        for key, column in allowed.items():
            if key not in fields or fields[key] is None:
                continue
            value = fields[key]
            if key in {"channels", "environments"}:
                value = _json_dumps(sorted({str(item).lower() for item in value}))
            elif key == "store_ids":
                value = _json_dumps(sorted({int(item) for item in value}))
            updates.append(f"{column} = ?")
            values.append(value)
        if not updates:
            return self.get_instance(instance_id)
        updates.append("updated_at = ?")
        values.append(utc_now())
        values.append(int(instance_id))
        with self._write() as connection:
            connection.execute(
                f"UPDATE gateway_instances SET {', '.join(updates)} WHERE id = ?", values
            )
        return self.get_instance(instance_id)

    def rotate_instance_token(self, instance_id: int, token_hash: str) -> dict[str, Any] | None:
        self._ensure()
        with self._write() as connection:
            connection.execute(
                """UPDATE gateway_instances
                   SET token_hash = ?, token_version = token_version + 1, updated_at = ?
                   WHERE id = ?""",
                (token_hash, utc_now(), int(instance_id)),
            )
        return self.get_instance(instance_id)

    def set_instance_status(self, instance_id: int, status: str) -> dict[str, Any] | None:
        return self.update_instance(instance_id, status=status)

    def mark_instance_seen(self, instance_id: int) -> None:
        self._ensure()
        with self._write() as connection:
            connection.execute(
                "UPDATE gateway_instances SET last_seen_at = ?, updated_at = ? WHERE id = ?",
                (utc_now(), utc_now(), int(instance_id)),
            )

    def mark_instance_verified(self, instance_id: int) -> dict[str, Any] | None:
        self._ensure()
        with self._write() as connection:
            connection.execute(
                "UPDATE gateway_instances SET verified_at = ?, updated_at = ? WHERE id = ?",
                (utc_now(), utc_now(), int(instance_id)),
            )
        return self.get_instance(instance_id)

    def get_instance(self, instance_id: int) -> dict[str, Any] | None:
        self._ensure()
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_instances WHERE id = ?", (int(instance_id),)
            ).fetchone()
        return self._instance_payload(row) if row is not None else None

    def get_instance_by_token_hash(self, token_hash: str) -> tuple[dict[str, Any] | None, Row | None]:
        self._ensure()
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_instances WHERE token_hash = ? AND token_hash != ''",
                (token_hash,),
            ).fetchone()
        return (self._instance_payload(row) if row is not None else None), row

    def list_instances(self) -> list[dict[str, Any]]:
        self._ensure()
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_instances ORDER BY id ASC"
            ).fetchall()
        return [self._instance_payload(row) for row in rows]

    def instance_counts(self) -> dict[str, int]:
        self._ensure()
        with self._read() as connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM gateway_instances WHERE status = 'active'"
            ).fetchone()[0]
            verified = connection.execute(
                "SELECT COUNT(*) FROM gateway_instances WHERE status = 'active' AND verified_at IS NOT NULL"
            ).fetchone()[0]
            total = connection.execute("SELECT COUNT(*) FROM gateway_instances").fetchone()[0]
        return {"total": int(total), "active": int(active), "verified": int(verified)}

    @staticmethod
    def _instance_payload(row: Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "instance_uuid": row["instance_uuid"],
            "tenant_id": str(row["tenant_id"]),
            "name": row["name"],
            "status": row["status"],
            "channels": _json_loads(row["channels_json"], []),
            "environments": _json_loads(row["environments_json"], []),
            "store_ids": [str(item) for item in _json_loads(row["store_ids_json"], [])],
            "callback_url": row["callback_url"],
            "token_version": int(row["token_version"] or 0),
            "has_token": bool(row["token_hash"]),
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_seen_at": row["last_seen_at"],
            "verified_at": row["verified_at"],
        }

    # ------------------------------------------------------------------ 渠道应用
    def list_channel_apps(self) -> list[dict[str, Any]]:
        self._ensure()
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_channel_apps ORDER BY channel, environment"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_channel_app(self, channel: str, environment: str) -> dict[str, Any] | None:
        self._ensure()
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_channel_apps WHERE channel = ? AND environment = ?",
                (channel, environment),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_channel_app(
        self,
        *,
        channel: str,
        environment: str,
        app_key: str,
        secret_cipher: str,
        region: str,
        extra: dict[str, Any],
        updated_by: str,
    ) -> None:
        self._ensure()
        with self._write() as connection:
            connection.execute(
                """INSERT INTO gateway_channel_apps
                   (channel, environment, app_key, secret_cipher, region, extra_json, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(channel, environment) DO UPDATE SET
                     app_key=excluded.app_key, secret_cipher=excluded.secret_cipher,
                     region=excluded.region, extra_json=excluded.extra_json,
                     updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                (
                    channel,
                    environment,
                    app_key,
                    secret_cipher,
                    region,
                    json.dumps(extra, ensure_ascii=False),
                    updated_by,
                    utc_now(),
                ),
            )

    # ------------------------------------------------------------------ Webhook 路由与 Inbox
    def upsert_webhook_shop(
        self,
        *,
        channel: str,
        platform_shop_id: str,
        instance_id: int,
        tenant_id: int,
        store_id: int,
    ) -> dict[str, Any]:
        self._ensure()
        now = utc_now()
        with self._write() as connection:
            connection.execute(
                """INSERT INTO gateway_webhook_shops
                   (channel, platform_shop_id, instance_id, tenant_id, store_id, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(channel, platform_shop_id) DO UPDATE SET
                     instance_id=excluded.instance_id,
                     tenant_id=excluded.tenant_id,
                     store_id=excluded.store_id,
                     active=1,
                     updated_at=excluded.updated_at""",
                (channel, platform_shop_id, int(instance_id), int(tenant_id), int(store_id), now, now),
            )
            row = connection.execute(
                "SELECT * FROM gateway_webhook_shops WHERE channel = ? AND platform_shop_id = ?",
                (channel, platform_shop_id),
            ).fetchone()
        return dict(row) if row is not None else {}

    def get_webhook_shop(self, channel: str, platform_shop_id: str) -> dict[str, Any] | None:
        self._ensure()
        with self._read() as connection:
            row = connection.execute(
                """SELECT * FROM gateway_webhook_shops
                   WHERE channel = ? AND platform_shop_id = ? AND active = 1""",
                (channel, platform_shop_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def deactivate_webhook_shop(self, channel: str, platform_shop_id: str) -> None:
        self._ensure()
        with self._write() as connection:
            connection.execute(
                """UPDATE gateway_webhook_shops
                   SET active = 0, updated_at = ?
                   WHERE channel = ? AND platform_shop_id = ?""",
                (utc_now(), channel, platform_shop_id),
            )

    def create_webhook_event(
        self,
        *,
        channel: str,
        notification_id: str,
        event_type_code: int,
        event_type_name: str,
        platform_shop_id: str,
        instance_id: int | None,
        tenant_id: int | None,
        store_id: int | None,
        payload_cipher: str,
        payload_sha256: str,
        status: str = "pending",
        next_attempt_at: int = 0,
    ) -> tuple[int, bool]:
        self._ensure()
        now = utc_now()
        try:
            with self._write() as connection:
                cursor = connection.execute(
                    """INSERT INTO gateway_webhook_events
                       (channel, notification_id, event_type_code, event_type_name,
                        platform_shop_id, instance_id, tenant_id, store_id,
                        payload_cipher, payload_sha256, status, next_attempt_at,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       RETURNING id""",
                    (
                        channel,
                        notification_id,
                        int(event_type_code),
                        event_type_name,
                        platform_shop_id,
                        instance_id,
                        tenant_id,
                        store_id,
                        payload_cipher,
                        payload_sha256,
                        status,
                        int(next_attempt_at),
                        now,
                        now,
                    ),
                )
                return int(cursor.fetchone()[0]), False
        except self.backend.integrity_errors:
            with self._write() as connection:
                connection.execute(
                    """UPDATE gateway_webhook_events
                       SET duplicate_count = duplicate_count + 1, updated_at = ?
                       WHERE channel = ? AND notification_id = ?""",
                    (now, channel, notification_id),
                )
                row = connection.execute(
                    """SELECT id FROM gateway_webhook_events
                       WHERE channel = ? AND notification_id = ?""",
                    (channel, notification_id),
                ).fetchone()
            return int(row["id"]) if row is not None else 0, True

    def list_pending_webhook_events(self, *, limit: int = 20) -> list[dict[str, Any]]:
        self._ensure()
        now = int(time.time())
        with self._read() as connection:
            rows = connection.execute(
                """SELECT * FROM gateway_webhook_events
                   WHERE status = 'pending' AND next_attempt_at <= ?
                     AND (lease_token = '' OR lease_expires_at <= ?)
                   ORDER BY last_attempt_at ASC, id ASC LIMIT ?""",
                (now, now, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_webhook_events(
        self,
        *,
        limit: int = 16,
        lease_seconds: int = 60,
        tenant_limit: int = 8,
        store_limit: int = 1,
        tenant_in_flight: dict[int, int] | None = None,
        store_in_flight: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        """短事务领取待投递事件；按店铺轮转 + 租户/全局容量，返回独占租约的行。

        同一店铺默认只允许一个在途请求（store_limit=1），保证平台事件顺序；不同店铺
        轮转领取，避免单一店铺积压饿死其他店铺。领取后 ``attempts`` 立即 +1，租约到期
        仍未完成的行可被重新领取（进程崩溃/HTTP 超时）。
        """
        self._ensure()
        now = int(time.time())
        batch_limit = max(1, min(1000, int(limit)))
        tenant_limits = dict(tenant_in_flight or {})
        store_limits = dict(store_in_flight or {})
        with self._write() as connection:
            # 每个店铺取少量候选（窗口函数）再轮转，避免单一店铺的大积压把候选窗口占满。
            per_store_candidates = max(1, min(50, batch_limit))
            if self.backend.is_postgres:
                rows = connection.execute(
                    """WITH candidates AS (
                           SELECT id, last_attempt_at FROM (
                               SELECT id, last_attempt_at, ROW_NUMBER() OVER (
                                   PARTITION BY channel, platform_shop_id
                                   ORDER BY last_attempt_at ASC, id ASC
                               ) AS _store_rank
                               FROM gateway_webhook_events
                               WHERE status = 'pending' AND next_attempt_at <= ?
                                 AND (lease_token = '' OR lease_expires_at <= ?)
                           ) ranked WHERE _store_rank <= ?
                           ORDER BY last_attempt_at ASC, id ASC
                           LIMIT ?
                       )
                       SELECT e.* FROM gateway_webhook_events e
                       JOIN candidates c ON c.id = e.id
                       FOR UPDATE OF e SKIP LOCKED""",
                    (
                        now,
                        now,
                        per_store_candidates,
                        max(batch_limit * 10, batch_limit),
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM (
                           SELECT *, ROW_NUMBER() OVER (
                               PARTITION BY channel, platform_shop_id
                               ORDER BY last_attempt_at ASC, id ASC
                           ) AS _store_rank
                           FROM gateway_webhook_events
                           WHERE status = 'pending' AND next_attempt_at <= ?
                             AND (lease_token = '' OR lease_expires_at <= ?)
                       ) WHERE _store_rank <= ?
                       ORDER BY last_attempt_at ASC, id ASC
                       LIMIT ?""",
                    (
                        now,
                        now,
                        per_store_candidates,
                        max(batch_limit * 10, batch_limit),
                    ),
                ).fetchall()
            groups: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                item = dict(row)
                item.pop("_store_rank", None)
                key = f"{item.get('channel') or ''}:{item.get('platform_shop_id') or ''}"
                groups.setdefault(key, []).append(item)
            for items in groups.values():
                items.sort(key=lambda item: (int(item.get("last_attempt_at") or 0), int(item["id"])))
            keys = list(groups.keys())
            selected: list[dict[str, Any]] = []
            while len(selected) < batch_limit:
                progressed = False
                for key in keys:
                    if len(selected) >= batch_limit:
                        break
                    items = groups[key]
                    while items:
                        item = items[0]
                        tenant_id = item.get("tenant_id")
                        tenant_key = int(tenant_id) if tenant_id is not None else 0
                        if store_limits.get(key, 0) >= store_limit:
                            break
                        if tenant_key and tenant_limits.get(tenant_key, 0) >= tenant_limit:
                            break
                        items.pop(0)
                        selected.append(item)
                        store_limits[key] = store_limits.get(key, 0) + 1
                        tenant_limits[tenant_key] = tenant_limits.get(tenant_key, 0) + 1
                        progressed = True
                        break
                if not progressed:
                    break
            if not selected:
                return []
            now_text = utc_now()
            lease_until = now + max(5, int(lease_seconds))
            claimed: list[dict[str, Any]] = []
            for item in selected:
                lease_token = uuid.uuid4().hex
                updated = connection.execute(
                    """UPDATE gateway_webhook_events
                       SET lease_token = ?, lease_expires_at = ?, attempts = attempts + 1,
                           last_attempt_at = ?, updated_at = ?
                       WHERE id = ? AND status = 'pending'
                         AND (lease_token = '' OR lease_expires_at <= ?)""",
                    (
                        lease_token,
                        lease_until,
                        now,
                        now_text,
                        int(item["id"]),
                        now,
                    ),
                )
                if updated.rowcount != 1:
                    continue
                fresh = connection.execute(
                    "SELECT * FROM gateway_webhook_events WHERE id = ?",
                    (int(item["id"]),),
                ).fetchone()
                if fresh is not None:
                    claimed.append(dict(fresh))
            return claimed

    def complete_webhook_event(
        self,
        event_id: int,
        lease_token: str,
        *,
        status: str,
        status_code: int,
        latency_ms: int,
        next_attempt_at: int = 0,
        last_error: str = "",
        failure_class: str = "",
        delivered: bool = False,
    ) -> bool:
        """按租约回写结果，避免过期 worker 覆盖新租约。"""
        self._ensure()
        if not lease_token:
            return False
        now = utc_now()
        with self._write() as connection:
            updated = connection.execute(
                """UPDATE gateway_webhook_events
                   SET status = ?, last_status_code = ?, latency_ms = ?, next_attempt_at = ?,
                       last_error = ?, failure_class = ?, lease_token = '', lease_expires_at = 0,
                       delivered_at = ?, updated_at = ?
                   WHERE id = ? AND lease_token = ?""",
                (
                    status,
                    int(status_code),
                    int(latency_ms),
                    int(next_attempt_at),
                    last_error[:1000],
                    failure_class[:64],
                    now if delivered else None,
                    now,
                    int(event_id),
                    str(lease_token),
                ),
            )
            return updated.rowcount == 1

    def webhook_stats(self, *, now: int | None = None) -> dict[str, Any]:
        """可观测积压：pending/oldest/delivered/dead/retry/lease/最近延迟。"""
        self._ensure()
        now_epoch = int(now if now is not None else time.time())
        with self._read() as connection:
            status_rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM gateway_webhook_events GROUP BY status"
            ).fetchall()
            counts = {str(row["status"]): int(row["count"]) for row in status_rows}
            oldest = connection.execute(
                """SELECT id, created_at FROM gateway_webhook_events
                   WHERE status = 'pending' ORDER BY created_at ASC, id ASC LIMIT 1"""
            ).fetchone()
            due = connection.execute(
                """SELECT COUNT(*) FROM gateway_webhook_events
                   WHERE status = 'pending' AND next_attempt_at <= ?
                     AND (lease_token = '' OR lease_expires_at <= ?)""",
                (now_epoch, now_epoch),
            ).fetchone()[0]
            retry = connection.execute(
                "SELECT COUNT(*) FROM gateway_webhook_events WHERE status = 'pending' AND attempts > 0"
            ).fetchone()[0]
            leased = connection.execute(
                """SELECT COUNT(*) FROM gateway_webhook_events
                   WHERE lease_token != '' AND lease_expires_at > ?""",
                (now_epoch,),
            ).fetchone()[0]
            last_delivery = connection.execute(
                """SELECT latency_ms, delivered_at, last_status_code
                   FROM gateway_webhook_events
                   WHERE delivered_at IS NOT NULL
                   ORDER BY delivered_at DESC, id DESC LIMIT 1"""
            ).fetchone()
            failure_rows = connection.execute(
                """SELECT failure_class, COUNT(*) AS count FROM gateway_webhook_events
                   WHERE failure_class != '' AND status IN ('pending', 'dead')
                   GROUP BY failure_class"""
            ).fetchall()
        oldest_age = None
        oldest_at = None
        if oldest is not None:
            oldest_at = str(oldest["created_at"])
            try:
                parsed = datetime.fromisoformat(oldest_at.replace("Z", "+00:00"))
                oldest_age = max(0, int(now_epoch - parsed.timestamp()))
            except ValueError:
                oldest_age = None
        return {
            "pending": int(counts.get("pending", 0)),
            "delivered": int(counts.get("delivered", 0)),
            "dead": int(counts.get("dead", 0)),
            "unroutable": int(counts.get("unroutable", 0)),
            "retry": int(retry),
            "leased": int(leased),
            "due": int(due),
            "failure_classes": {
                str(row["failure_class"]): int(row["count"]) for row in failure_rows
            },
            "total": int(sum(counts.values())),
            "oldest_pending_at": oldest_at,
            "oldest_pending_age_seconds": oldest_age,
            "oldest_pending": oldest_age,
            "last_delivery_latency_ms": int(last_delivery["latency_ms"] or 0)
            if last_delivery is not None
            else None,
            "latency_ms": int(last_delivery["latency_ms"] or 0)
            if last_delivery is not None
            else None,
            "last_latency_ms": int(last_delivery["latency_ms"] or 0)
            if last_delivery is not None
            else None,
            "last_delivery_at": str(last_delivery["delivered_at"])
            if last_delivery is not None
            else None,
            "last_status_code": int(last_delivery["last_status_code"] or 0)
            if last_delivery is not None
            else None,
        }

    def replay_webhook_events(
        self,
        *,
        event_ids: list[int] | None = None,
        notification_ids: list[str] | None = None,
        platform_shop_id: str = "",
        tenant_id: int | None = None,
        created_from: str = "",
        created_to: str = "",
        statuses: list[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """按事件/店铺/时间段重放：复用原行（notification ID/幂等键不变）。"""
        self._ensure()
        clauses: list[str] = []
        params: list[Any] = []
        if event_ids:
            clauses.append(f"id IN ({','.join('?' for _ in event_ids)})")
            params.extend(int(item) for item in event_ids)
        if notification_ids:
            clauses.append(f"notification_id IN ({','.join('?' for _ in notification_ids)})")
            params.extend(str(item) for item in notification_ids)
        if platform_shop_id:
            clauses.append("platform_shop_id = ?")
            params.append(str(platform_shop_id))
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(int(tenant_id))
        if created_from:
            clauses.append("created_at >= ?")
            params.append(str(created_from))
        if created_to:
            clauses.append("created_at <= ?")
            params.append(str(created_to))
        selected_statuses = [str(item) for item in (statuses or ["dead"]) if str(item)]
        if selected_statuses:
            clauses.append(f"status IN ({','.join('?' for _ in selected_statuses)})")
            params.extend(selected_statuses)
        if not clauses:
            raise ValueError("replay 至少需要一个筛选条件（事件/店铺/时间/状态）")
        where = " AND ".join(clauses)
        batch_limit = max(1, min(1000, int(limit)))
        with self._write() as connection:
            rows = connection.execute(
                f"""SELECT * FROM gateway_webhook_events WHERE {where}
                    ORDER BY id ASC LIMIT ?""",
                (*params, batch_limit),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return []
            now = utc_now()
            connection.execute(
                f"""UPDATE gateway_webhook_events
                    SET status = 'pending', attempts = 0, next_attempt_at = 0,
                        lease_token = '', lease_expires_at = 0, last_error = '',
                        last_status_code = 0, latency_ms = 0, delivered_at = NULL,
                        failure_class = '', replay_count = replay_count + 1, updated_at = ?
                    WHERE id IN ({','.join('?' for _ in ids)})""",
                (now, *ids),
            )
            refreshed = connection.execute(
                f"SELECT * FROM gateway_webhook_events WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id ASC",
                tuple(ids),
            ).fetchall()
        return [dict(row) for row in refreshed]

    def mark_webhook_event(
        self,
        event_id: int,
        *,
        status: str,
        attempts: int | None = None,
        next_attempt_at: int | None = None,
        last_error: str = "",
    ) -> None:
        self._ensure()
        fields = ["status = ?", "updated_at = ?", "last_error = ?"]
        values: list[Any] = [status, utc_now(), last_error[:1000]]
        if attempts is not None:
            fields.append("attempts = ?")
            values.append(int(attempts))
        if next_attempt_at is not None:
            fields.append("next_attempt_at = ?")
            values.append(int(next_attempt_at))
        values.append(int(event_id))
        with self._write() as connection:
            connection.execute(
                f"UPDATE gateway_webhook_events SET {', '.join(fields)} WHERE id = ?",
                tuple(values),
            )

    def channel_app_count(self) -> int:
        self._ensure()
        with self._read() as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM gateway_channel_apps").fetchone()[0]
            )

    # ------------------------------------------------------------------ OAuth state
    def create_oauth_state(
        self,
        *,
        state_hash: str,
        instance_id: int,
        tenant_id: int,
        store_id: int,
        channel: str,
        environment: str,
        redirect_path: str,
        ttl_seconds: int,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure()
        now_epoch = epoch_now()
        created_at = utc_now()
        with self._write() as connection:
            connection.execute(
                """INSERT INTO gateway_oauth_states
                   (state_hash, instance_id, tenant_id, store_id, channel, environment,
                    redirect_path, context_json, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    state_hash,
                    int(instance_id),
                    int(tenant_id),
                    int(store_id),
                    channel,
                    environment,
                    redirect_path,
                    json.dumps(context or {}, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    now_epoch + max(30, int(ttl_seconds)),
                ),
            )
        return {
            "instance_id": str(instance_id),
            "tenant_id": str(tenant_id),
            "store_id": str(store_id),
            "channel": channel,
            "environment": environment,
            "redirect_path": redirect_path,
            "created_at": created_at,
            "expires_at": now_epoch + max(30, int(ttl_seconds)),
        }

    def consume_oauth_state(
        self, state_hash: str, *, channel: str | None = None, used_ip: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        """原子消费 state；返回 ``(state, reason)``，reason 为 expired/used/not_found/channel_mismatch。"""
        self._ensure()
        now_epoch = epoch_now()
        now = utc_now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_oauth_states WHERE state_hash = ?", (state_hash,)
            ).fetchone()
            if row is None:
                return None, "not_found"
            if channel is not None and str(row["channel"]) != channel:
                return None, "channel_mismatch"
            if row["used_at"]:
                return None, "used"
            if int(row["expires_at"] or 0) <= now_epoch:
                return None, "expired"
            cursor = connection.execute(
                """UPDATE gateway_oauth_states
                   SET used_at = ?, used_ip = ?
                   WHERE state_hash = ? AND used_at IS NULL AND expires_at > ?""",
                (now, used_ip, state_hash, now_epoch),
            )
            if cursor.rowcount != 1:
                return None, "used"
        return (
            {
                "instance_id": str(row["instance_id"]),
                "tenant_id": str(row["tenant_id"]),
                "store_id": str(row["store_id"]),
                "channel": row["channel"],
                "environment": row["environment"],
                "redirect_path": row["redirect_path"],
                "created_at": row["created_at"],
                "expires_at": int(row["expires_at"] or 0),
            },
            "",
        )

    def resolve_oauth_state(
        self, state_hash: str, *, instance_id: int
    ) -> tuple[dict[str, Any] | None, str]:
        """租户实例用 state 解析授权上下文（回调完成后一次性换取）。

        state 必须已被网关回调消费（``used_at`` 非空），且只能由所属实例解析一次；
        返回 ``(payload, reason)``，reason: not_found/instance_mismatch/not_used/expired/replayed。
        """
        self._ensure()
        now_epoch = epoch_now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_oauth_states WHERE state_hash = ?", (state_hash,)
            ).fetchone()
            if row is None:
                return None, "not_found"
            if str(row["instance_id"]) != str(instance_id):
                return None, "instance_mismatch"
            if not row["used_at"]:
                return None, "not_used"
            if row["resolved_at"]:
                return None, "replayed"
            if int(row["expires_at"] or 0) <= now_epoch:
                return None, "expired"
            cursor = connection.execute(
                """UPDATE gateway_oauth_states SET resolved_at = ?
                   WHERE state_hash = ? AND used_at IS NOT NULL AND resolved_at IS NULL""",
                (utc_now(), state_hash),
            )
            if cursor.rowcount != 1:
                return None, "replayed"
        context = _json_loads(row["context_json"], {})
        return (
            {
                "instance_id": str(row["instance_id"]),
                "tenant_id": str(row["tenant_id"]),
                "store_id": str(row["store_id"]),
                "channel": row["channel"],
                "environment": row["environment"],
                "redirect_path": row["redirect_path"],
                "context": context if isinstance(context, dict) else {},
                "created_at": row["created_at"],
                "expires_at": int(row["expires_at"] or 0),
            },
            "",
        )

    # ------------------------------------------------------------------ 刷新锁
    def acquire_refresh_lock(
        self,
        *,
        lock_key: str,
        instance_id: int,
        owner: str,
        ttl_seconds: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """返回 ``(acquired_lock, existing_lock)``；已存在他人有效锁时返回现有锁。

        使用 ``INSERT ... ON CONFLICT ... WHERE 过期或同 owner ... RETURNING`` 原子抢占；
        PostgreSQL 额外 ``SELECT ... FOR UPDATE`` 串行化同一 lock_key 的竞争实例。
        """
        self._ensure()
        now_epoch = epoch_now()
        now = utc_now()
        expires = now_epoch + max(10, int(ttl_seconds))
        with self._write() as connection:
            lock_clause = " FOR UPDATE" if self.backend.is_postgres else ""
            row = connection.execute(
                f"SELECT * FROM gateway_refresh_locks WHERE lock_key = ?{lock_clause}",
                (lock_key,),
            ).fetchone()
            if (
                row is not None
                and int(row["expires_at"] or 0) > now_epoch
                and str(row["owner"]) != owner
            ):
                return None, dict(row)
            cursor = connection.execute(
                """INSERT INTO gateway_refresh_locks
                   (lock_key, instance_id, owner, acquired_at, expires_at, renewed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lock_key) DO UPDATE SET
                     instance_id=excluded.instance_id, owner=excluded.owner,
                     acquired_at=excluded.acquired_at, expires_at=excluded.expires_at,
                     renewed_at=excluded.renewed_at
                   WHERE gateway_refresh_locks.expires_at <= ? OR gateway_refresh_locks.owner = excluded.owner
                   RETURNING *""",
                (lock_key, int(instance_id), owner, now, expires, now, now_epoch),
            )
            acquired = cursor.fetchone()
            if acquired is None:
                existing = connection.execute(
                    "SELECT * FROM gateway_refresh_locks WHERE lock_key = ?", (lock_key,)
                ).fetchone()
                return None, dict(existing) if existing is not None else None
        return (
            {
                "lock_key": lock_key,
                "instance_id": str(instance_id),
                "owner": owner,
                "acquired_at": now,
                "expires_at": expires,
                "ttl_seconds": max(10, int(ttl_seconds)),
            },
            None,
        )

    def renew_refresh_lock(
        self, *, lock_key: str, owner: str, ttl_seconds: int
    ) -> tuple[dict[str, Any] | None, str]:
        self._ensure()
        now_epoch = epoch_now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_refresh_locks WHERE lock_key = ?", (lock_key,)
            ).fetchone()
            if row is None:
                return None, "not_found"
            if str(row["owner"]) != owner:
                return None, "not_owner"
            if int(row["expires_at"] or 0) <= now_epoch:
                connection.execute(
                    "DELETE FROM gateway_refresh_locks WHERE lock_key = ?", (lock_key,)
                )
                return None, "expired"
            expires = now_epoch + max(10, int(ttl_seconds))
            connection.execute(
                "UPDATE gateway_refresh_locks SET expires_at = ?, renewed_at = ? WHERE lock_key = ?",
                (expires, utc_now(), lock_key),
            )
        return (
            {
                "lock_key": lock_key,
                "owner": owner,
                "expires_at": expires,
                "ttl_seconds": max(10, int(ttl_seconds)),
            },
            "",
        )

    def release_refresh_lock(self, *, lock_key: str, owner: str) -> bool:
        self._ensure()
        with self._write() as connection:
            cursor = connection.execute(
                "DELETE FROM gateway_refresh_locks WHERE lock_key = ? AND owner = ?",
                (lock_key, owner),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------ 幂等交付
    def create_or_get_delivery(
        self,
        *,
        instance_id: int,
        idempotency_key: str,
        request_hash: str,
        kind: str,
        target: str,
        payload: dict[str, Any],
        max_attempts: int,
    ) -> tuple[dict[str, Any], bool]:
        """创建或返回已有交付；同 key 不同请求体抛 ``ValueError("idempotency_conflict")``。"""
        self._ensure()
        now = utc_now()
        with self._write() as connection:
            row = connection.execute(
                """SELECT * FROM gateway_deliveries
                   WHERE instance_id = ? AND idempotency_key = ?""",
                (int(instance_id), idempotency_key),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash:
                    raise ValueError("idempotency_conflict")
                return self._delivery_payload(row), True
            cursor = connection.execute(
                """INSERT INTO gateway_deliveries
                   (instance_id, idempotency_key, request_hash, kind, target, payload_json,
                    status, attempts, max_attempts, next_attempt_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, 0, ?, ?)
                   ON CONFLICT(instance_id, idempotency_key) DO NOTHING
                   RETURNING id""",
                (
                    int(instance_id),
                    idempotency_key,
                    request_hash,
                    kind,
                    target,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    max(1, int(max_attempts)),
                    now,
                    now,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                row = connection.execute(
                    """SELECT * FROM gateway_deliveries
                       WHERE instance_id = ? AND idempotency_key = ?""",
                    (int(instance_id), idempotency_key),
                ).fetchone()
                if row is None:
                    raise RuntimeError("delivery insert conflict but row missing")
                if str(row["request_hash"]) != request_hash:
                    raise ValueError("idempotency_conflict")
                return self._delivery_payload(row), True
            delivery_id = int(inserted[0])
            row = connection.execute(
                "SELECT * FROM gateway_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return self._delivery_payload(row), False

    def get_delivery(self, instance_id: int, delivery_id: int) -> dict[str, Any] | None:
        self._ensure()
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_deliveries WHERE id = ? AND instance_id = ?",
                (int(delivery_id), int(instance_id)),
            ).fetchone()
        return self._delivery_payload(row) if row is not None else None

    def retry_delivery(
        self,
        instance_id: int,
        delivery_id: int,
        *,
        error: str = "",
        backoff_seconds: int = 30,
    ) -> dict[str, Any] | None:
        self._ensure()
        now = utc_now()
        now_epoch = epoch_now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_deliveries WHERE id = ? AND instance_id = ?",
                (int(delivery_id), int(instance_id)),
            ).fetchone()
            if row is None:
                return None
            if str(row["status"]) in {"succeeded", "failed"}:
                return self._delivery_payload(row)
            attempts = int(row["attempts"] or 0) + 1
            max_attempts = int(row["max_attempts"] or 1)
            status = "failed" if attempts >= max_attempts else "pending"
            next_attempt_at = (
                0
                if status == "failed"
                else now_epoch + max(0, int(backoff_seconds))
            )
            connection.execute(
                """UPDATE gateway_deliveries
                   SET attempts = ?, status = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
                   WHERE id = ?""",
                (attempts, status, (error or "delivery_failed")[:500], next_attempt_at, now, int(delivery_id)),
            )
            row = connection.execute(
                "SELECT * FROM gateway_deliveries WHERE id = ?", (int(delivery_id),)
            ).fetchone()
        return self._delivery_payload(row)

    def mark_delivery_succeeded(
        self, instance_id: int, delivery_id: int, response: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self._ensure()
        with self._write() as connection:
            connection.execute(
                """UPDATE gateway_deliveries
                   SET status = 'succeeded', attempts = attempts + 1,
                       response_json = ?, next_attempt_at = 0, updated_at = ?
                   WHERE id = ? AND instance_id = ?""",
                (
                    json.dumps(response or {}, ensure_ascii=False),
                    utc_now(),
                    int(delivery_id),
                    int(instance_id),
                ),
            )
            row = connection.execute(
                "SELECT * FROM gateway_deliveries WHERE id = ?", (int(delivery_id),)
            ).fetchone()
        return self._delivery_payload(row) if row is not None else None

    @staticmethod
    def _delivery_payload(row: Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "instance_id": str(row["instance_id"]),
            "idempotency_key": row["idempotency_key"],
            "kind": row["kind"],
            "target": row["target"],
            "status": row["status"],
            "attempts": int(row["attempts"] or 0),
            "max_attempts": int(row["max_attempts"] or 0),
            "payload": _json_loads(row["payload_json"], {}),
            "response": _json_loads(row["response_json"], None),
            "last_error": row["last_error"],
            "next_attempt_at": int(row["next_attempt_at"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ------------------------------------------------------------------ 限流
    def hit_rate_limit(
        self, *, scope: str, limit: int, window_seconds: int = 60
    ) -> dict[str, Any]:
        self._ensure()
        limit = max(1, int(limit))
        window = max(1, int(window_seconds))
        now_epoch = epoch_now()
        window_start = now_epoch - (now_epoch % window)
        with self._write() as connection:
            cursor = connection.execute(
                """INSERT INTO gateway_rate_limits(scope, window_start, count, updated_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(scope) DO UPDATE SET
                     count = CASE
                       WHEN gateway_rate_limits.window_start = excluded.window_start
                       THEN gateway_rate_limits.count + 1
                       ELSE 1
                     END,
                     window_start = excluded.window_start,
                     updated_at = excluded.updated_at
                   RETURNING count""",
                (scope, window_start, utc_now()),
            )
            count = int(cursor.fetchone()[0])
        allowed = count <= limit
        reset_at = window_start + window
        return {
            "allowed": allowed,
            "limit": limit,
            "remaining": max(0, limit - count),
            "count": count,
            "retry_after": max(1, reset_at - now_epoch) if not allowed else 0,
            "reset_at": reset_at,
            "window_seconds": window,
        }

    # ------------------------------------------------------------------ 审计
    def audit(
        self,
        *,
        action: str,
        summary: str = "",
        actor: str = "",
        instance_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._ensure()
        with self._write() as connection:
            connection.execute(
                """INSERT INTO gateway_audit_logs
                   (instance_id, actor, action, summary, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    int(instance_id) if instance_id is not None else None,
                    actor,
                    action,
                    summary,
                    json.dumps(payload or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def list_audit(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self._ensure()
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_audit_logs ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "instance_id": str(row["instance_id"]) if row["instance_id"] is not None else None,
                "actor": row["actor"],
                "action": row["action"],
                "summary": row["summary"],
                "payload": _json_loads(row["payload_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ 清理
    def purge_expired(self) -> dict[str, int]:
        """清理过期 state / 锁 / 旧限流窗口（健康检查可调用，可重复执行）。"""
        self._ensure()
        now_epoch = epoch_now()
        with self._write() as connection:
            states = connection.execute(
                "DELETE FROM gateway_oauth_states WHERE expires_at < ?", (now_epoch - 86400,)
            ).rowcount
            locks = connection.execute(
                "DELETE FROM gateway_refresh_locks WHERE expires_at < ?", (now_epoch - 3600,)
            ).rowcount
            limits = connection.execute(
                "DELETE FROM gateway_rate_limits WHERE window_start < ?", (now_epoch - 3600,)
            ).rowcount
        return {"states": int(states), "locks": int(locks), "rate_limits": int(limits)}

    def stats(self) -> dict[str, int]:
        counts = self.instance_counts()
        return {
            **counts,
            "channel_apps": self.channel_app_count(),
        }


__all__ = ["GatewayStore", "SCHEMA", "epoch_now", "utc_now"]
