"""ERP 独立集成网关（阶段三基础）。

独立进程/容器运行，拥有自己的 SQLite 配置库，不读取中心 MySQL：
* 共享渠道应用配置（Shopee/TikTok 等）加密保存
* 健康检查
* 受限管理接口，使用 GATEWAY_ADMIN_TOKEN
* 不保存租户业务数据；OAuth 回调与令牌刷新后续接入

运行：
  GATEWAY_ADMIN_TOKEN=... GATEWAY_SECRET_KEY=... \
  uvicorn gateway.gateway_app:app --host 0.0.0.0 --port 8100
"""

from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator

DB_PATH = os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db")
ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")
SECRET_KEY = os.environ.get("GATEWAY_SECRET_KEY", "")
CHANNELS = {"shopee", "tiktok"}
ENVIRONMENTS = {"production", "test"}

app = FastAPI(title="ERP 集成网关", version="0.1.0", docs_url=None, redoc_url=None)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fernet() -> Fernet:
    if not SECRET_KEY:
        raise HTTPException(status_code=503, detail="网关密钥未配置")
    digest = hashlib.sha256(SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS gateway_channel_apps (
            channel TEXT NOT NULL,
            environment TEXT NOT NULL,
            app_key TEXT NOT NULL DEFAULT '',
            secret_cipher TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            extra_json TEXT NOT NULL DEFAULT '{}',
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (channel, environment)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS gateway_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    return conn


def _require_admin(token: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="网关管理令牌未配置")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无权访问网关配置")


class ChannelAppInput(BaseModel):
    app_key: str = Field(min_length=1, max_length=255)
    app_secret: str = Field(default="", max_length=512)
    region: str = Field(default="", max_length=64)
    extra: dict[str, Any] = Field(default_factory=dict)
    updated_by: str = Field(default="", max_length=100)

    @field_validator("app_secret")
    @classmethod
    def strip_secret(cls, value: str) -> str:
        return value.strip()


def _row_payload(row: sqlite3.Row, *, include_secret: bool = False) -> dict[str, Any]:
    payload = {
        "channel": row["channel"],
        "environment": row["environment"],
        "app_key": row["app_key"],
        "region": row["region"],
        "has_secret": bool(row["secret_cipher"]),
        "extra": row["extra_json"],
        "updated_at": row["updated_at"],
    }
    if include_secret:
        try:
            payload["app_secret"] = _fernet().decrypt(row["secret_cipher"].encode()).decode()
        except (InvalidToken, ValueError):
            payload["app_secret"] = ""
    return payload


@app.get("/api/v1/gateway/oauth/{channel}/callback")
def oauth_callback(channel: str, request: Request) -> RedirectResponse:
    """渠道 OAuth 统一入口：校验渠道后转发到当前租户实例回调地址。"""
    _validate(channel, "production")
    target = os.environ.get("GATEWAY_DEFAULT_TENANT_CALLBACK", "").strip()
    if not target:
        raise HTTPException(status_code=503, detail="租户回调地址未配置")
    query = request.url.query
    location = f"{target}?{query}" if query else target
    return RedirectResponse(url=location, status_code=307)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    try:
        with closing(_db()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM gateway_channel_apps").fetchone()[0]
        return {"ok": True, "service": "erp-gateway", "channel_apps": count, "time": utc_now()}
    except sqlite3.Error as exc:
        raise HTTPException(status_code=503, detail=f"网关配置库不可用：{exc}") from exc


@app.get("/api/v1/gateway/health")
def gateway_health() -> dict[str, Any]:
    return healthz()


@app.get("/api/v1/gateway/channel-apps")
def list_channel_apps(x_gateway_token: str | None = Header(default=None)) -> dict[str, Any]:
    _require_admin(x_gateway_token)
    with closing(_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM gateway_channel_apps ORDER BY channel, environment"
        ).fetchall()
    return {"items": [_row_payload(row) for row in rows]}


@app.get("/api/v1/gateway/channel-apps/{channel}/{environment}")
def get_channel_app(
    channel: str,
    environment: str,
    request: Request,
    x_gateway_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_gateway_token)
    _validate(channel, environment)
    with closing(_db()) as conn:
        row = conn.execute(
            "SELECT * FROM gateway_channel_apps WHERE channel=? AND environment=?",
            (channel, environment),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="渠道应用配置不存在")
    # 管理端只回显掩码；真实密钥仅供网关内部使用，不通过 HTTP 返回。
    payload = _row_payload(row)
    payload["secret_masked"] = "********" if row["secret_cipher"] else ""
    return payload


@app.get("/api/v1/gateway/channel-apps/{channel}/{environment}/internal")
def internal_channel_app(
    channel: str,
    environment: str,
    x_gateway_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """供受信租户实例内部拉取含密钥的渠道配置；不得暴露给浏览器。"""
    _require_admin(x_gateway_token)
    _validate(channel, environment)
    with closing(_db()) as conn:
        row = conn.execute(
            "SELECT * FROM gateway_channel_apps WHERE channel=? AND environment=?",
            (channel, environment),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="渠道应用配置不存在")
    return _row_payload(row, include_secret=True)


@app.put("/api/v1/gateway/channel-apps/{channel}/{environment}")
def put_channel_app(
    channel: str,
    environment: str,
    payload: ChannelAppInput,
    x_gateway_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_admin(x_gateway_token)
    _validate(channel, environment)
    import json

    with closing(_db()) as conn:
        existing = conn.execute(
            "SELECT * FROM gateway_channel_apps WHERE channel=? AND environment=?",
            (channel, environment),
        ).fetchone()
        secret_cipher = existing["secret_cipher"] if existing else ""
        if payload.app_secret:
            secret_cipher = _fernet().encrypt(payload.app_secret.encode()).decode()
        conn.execute(
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
                payload.app_key,
                secret_cipher,
                payload.region,
                json.dumps(payload.extra, ensure_ascii=False),
                payload.updated_by,
                utc_now(),
            ),
        )
        conn.commit()
    return {"ok": True, "channel": channel, "environment": environment}


def _validate(channel: str, environment: str) -> None:
    if channel not in CHANNELS:
        raise HTTPException(status_code=400, detail="不支持的渠道")
    if environment not in ENVIRONMENTS:
        raise HTTPException(status_code=400, detail="不支持的环境")
