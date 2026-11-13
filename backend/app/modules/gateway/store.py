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
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.modules.gateway.config import GatewayConfig


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "instance_auth_enabled": "true",
    "legacy_tenant_admin_enabled": "true",
    "legacy_oauth_fallback_enabled": "true",
    "legacy_revoked_at": "",
    "migration_switched_at": "",
}


class GatewayStore:
    """SQLite 存储；每次操作独立连接，写事务 ``BEGIN IMMEDIATE`` 串行。"""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self._init_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> sqlite3.Connection:
        import os

        directory = os.path.dirname(self.config.db_path or "") or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        connection = sqlite3.connect(
            self.config.db_path,
            timeout=self.config.sqlite_busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {int(self.config.sqlite_busy_timeout_ms)}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = OFF")
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
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
            with self._read() as connection:
                connection.executescript(SCHEMA)
            with self._write() as connection:
                # 旧网关 SQLite 升级：oauth_states 增加 context_json/resolved_at。
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(gateway_oauth_states)"
                    ).fetchall()
                }
                if "context_json" not in columns:
                    connection.execute(
                        "ALTER TABLE gateway_oauth_states ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'"
                    )
                if "resolved_at" not in columns:
                    connection.execute(
                        "ALTER TABLE gateway_oauth_states ADD COLUMN resolved_at TEXT"
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
                   VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
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
            instance_id = int(cursor.lastrowid or 0)
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

    def get_instance_by_token_hash(self, token_hash: str) -> tuple[dict[str, Any] | None, sqlite3.Row | None]:
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
    def _instance_payload(row: sqlite3.Row) -> dict[str, Any]:
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
        """返回 ``(acquired_lock, existing_lock)``；已存在他人有效锁时返回现有锁。"""
        self._ensure()
        now_epoch = epoch_now()
        now = utc_now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_refresh_locks WHERE lock_key = ?", (lock_key,)
            ).fetchone()
            if row is not None and int(row["expires_at"] or 0) > now_epoch and str(row["owner"]) != owner:
                return None, dict(row)
            expires = now_epoch + max(10, int(ttl_seconds))
            connection.execute(
                """INSERT INTO gateway_refresh_locks(lock_key, instance_id, owner, acquired_at, expires_at, renewed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lock_key) DO UPDATE SET
                     instance_id=excluded.instance_id, owner=excluded.owner,
                     acquired_at=excluded.acquired_at, expires_at=excluded.expires_at,
                     renewed_at=excluded.renewed_at""",
                (lock_key, int(instance_id), owner, now, expires, now),
            )
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
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, 0, ?, ?)""",
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
            delivery_id = int(cursor.lastrowid or 0)
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
    def _delivery_payload(row: sqlite3.Row) -> dict[str, Any]:
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
            row = connection.execute(
                "SELECT * FROM gateway_rate_limits WHERE scope = ?", (scope,)
            ).fetchone()
            if row is None or int(row["window_start"] or 0) != window_start:
                connection.execute(
                    """INSERT INTO gateway_rate_limits(scope, window_start, count, updated_at)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(scope) DO UPDATE SET
                         window_start=excluded.window_start, count=1, updated_at=excluded.updated_at""",
                    (scope, window_start, utc_now()),
                )
                count = 1
            else:
                count = int(row["count"] or 0) + 1
                connection.execute(
                    "UPDATE gateway_rate_limits SET count = ?, updated_at = ? WHERE scope = ?",
                    (count, utc_now(), scope),
                )
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
