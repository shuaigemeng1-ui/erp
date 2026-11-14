"""Gateway storage backend selection: SQLite (single instance) or PostgreSQL (HA).

The gateway app is written against a small DB-API-like surface used by
:class:`app.modules.gateway.store.GatewayStore`:

* ``execute(sql, params)`` with ``?`` placeholders;
* ``fetchone()/fetchall()`` returning mapping rows that also support ``row[0]``;
* ``commit()/rollback()/close()`` and transaction begin per write operation;
* ``rowcount`` on cursors.

SQLite remains the default for single-instance/local use.  Setting
``GATEWAY_DATABASE_URL=postgresql://...`` selects the PostgreSQL backend so two
stateless gateway instances can share event/instance/OAuth/lock state.

``psycopg`` is an optional dependency; it is only imported when PostgreSQL is
selected.  The HA deployment image installs it (see deploy/gateway/ha).
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any

from app.modules.gateway.config import GatewayConfig

POSTGRES_SCHEME_PREFIXES = ("postgresql://", "postgres://")


class Row(Mapping[str, Any]):
    """Small mapping/sequence row compatible with sqlite3.Row-style access."""

    __slots__ = ("_keys", "_values")

    def __init__(self, keys: tuple[str, ...], values: tuple[Any, ...]) -> None:
        self._keys = keys
        self._values = values

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        try:
            return self._values[self._keys.index(key)]
        except ValueError as exc:
            raise KeyError(key) from exc

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self) -> tuple[str, ...]:  # type: ignore[override]
        return self._keys

    def values(self) -> tuple[Any, ...]:  # type: ignore[override]
        return self._values


class Cursor:
    def __init__(self, raw: Any, backend: BaseBackend) -> None:
        self._raw = raw
        self._backend = backend

    def fetchone(self) -> Row | None:
        row = self._raw.fetchone()
        return self._backend.wrap_row(row) if row is not None else None

    def fetchall(self) -> list[Row]:
        return [self._backend.wrap_row(row) for row in self._raw.fetchall()]

    @property
    def rowcount(self) -> int:
        return int(getattr(self._raw, "rowcount", 0) or 0)

    @property
    def lastrowid(self) -> int | None:
        value = getattr(self._raw, "lastrowid", None)
        return int(value) if value is not None else None

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001 - best effort
            pass


class Connection:
    """Thin DB-API wrapper; SQL is translated by the backend."""

    def __init__(self, raw: Any, backend: BaseBackend) -> None:
        self.raw = raw
        self._backend = backend

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Cursor:
        translated = self._backend.translate_sql(sql)
        if self._backend.is_postgres:
            return Cursor(self.raw.execute(translated, tuple(params)), self._backend)
        return Cursor(self.raw.execute(translated, tuple(params)), self._backend)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


class BaseBackend:
    name = "base"
    is_postgres = False
    integrity_errors: tuple[type[BaseException], ...] = (Exception,)

    def connect(self) -> Connection:
        raise NotImplementedError

    def begin(self, connection: Connection) -> None:
        raise NotImplementedError

    def init_schema(self, connection: Connection, sqlite_schema: str) -> None:
        raise NotImplementedError

    def table_columns(self, connection: Connection, table: str) -> set[str]:
        raise NotImplementedError

    def translate_sql(self, sql: str) -> str:
        return sql

    def wrap_row(self, row: Any) -> Row:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "postgres": self.is_postgres}


class SQLiteBackend(BaseBackend):
    name = "sqlite"
    is_postgres = False

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.integrity_errors = (sqlite3.IntegrityError,)

    def connect(self) -> Connection:
        directory = os.path.dirname(self.config.db_path or "") or "."
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        raw = sqlite3.connect(
            self.config.db_path,
            timeout=self.config.sqlite_busy_timeout_ms / 1000,
            isolation_level=None,
        )
        raw.row_factory = sqlite3.Row
        raw.execute(f"PRAGMA busy_timeout = {int(self.config.sqlite_busy_timeout_ms)}")
        raw.execute("PRAGMA journal_mode = WAL")
        synchronous = str(self.config.sqlite_synchronous or "FULL").upper()
        if synchronous not in {"NORMAL", "FULL"}:
            synchronous = "FULL"
        raw.execute(f"PRAGMA synchronous = {synchronous}")
        raw.execute("PRAGMA foreign_keys = OFF")
        return Connection(raw, self)

    def begin(self, connection: Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")

    def init_schema(self, connection: Connection, sqlite_schema: str) -> None:
        connection.raw.executescript(sqlite_schema)

    def table_columns(self, connection: Connection, table: str) -> set[str]:
        rows = connection.raw.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    def wrap_row(self, row: Any) -> Row:
        if isinstance(row, Row):
            return row
        keys = tuple(row.keys())
        return Row(keys, tuple(row[key] for key in keys))


class PostgresBackend(BaseBackend):
    name = "postgresql"
    is_postgres = True

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on deployment image
            raise RuntimeError(
                "GATEWAY_DATABASE_URL 选择了 PostgreSQL，但未安装 psycopg；"
                "请使用 deploy/gateway/ha 镜像或在网关环境安装 psycopg[binary]"
            ) from exc
        self.psycopg = psycopg
        self.dict_row = dict_row
        self.integrity_errors = (psycopg.IntegrityError,)

    def connect(self) -> Connection:
        raw = self.psycopg.connect(
            self.config.database_url,
            row_factory=self.dict_row,
            autocommit=False,
            connect_timeout=max(3, int(self.config.sqlite_busy_timeout_ms / 1000)),
            application_name="erp-gateway",
        )
        return Connection(raw, self)

    def begin(self, connection: Connection) -> None:
        # psycopg opens a transaction implicitly; explicit BEGIN keeps parity with SQLite.
        connection.execute("BEGIN")

    def init_schema(self, connection: Connection, sqlite_schema: str) -> None:
        del sqlite_schema
        for statement in _split_sql(POSTGRES_SCHEMA):
            connection.execute(statement)

    def table_columns(self, connection: Connection, table: str) -> set[str]:
        rows = connection.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = ?""",
            (table,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}

    def translate_sql(self, sql: str) -> str:
        # Gateway SQL uses only positional ``?`` placeholders; no literal '?' exists.
        return sql.replace("?", "%s")

    def wrap_row(self, row: Any) -> Row:
        if isinstance(row, Row):
            return row
        if isinstance(row, dict):
            return Row(tuple(row.keys()), tuple(row.values()))
        keys = tuple(row.keys())
        return Row(keys, tuple(row[key] for key in keys))

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "postgres": True}


class GatewayStoreBackendError(RuntimeError):
    """Raised when the configured backend cannot be created."""


def create_backend(config: GatewayConfig) -> BaseBackend:
    url = (getattr(config, "database_url", "") or "").strip()
    if url.startswith(POSTGRES_SCHEME_PREFIXES):
        return PostgresBackend(config)
    if url.startswith("sqlite:"):
        path = url.removeprefix("sqlite://").removeprefix("sqlite:")
        path = path.lstrip("/") if path.startswith("/") and os.name == "nt" else path
        return SQLiteBackend(replace(config, db_path=path or config.db_path))
    if url:
        raise GatewayStoreBackendError(
            "GATEWAY_DATABASE_URL 只支持 postgresql:// 或 sqlite: 前缀（留空使用 GATEWAY_DB_PATH）"
        )
    return SQLiteBackend(config)


def _split_sql(script: str) -> list[str]:
    statements = []
    for chunk in re.split(r";\s*(?:\n|$)", script):
        statement = chunk.strip()
        if statement and not statement.startswith("--"):
            statements.append(statement)
    return statements


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_instances (
    id BIGSERIAL PRIMARY KEY,
    instance_uuid TEXT NOT NULL UNIQUE,
    tenant_id BIGINT NOT NULL,
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
    instance_id BIGINT NOT NULL,
    tenant_id BIGINT NOT NULL,
    store_id BIGINT NOT NULL,
    channel TEXT NOT NULL,
    environment TEXT NOT NULL,
    redirect_path TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at BIGINT NOT NULL,
    used_at TEXT,
    resolved_at TEXT,
    used_ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_gateway_oauth_states_expires ON gateway_oauth_states(expires_at);

CREATE TABLE IF NOT EXISTS gateway_refresh_locks (
    lock_key TEXT PRIMARY KEY,
    instance_id BIGINT NOT NULL,
    owner TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at BIGINT NOT NULL,
    renewed_at TEXT
);

CREATE TABLE IF NOT EXISTS gateway_deliveries (
    id BIGSERIAL PRIMARY KEY,
    instance_id BIGINT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_attempt_at BIGINT NOT NULL DEFAULT 0,
    response_json TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(instance_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_gateway_deliveries_status ON gateway_deliveries(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS gateway_rate_limits (
    scope TEXT PRIMARY KEY,
    window_start BIGINT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_audit_logs (
    id BIGSERIAL PRIMARY KEY,
    instance_id BIGINT,
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gateway_audit_created ON gateway_audit_logs(created_at);

CREATE TABLE IF NOT EXISTS gateway_webhook_shops (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    platform_shop_id TEXT NOT NULL,
    instance_id BIGINT NOT NULL,
    tenant_id BIGINT NOT NULL,
    store_id BIGINT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, platform_shop_id)
);
CREATE INDEX IF NOT EXISTS ix_gateway_webhook_shops_instance ON gateway_webhook_shops(instance_id);

CREATE TABLE IF NOT EXISTS gateway_webhook_events (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    notification_id TEXT NOT NULL,
    event_type_code INTEGER NOT NULL DEFAULT 0,
    event_type_name TEXT NOT NULL DEFAULT '',
    platform_shop_id TEXT NOT NULL DEFAULT '',
    instance_id BIGINT,
    tenant_id BIGINT,
    store_id BIGINT,
    payload_cipher TEXT NOT NULL DEFAULT '',
    payload_sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at BIGINT NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    lease_token TEXT NOT NULL DEFAULT '',
    lease_expires_at BIGINT NOT NULL DEFAULT 0,
    last_attempt_at BIGINT NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS ix_gateway_webhook_events_claim
    ON gateway_webhook_events(status, next_attempt_at, lease_expires_at, last_attempt_at);
"""

__all__ = [
    "BaseBackend",
    "Connection",
    "Cursor",
    "GatewayStoreBackendError",
    "POSTGRES_SCHEMA",
    "PostgresBackend",
    "Row",
    "SQLiteBackend",
    "create_backend",
]
