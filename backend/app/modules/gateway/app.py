"""独立集成网关 FastAPI 应用（实例身份 / OAuth state / 锁 / 限流 / 幂等交付）。

路由分组：

* 健康检查：``/healthz``、``/api/v1/gateway/health``
* 平台管理（``X-Gateway-Token``，平台保留）：渠道应用配置、实例登记/轮换/验证、迁移开关
* 受信实例（实例令牌）：``/api/v1/gateway/instance/*``（渠道配置、OAuth state、刷新锁、幂等交付）
* OAuth 公共回调：``/api/v1/gateway/oauth/{channel}/callback``（一次性 state，按实例 callback_url 转发）
* 迁移兼容：旧 ``/channel-apps/.../internal`` admin token 访问，仅在 legacy 开关开启时可用

网关只依赖本地 SQLite 与主密钥，不依赖管理中心数据库、Redis、Celery 或本地任务进程；
可用 :func:`create_app` 在测试中注入临时配置。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

from app.modules.gateway.config import GatewayConfig
from app.modules.gateway.security import (
    constant_time_equals,
    decrypt_secret,
    encrypt_secret,
    instance_token_hash,
    new_instance_token,
    new_oauth_state,
    oauth_state_hash,
)
from app.modules.gateway.store import GatewayStore, epoch_now, utc_now
from app.modules.gateway.webhook import (
    WEBHOOK_PATH,
    WebhookPayloadError,
    parse_notification,
    relay_signature,
    tenant_relay_url,
    verify_tiktok_signature,
)

INSTANCE_TOKEN_HEADER = "x-gateway-instance-token"
ADMIN_TOKEN_HEADER = "x-gateway-token"


# --------------------------------------------------------------------------- 错误
class GatewayError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, headers: Mapping[str, str] | None = None) -> None:
        self.status_code = int(status_code)
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        super().__init__(message)


def _error(status_code: int, code: str, message: str, **kwargs: Any) -> GatewayError:
    return GatewayError(status_code, code, message, **kwargs)


# --------------------------------------------------------------------------- 请求模型
class ChannelAppInput(BaseModel):
    app_key: str = Field(min_length=1, max_length=255)
    app_secret: str = Field(default="", max_length=1024)
    region: str = Field(default="", max_length=64)
    extra: dict[str, Any] = Field(default_factory=dict)
    updated_by: str = Field(default="", max_length=100)

    @field_validator("app_secret")
    @classmethod
    def _strip_secret(cls, value: str) -> str:
        return value.strip()


class InstanceCreateIn(BaseModel):
    tenant_id: int = Field(gt=0)
    name: str = Field(default="", max_length=100)
    instance_uuid: str = Field(default="", max_length=64)
    channels: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)
    store_ids: list[int] = Field(default_factory=list)
    callback_url: str = Field(default="", max_length=1000)
    notes: str = Field(default="", max_length=500)


class InstanceUpdateIn(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    channels: list[str] | None = None
    environments: list[str] | None = None
    store_ids: list[int] | None = None
    callback_url: str | None = Field(default=None, max_length=1000)
    notes: str | None = Field(default=None, max_length=500)


class InstanceVerifyIn(BaseModel):
    force: bool = False


class OAuthStateIn(BaseModel):
    channel: str = Field(min_length=1, max_length=32)
    environment: str = Field(min_length=1, max_length=16)
    # 0 表示“新建店铺”授权（租户侧稍后落库），>0 时校验实例店铺白名单
    store_id: int = Field(default=0, ge=0)
    redirect_path: str = Field(default="", max_length=500)
    # 租户授权上下文（authorization_link_id/member_id 等），网关只做不透明存储与一次性回传
    context: dict[str, Any] = Field(default_factory=dict)


class OAuthResolveIn(BaseModel):
    state: str = Field(min_length=1, max_length=256)


class RefreshLockIn(BaseModel):
    lock_key: str = Field(min_length=1, max_length=128)
    owner: str = Field(min_length=1, max_length=128)
    ttl_seconds: int | None = Field(default=None, ge=10, le=3600)


class RefreshLockOwnerIn(BaseModel):
    owner: str = Field(min_length=1, max_length=128)


class DeliveryIn(BaseModel):
    kind: str = Field(default="", max_length=64)
    target: str = Field(default="", max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)


class DeliveryRetryIn(BaseModel):
    error: str = Field(default="", max_length=500)


class WebhookShopIn(BaseModel):
    store_id: int = Field(gt=0)
    platform_shop_id: str = Field(min_length=1, max_length=64)


class MigrationSwitchIn(BaseModel):
    instance_auth_enabled: bool | None = None
    legacy_tenant_admin_enabled: bool | None = None
    legacy_oauth_fallback_enabled: bool | None = None
    revoke_legacy: bool = False
    force: bool = False


# --------------------------------------------------------------------------- 工具
def _iso_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _json_response_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def _append_query(url: str, extra: Mapping[str, str]) -> str:
    parts = urlsplit(url)
    existing = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    existing_keys = {key for key, _ in existing}
    merged = list(existing)
    for key, value in extra.items():
        if key not in existing_keys:
            merged.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged), parts.fragment))


def _safe_callback_url(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return ""
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise _error(400, "gateway_callback_invalid", "实例回调地址必须是 http(s) URL")
    return candidate


def _request_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_instance_token(request: Request) -> str:
    header = (request.headers.get(INSTANCE_TOKEN_HEADER) or "").strip()
    if header:
        return header
    authorization = (request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64] or ""


# --------------------------------------------------------------------------- 应用工厂
def create_app(config: GatewayConfig | None = None) -> FastAPI:
    cfg = config or GatewayConfig.from_env()
    cfg.validate()
    store = GatewayStore(cfg)
    store.init()

    application = FastAPI(
        title="ERP 集成网关",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )
    application.state.gateway_config = cfg
    application.state.gateway_store = store

    @application.exception_handler(GatewayError)
    async def _gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        headers = dict(exc.headers or {})
        response = _json_response_error(exc.status_code, exc.code, exc.message)
        for key, value in headers.items():
            response.headers[key] = value
        return response

    # ------------------------------------------------------------------ 鉴权
    def require_admin(request: Request) -> str:
        configured = (cfg.admin_token or "").strip()
        if not configured:
            raise _error(503, "gateway_admin_token_missing", "网关管理令牌未配置")
        provided = (request.headers.get(ADMIN_TOKEN_HEADER) or "").strip()
        if not provided:
            authorization = (request.headers.get("authorization") or "").strip()
            if authorization.lower().startswith("bearer "):
                provided = authorization[7:].strip()
        if not provided or not constant_time_equals(provided, configured):
            raise _error(401, "gateway_admin_unauthorized", "无权访问网关管理接口")
        return "gateway-admin"

    def require_instance(request: Request) -> dict[str, Any]:
        if not store.instance_auth_enabled():
            raise _error(403, "gateway_instance_auth_disabled", "网关实例身份认证已关闭")
        token = _extract_instance_token(request)
        if not token:
            raise _error(401, "gateway_instance_token_required", "缺少实例令牌")
        token_hash = instance_token_hash(cfg, token)
        instance, _row = store.get_instance_by_token_hash(token_hash)
        if instance is None:
            raise _error(401, "gateway_instance_invalid", "实例令牌无效")
        if instance.get("status") != "active":
            raise _error(403, "gateway_instance_disabled", "实例已停用")
        store.mark_instance_seen(int(instance["id"]))
        return instance

    def limit_instance(instance: Mapping[str, Any], request: Request) -> dict[str, Any]:
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or request.url.path
        scope = f"{instance['id']}:{route_path}"
        result = store.hit_rate_limit(scope=scope, limit=cfg.rate_limit_per_minute)
        if not result["allowed"]:
            raise _error(
                429,
                "gateway_rate_limited",
                "请求过于频繁，请稍后重试",
                headers={"Retry-After": str(result["retry_after"])},
            )
        return result

    def scope_check(instance: Mapping[str, Any], *, channel: str, environment: str) -> None:
        if channel not in instance.get("channels", []):
            raise _error(403, "gateway_instance_channel_denied", "实例无权访问该渠道")
        if environment not in instance.get("environments", []):
            raise _error(403, "gateway_instance_environment_denied", "实例无权访问该环境")

    def store_check(instance: Mapping[str, Any], store_id: int) -> None:
        if str(store_id) not in (instance.get("store_ids") or []):
            raise _error(403, "gateway_instance_store_denied", "实例无权操作该店铺")

    def channel_app_payload(row: Mapping[str, Any], *, include_secret: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "channel": row.get("channel"),
            "environment": row.get("environment"),
            "app_key": row.get("app_key") or "",
            "region": row.get("region") or "",
            "has_secret": bool(row.get("secret_cipher")),
            "extra": row.get("extra_json") or "{}",
            "updated_at": row.get("updated_at"),
        }
        if include_secret:
            payload["app_secret"] = decrypt_secret(cfg, str(row.get("secret_cipher") or ""))
        else:
            payload["secret_masked"] = "********" if row.get("secret_cipher") else ""
        return payload

    def require_channel_app_access(
        request: Request, channel: str, environment: str
    ) -> dict[str, Any]:
        """旧 internal 接口兼容：实例令牌（新链）或 admin token（legacy，受开关限制）。"""
        token = _extract_instance_token(request)
        if token:
            instance = require_instance(request)
            limit_instance(instance, request)
            scope_check(instance, channel=channel, environment=environment)
            return {"mode": "instance", "instance": instance}
        admin_header = (request.headers.get(ADMIN_TOKEN_HEADER) or "").strip()
        if admin_header or (request.headers.get("authorization") or "").lower().startswith("bearer "):
            if not store.legacy_tenant_admin_enabled():
                raise _error(403, "gateway_legacy_admin_disabled", "legacy 管理员令牌已停用")
            require_admin(request)
            return {"mode": "legacy_admin"}
        raise _error(401, "gateway_token_required", "缺少实例令牌或管理令牌")

    # ------------------------------------------------------------------ Webhook 分发
    def _deliver_webhook_event(event: Mapping[str, Any]) -> None:
        """Best-effort delivery from durable Gateway inbox to the tenant relay."""
        event_id = int(event["id"])
        attempts = int(event.get("attempts") or 0) + 1
        if not cfg.webhook_relay_secret:
            store.mark_webhook_event(
                event_id,
                status="pending",
                attempts=attempts,
                next_attempt_at=epoch_now() + 60,
                last_error="GATEWAY_WEBHOOK_RELAY_SECRET is not configured",
            )
            return
        instance = store.get_instance(int(event["instance_id"])) if event.get("instance_id") else None
        if instance is None:
            store.mark_webhook_event(event_id, status="dead", attempts=attempts, last_error="instance not found")
            return
        relay_url = tenant_relay_url(str(instance.get("callback_url") or ""))
        if not relay_url:
            store.mark_webhook_event(event_id, status="dead", attempts=attempts, last_error="tenant relay url missing")
            return
        try:
            raw_body = decrypt_secret(cfg, str(event.get("payload_cipher") or "")).encode("utf-8")
        except Exception:
            store.mark_webhook_event(event_id, status="dead", attempts=attempts, last_error="payload decrypt failed")
            return
        timestamp = str(epoch_now())
        signature = relay_signature(
            relay_secret=cfg.webhook_relay_secret,
            timestamp=timestamp,
            raw_body=raw_body,
        )
        request = UrlRequest(
            relay_url,
            data=raw_body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Gateway-Instance-Id": str(instance.get("id") or ""),
                "X-Gateway-Tenant-Id": str(instance.get("tenant_id") or ""),
                "X-Gateway-Event-Id": str(event.get("notification_id") or event_id),
                "X-Gateway-Timestamp": timestamp,
                "X-Gateway-Signature": signature,
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                status_code = int(response.status)
                response.read(2048)
        except Exception as exc:  # network/timeout/TLS
            status_code = 0
            last_error = str(exc)[:500]
        else:
            last_error = ""
        if 200 <= status_code < 300 or status_code == 409:
            store.mark_webhook_event(event_id, status="delivered", attempts=attempts)
            return
        if status_code in {400, 413, 422}:
            store.mark_webhook_event(
                event_id, status="dead", attempts=attempts, last_error=last_error or f"tenant returned {status_code}"
            )
            return
        # 5xx、超时、连接失败、401/403/404、以及长期配置故障都不能丢消息：
        # 保持 pending，持续退避重试；租户升级/故障恢复后自动补投。
        store.mark_webhook_event(
            event_id,
            status="pending",
            attempts=attempts,
            next_attempt_at=epoch_now() + min(3600, 5 * (2 ** min(attempts, 8))),
            last_error=last_error or f"tenant returned {status_code}",
        )

    async def _webhook_dispatcher_loop() -> None:
        interval = max(1, int(cfg.webhook_dispatch_interval_seconds or 2))
        while True:
            try:
                if cfg.webhook_dispatch_enabled:
                    pending = await asyncio.to_thread(store.list_pending_webhook_events, limit=20)
                    for event in pending:
                        await asyncio.to_thread(_deliver_webhook_event, event)
            except Exception:  # noqa: BLE001 - dispatcher must survive one bad row
                pass
            await asyncio.sleep(interval)

    @application.on_event("startup")
    async def _start_webhook_dispatcher() -> None:
        if not cfg.webhook_dispatch_enabled:
            return
        task = asyncio.create_task(_webhook_dispatcher_loop())
        application.state.webhook_dispatcher_task = task

    @application.on_event("shutdown")
    async def _stop_webhook_dispatcher() -> None:
        task = getattr(application.state, "webhook_dispatcher_task", None)
        if task is not None:
            task.cancel()

    @application.post(WEBHOOK_PATH)
    async def receive_tiktok_webhook(request: Request) -> Response:
        """Public TikTok callback: verify raw body, persist, ACK, then dispatch."""
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > int(cfg.webhook_max_body_bytes):
                    return Response(status_code=413)
            except ValueError:
                return Response(status_code=400)
        raw_body = await request.body()
        if len(raw_body) > int(cfg.webhook_max_body_bytes):
            return Response(status_code=413)
        channel_app = store.get_channel_app("tiktok", "production")
        if channel_app is None or not channel_app.get("app_key") or not channel_app.get("secret_cipher"):
            return Response(status_code=503)
        signature = (request.headers.get("authorization") or request.headers.get("x-tiktok-signature") or "").strip()
        if signature.lower().startswith("bearer "):
            signature = signature[7:].strip()
        app_secret = decrypt_secret(cfg, str(channel_app.get("secret_cipher") or ""))
        if not verify_tiktok_signature(
            app_key=str(channel_app.get("app_key") or ""),
            app_secret=app_secret,
            raw_body=raw_body,
            received_signature=signature,
        ):
            return Response(status_code=401)
        try:
            notification = parse_notification(raw_body)
        except WebhookPayloadError:
            return Response(status_code=400)
        shop = store.get_webhook_shop("tiktok", notification.platform_shop_id)
        try:
            payload_cipher = encrypt_secret(
                cfg,
                json.dumps(notification.payload, ensure_ascii=False, separators=(",", ":")),
            )
            payload_sha256 = hashlib.sha256(raw_body).hexdigest()
            store.create_webhook_event(
                channel="tiktok",
                notification_id=notification.notification_id,
                event_type_code=notification.event_type_code,
                event_type_name=notification.event_type_name,
                platform_shop_id=notification.platform_shop_id,
                instance_id=int(shop["instance_id"]) if shop else None,
                tenant_id=int(shop["tenant_id"]) if shop else None,
                store_id=int(shop["store_id"]) if shop else None,
                payload_cipher=payload_cipher,
                payload_sha256=payload_sha256,
                status="pending" if shop else "unroutable",
                next_attempt_at=0 if shop else 0,
            )
        except Exception:  # noqa: BLE001 - persistence failure must trigger platform retry
            return Response(status_code=503)
        if not shop:
            store.audit(
                action="webhook.unroutable",
                actor="tiktok",
                summary=f"未知 TikTok 店铺 {notification.platform_shop_id}",
                payload={"notification_id": notification.notification_id},
            )
        return Response(status_code=200)

    @application.post("/api/v1/gateway/instance/webhook-shops")
    def register_webhook_shop(body: WebhookShopIn, request: Request) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        if "tiktok" not in (instance.get("channels") or []):
            raise _error(403, "gateway_instance_channel_denied", "实例无权访问 TikTok 渠道")
        store_check(instance, int(body.store_id))
        existing = store.get_webhook_shop("tiktok", body.platform_shop_id)
        if existing is not None and int(existing["instance_id"]) != int(instance["id"]):
            raise _error(409, "gateway_webhook_shop_conflict", "该平台店铺已绑定其他实例")
        row = store.upsert_webhook_shop(
            channel="tiktok",
            platform_shop_id=body.platform_shop_id,
            instance_id=int(instance["id"]),
            tenant_id=int(instance["tenant_id"]),
            store_id=int(body.store_id),
        )
        store.audit(
            action="webhook.shop.register",
            actor="instance",
            instance_id=int(instance["id"]),
            summary=f"注册 TikTok Webhook 店铺 {body.platform_shop_id}",
            payload={"store_id": body.store_id},
        )
        return {"shop": row}

    @application.delete("/api/v1/gateway/instance/webhook-shops/{platform_shop_id}")
    def unregister_webhook_shop(platform_shop_id: str, request: Request) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        existing = store.get_webhook_shop("tiktok", platform_shop_id)
        if existing is not None and int(existing["instance_id"]) != int(instance["id"]):
            raise _error(404, "gateway_webhook_shop_not_found", "店铺映射不存在")
        store.deactivate_webhook_shop("tiktok", platform_shop_id)
        store.audit(
            action="webhook.shop.unregister",
            actor="instance",
            instance_id=int(instance["id"]),
            summary=f"停用 TikTok Webhook 店铺 {platform_shop_id}",
        )
        return {"deactivated": True}

    # ------------------------------------------------------------------ 健康检查
    def health_payload() -> dict[str, Any]:
        if cfg.purge_expired_on_health:
            store.purge_expired()
        return {
            "ok": True,
            "service": "erp-gateway",
            "version": "1.0.0",
            "time": utc_now(),
            "channel_apps": store.channel_app_count(),
            "instances": store.instance_counts(),
            "flags": {
                "instance_auth_enabled": store.instance_auth_enabled(),
                "legacy_tenant_admin_enabled": store.legacy_tenant_admin_enabled(),
                "legacy_oauth_fallback_enabled": store.legacy_oauth_fallback_enabled(),
            },
        }

    @application.get("/healthz")
    def healthz() -> dict[str, Any]:
        return health_payload()

    @application.get("/api/v1/gateway/health")
    def gateway_health() -> dict[str, Any]:
        return health_payload()

    # ------------------------------------------------------------------ 渠道应用（平台管理）
    @application.get("/api/v1/gateway/channel-apps")
    def list_channel_apps(request: Request) -> dict[str, Any]:
        require_admin(request)
        return {
            "items": [channel_app_payload(row, include_secret=False) for row in store.list_channel_apps()]
        }

    @application.get("/api/v1/gateway/channel-apps/{channel}/{environment}")
    def get_channel_app(channel: str, environment: str, request: Request) -> dict[str, Any]:
        require_admin(request)
        _validate_channel_env(cfg, channel, environment)
        row = store.get_channel_app(channel, environment)
        if row is None:
            raise _error(404, "gateway_channel_app_not_found", "渠道应用配置不存在")
        return channel_app_payload(row, include_secret=False)

    @application.get("/api/v1/gateway/channel-apps/{channel}/{environment}/internal")
    def legacy_internal_channel_app(channel: str, environment: str, request: Request) -> dict[str, Any]:
        _validate_channel_env(cfg, channel, environment)
        access = require_channel_app_access(request, channel, environment)
        row = store.get_channel_app(channel, environment)
        if row is None:
            raise _error(404, "gateway_channel_app_not_found", "渠道应用配置不存在")
        payload = channel_app_payload(row, include_secret=True)
        payload["auth_mode"] = access["mode"]
        return payload

    @application.put("/api/v1/gateway/channel-apps/{channel}/{environment}")
    def put_channel_app(
        channel: str, environment: str, body: ChannelAppInput, request: Request
    ) -> dict[str, Any]:
        admin = require_admin(request)
        _validate_channel_env(cfg, channel, environment)
        existing = store.get_channel_app(channel, environment) or {}
        secret_cipher = str(existing.get("secret_cipher") or "")
        if body.app_secret:
            secret_cipher = encrypt_secret(cfg, body.app_secret)
        store.upsert_channel_app(
            channel=channel,
            environment=environment,
            app_key=body.app_key,
            secret_cipher=secret_cipher,
            region=body.region,
            extra=body.extra,
            updated_by=body.updated_by or admin,
        )
        store.audit(
            action="channel_app.update",
            actor=admin,
            summary=f"更新渠道应用 {channel}/{environment}",
            payload={"channel": channel, "environment": environment},
        )
        return {"ok": True, "channel": channel, "environment": environment}

    # ------------------------------------------------------------------ 实例受限接口
    @application.get("/api/v1/gateway/instance/me")
    def instance_me(request: Request) -> dict[str, Any]:
        instance = require_instance(request)
        rate = limit_instance(instance, request)
        return {
            "instance": instance,
            "auth_mode": "instance_token",
            "rate_limit": {"limit": rate["limit"], "remaining": rate["remaining"]},
            "flags": {
                "instance_auth_enabled": store.instance_auth_enabled(),
                "legacy_tenant_admin_enabled": store.legacy_tenant_admin_enabled(),
            },
        }

    @application.get("/api/v1/gateway/instance/channel-apps/{channel}/{environment}")
    def instance_channel_app(channel: str, environment: str, request: Request) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        _validate_channel_env(cfg, channel, environment)
        scope_check(instance, channel=channel, environment=environment)
        row = store.get_channel_app(channel, environment)
        if row is None:
            raise _error(404, "gateway_channel_app_not_found", "渠道应用配置不存在")
        payload = channel_app_payload(row, include_secret=True)
        payload["auth_mode"] = "instance"
        payload["instance_id"] = instance["id"]
        return payload

    @application.post("/api/v1/gateway/instance/oauth/states")
    def create_oauth_state(body: OAuthStateIn, request: Request) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        channel = body.channel.strip().lower()
        environment = body.environment.strip().lower()
        _validate_channel_env(cfg, channel, environment)
        scope_check(instance, channel=channel, environment=environment)
        if body.store_id > 0:
            store_check(instance, body.store_id)
        context = dict(body.context or {})
        encoded_context = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        if len(encoded_context.encode("utf-8")) > 4096:
            raise _error(422, "gateway_oauth_context_too_large", "授权上下文超过 4KB")
        state = new_oauth_state()
        state_hash = oauth_state_hash(cfg, state)
        record = store.create_oauth_state(
            state_hash=state_hash,
            instance_id=int(instance["id"]),
            tenant_id=int(instance["tenant_id"]),
            store_id=int(body.store_id),
            channel=channel,
            environment=environment,
            redirect_path=body.redirect_path,
            ttl_seconds=cfg.oauth_state_ttl_seconds,
            context=context,
        )
        return {
            "state": state,
            "instance_id": instance["id"],
            "tenant_id": instance["tenant_id"],
            "store_id": str(body.store_id),
            "channel": channel,
            "environment": environment,
            "expires_at": record["expires_at"],
            "expires_at_iso": _iso_from_epoch(int(record["expires_at"])),
            "callback_url": instance.get("callback_url") or cfg.legacy_default_callback,
        }

    @application.post("/api/v1/gateway/instance/oauth/resolve")
    def resolve_oauth_state(body: OAuthResolveIn, request: Request) -> dict[str, Any]:
        """租户回调凭网关 state 一次性换取绑定上下文（回调完成后，state 已消费）。"""
        instance = require_instance(request)
        limit_instance(instance, request)
        payload, reason = store.resolve_oauth_state(
            oauth_state_hash(cfg, body.state), instance_id=int(instance["id"])
        )
        if payload is None:
            status = 403 if reason == "instance_mismatch" else 410
            if reason == "not_found":
                status = 404
            elif reason == "not_used":
                status = 409
            raise _error(
                status,
                f"gateway_oauth_state_{reason}",
                {
                    "not_found": "授权 state 不存在",
                    "instance_mismatch": "授权 state 不属于本实例",
                    "not_used": "授权回调尚未完成，不能解析 state",
                    "expired": "授权 state 已过期",
                    "replayed": "授权 state 已被解析（重放被拒绝）",
                }.get(reason, "授权 state 无效"),
            )
        payload["auth_mode"] = "instance"
        return payload

    # ------------------------------------------------------------------ 刷新锁
    def _lock_key(instance: Mapping[str, Any], key: str) -> str:
        return f"{instance['id']}:{key}"

    @application.post("/api/v1/gateway/instance/refresh-locks")
    def acquire_refresh_lock(body: RefreshLockIn, request: Request) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        ttl = int(body.ttl_seconds or cfg.refresh_lock_ttl_seconds)
        acquired, existing = store.acquire_refresh_lock(
            lock_key=_lock_key(instance, body.lock_key),
            instance_id=int(instance["id"]),
            owner=body.owner,
            ttl_seconds=ttl,
        )
        if acquired is None:
            raise _error(
                409,
                "gateway_refresh_lock_conflict",
                "同一凭据正在刷新，请稍后重试",
                headers={"Retry-After": str(max(1, int(existing["expires_at"] or 0) - epoch_now()))}
                if existing
                else None,
            )
        return {"ok": True, "lock": acquired}

    @application.post("/api/v1/gateway/instance/refresh-locks/{lock_key}/renew")
    def renew_refresh_lock(
        lock_key: str, body: RefreshLockOwnerIn, request: Request
    ) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        renewed, reason = store.renew_refresh_lock(
            lock_key=_lock_key(instance, lock_key),
            owner=body.owner,
            ttl_seconds=cfg.refresh_lock_ttl_seconds,
        )
        if renewed is None:
            status = 404 if reason == "not_found" else 409
            raise _error(status, f"gateway_refresh_lock_{reason}", "刷新锁续约失败")
        return {"ok": True, "lock": renewed}

    @application.post("/api/v1/gateway/instance/refresh-locks/{lock_key}/release")
    def release_refresh_lock(
        lock_key: str, body: RefreshLockOwnerIn, request: Request
    ) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        released = store.release_refresh_lock(
            lock_key=_lock_key(instance, lock_key), owner=body.owner
        )
        return {"ok": True, "released": released}

    # ------------------------------------------------------------------ 幂等交付
    @application.post("/api/v1/gateway/instance/deliveries")
    def create_delivery(
        body: DeliveryIn,
        request: Request,
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        key = (idempotency_key or "").strip()
        if not key or len(key) > 128:
            raise _error(422, "gateway_idempotency_key_required", "必须提供 1~128 字符的 Idempotency-Key")
        try:
            delivery, replayed = store.create_or_get_delivery(
                instance_id=int(instance["id"]),
                idempotency_key=key,
                request_hash=_request_hash({"kind": body.kind, "target": body.target, "payload": body.payload}),
                kind=body.kind,
                target=body.target,
                payload=body.payload,
                max_attempts=cfg.delivery_max_attempts,
            )
        except ValueError as exc:
            raise _error(409, "gateway_idempotency_conflict", "同一 Idempotency-Key 的请求体不一致") from exc
        return {"delivery": delivery, "replayed": replayed}

    @application.get("/api/v1/gateway/instance/deliveries/{delivery_id}")
    def get_delivery(delivery_id: int, request: Request) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        delivery = store.get_delivery(int(instance["id"]), delivery_id)
        if delivery is None:
            raise _error(404, "gateway_delivery_not_found", "交付记录不存在")
        return {"delivery": delivery}

    @application.post("/api/v1/gateway/instance/deliveries/{delivery_id}/retry")
    def retry_delivery(
        delivery_id: int, body: DeliveryRetryIn, request: Request
    ) -> dict[str, Any]:
        instance = require_instance(request)
        limit_instance(instance, request)
        delivery = store.retry_delivery(
            int(instance["id"]),
            delivery_id,
            error=body.error,
            backoff_seconds=cfg.delivery_retry_backoff_seconds,
        )
        if delivery is None:
            raise _error(404, "gateway_delivery_not_found", "交付记录不存在")
        return {"delivery": delivery}

    # ------------------------------------------------------------------ OAuth 回调
    @application.get("/api/v1/gateway/oauth/{channel}/callback")
    def oauth_callback(channel: str, request: Request, state: str = Query(default="")) -> RedirectResponse:
        channel_value = (channel or "").strip().lower()
        if not cfg.channel_supported(channel_value):
            raise _error(400, "gateway_channel_unsupported", "不支持的渠道")
        if state:
            state_hash = oauth_state_hash(cfg, state)
            record, reason = store.consume_oauth_state(
                state_hash, channel=channel_value, used_ip=_client_ip(request)
            )
            if record is None:
                status = 410 if reason in {"used", "expired"} else 400
                raise _error(
                    status,
                    f"gateway_oauth_state_{reason}",
                    {
                        "used": "授权 state 已被使用（重放被拒绝）",
                        "expired": "授权 state 已过期，请重新发起授权",
                        "channel_mismatch": "授权 state 与回调渠道不一致",
                        "not_found": "授权 state 无效",
                    }.get(reason, "授权 state 无效"),
                )
            instance = store.get_instance(int(record["instance_id"]))
            if instance is None or instance.get("status") != "active":
                raise _error(403, "gateway_instance_disabled", "实例已停用，不能完成授权")
            target = _safe_callback_url(str(instance.get("callback_url") or ""))
            if not target:
                target = _safe_callback_url(cfg.legacy_default_callback)
            if not target:
                raise _error(503, "gateway_callback_missing", "实例未配置回调地址")
            extra = {
                "gateway_instance_id": str(instance["id"]),
                "gateway_state": state,
                "gateway_channel": channel_value,
            }
            location = _append_query(target, {**dict(parse_qsl(request.url.query)), **extra})
            return RedirectResponse(url=location, status_code=302)
        # 无 state：仅 legacy 回退开关开启时保留旧的固定回调重定向
        if store.legacy_oauth_fallback_enabled() and cfg.legacy_default_callback:
            location = f"{cfg.legacy_default_callback}?{request.url.query}" if request.url.query else cfg.legacy_default_callback
            return RedirectResponse(url=location, status_code=307)
        raise _error(400, "gateway_oauth_state_required", "缺少一次性授权 state")

    # ------------------------------------------------------------------ 平台实例管理
    @application.get("/api/v1/gateway/admin/instances")
    def admin_list_instances(request: Request) -> dict[str, Any]:
        require_admin(request)
        return {"items": store.list_instances()}

    @application.post("/api/v1/gateway/admin/instances")
    def admin_create_instance(body: InstanceCreateIn, request: Request) -> dict[str, Any]:
        admin = require_admin(request)
        channels = [item.strip().lower() for item in (body.channels or list(cfg.channels))]
        environments = [
            item.strip().lower() for item in (body.environments or list(cfg.environments))
        ]
        _validate_scopes(cfg, channels, environments)
        token = new_instance_token()
        instance_uuid = (body.instance_uuid or "").strip() or str(uuid.uuid4())
        instance = store.create_instance(
            instance_uuid=instance_uuid,
            tenant_id=body.tenant_id,
            name=body.name or f"tenant-{body.tenant_id}",
            channels=channels,
            environments=environments,
            store_ids=[int(item) for item in body.store_ids],
            callback_url=_safe_callback_url(body.callback_url),
            token_hash=instance_token_hash(cfg, token),
            notes=body.notes,
        )
        store.audit(
            action="instance.create",
            actor=admin,
            instance_id=int(instance["id"]),
            summary=f"登记实例 {instance_uuid} tenant={body.tenant_id}",
            payload={"channels": channels, "environments": environments},
        )
        return {"instance": instance, "instance_token": token, "token_returned_once": True}

    @application.get("/api/v1/gateway/admin/instances/{instance_id}")
    def admin_get_instance(instance_id: int, request: Request) -> dict[str, Any]:
        require_admin(request)
        instance = store.get_instance(instance_id)
        if instance is None:
            raise _error(404, "gateway_instance_not_found", "实例不存在")
        return {"instance": instance}

    @application.patch("/api/v1/gateway/admin/instances/{instance_id}")
    def admin_update_instance(
        instance_id: int, body: InstanceUpdateIn, request: Request
    ) -> dict[str, Any]:
        admin = require_admin(request)
        if body.channels is not None or body.environments is not None:
            instance = store.get_instance(instance_id) or {}
            channels = [item.strip().lower() for item in (body.channels or instance.get("channels") or [])]
            environments = [
                item.strip().lower()
                for item in (body.environments or instance.get("environments") or [])
            ]
            _validate_scopes(cfg, channels, environments)
        updated = store.update_instance(
            instance_id,
            name=body.name,
            status=body.status,
            channels=body.channels,
            environments=body.environments,
            store_ids=body.store_ids,
            callback_url=_safe_callback_url(body.callback_url) if body.callback_url is not None else None,
            notes=body.notes,
        )
        if updated is None:
            raise _error(404, "gateway_instance_not_found", "实例不存在")
        store.audit(
            action="instance.update",
            actor=admin,
            instance_id=instance_id,
            summary=f"更新实例 {instance_id}",
            payload=body.model_dump(exclude_none=True),
        )
        return {"instance": updated}

    @application.post("/api/v1/gateway/admin/instances/{instance_id}/rotate-token")
    def admin_rotate_instance_token(instance_id: int, request: Request) -> dict[str, Any]:
        admin = require_admin(request)
        instance = store.get_instance(instance_id)
        if instance is None:
            raise _error(404, "gateway_instance_not_found", "实例不存在")
        token = new_instance_token()
        updated = store.rotate_instance_token(instance_id, instance_token_hash(cfg, token))
        store.audit(
            action="instance.rotate_token",
            actor=admin,
            instance_id=instance_id,
            summary="轮换实例令牌（旧令牌立即失效）",
        )
        return {"instance": updated, "instance_token": token, "token_returned_once": True}

    @application.post("/api/v1/gateway/admin/instances/{instance_id}/verify")
    def admin_verify_instance(
        instance_id: int, body: InstanceVerifyIn, request: Request
    ) -> dict[str, Any]:
        admin = require_admin(request)
        instance = store.get_instance(instance_id)
        if instance is None:
            raise _error(404, "gateway_instance_not_found", "实例不存在")
        if instance.get("status") != "active":
            raise _error(409, "gateway_instance_not_active", "实例未启用，不能验证")
        if not instance.get("has_token"):
            raise _error(409, "gateway_instance_token_missing", "实例未发放令牌")
        if not instance.get("channels") or not instance.get("environments"):
            raise _error(409, "gateway_instance_scope_empty", "实例渠道/环境白名单为空")
        if not body.force:
            last_seen = instance.get("last_seen_at")
            if not last_seen:
                raise _error(409, "gateway_instance_not_seen", "实例尚未通过新链调用网关")
            try:
                seen_epoch = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00")).timestamp()
            except ValueError as exc:
                raise _error(409, "gateway_instance_not_seen", "实例最近调用时间无效") from exc
            if epoch_now() - seen_epoch > cfg.verify_window_seconds:
                raise _error(409, "gateway_instance_not_seen", "实例最近调用已超过验证窗口")
        updated = store.mark_instance_verified(instance_id)
        store.audit(
            action="instance.verify",
            actor=admin,
            instance_id=instance_id,
            summary="实例新调用链验证通过",
            payload={"force": body.force},
        )
        return {"instance": updated, "verified": True}

    # ------------------------------------------------------------------ 迁移/回滚
    def migration_payload() -> dict[str, Any]:
        counts = store.instance_counts()
        all_verified = counts["active"] > 0 and counts["verified"] >= counts["active"]
        return {
            "instance_auth_enabled": store.instance_auth_enabled(),
            "legacy_tenant_admin_enabled": store.legacy_tenant_admin_enabled(),
            "legacy_oauth_fallback_enabled": store.legacy_oauth_fallback_enabled(),
            "instances": {
                "total": counts["total"],
                "active": counts["active"],
                "verified": counts["verified"],
                "all_active_verified": all_verified,
            },
            "legacy_revoked_at": store.get_setting("legacy_revoked_at", ""),
            "migration_switched_at": store.get_setting("migration_switched_at", ""),
            "rollback_hint": (
                "全部 active 实例已验证；可 POST /admin/migration/switch {revoke_legacy:true}"
                if all_verified and store.legacy_tenant_admin_enabled()
                else "先让实例走 /instance/* 新链并 verify，再撤销 legacy"
            ),
        }

    @application.get("/api/v1/gateway/admin/migration")
    def admin_migration(request: Request) -> dict[str, Any]:
        require_admin(request)
        return migration_payload()

    @application.post("/api/v1/gateway/admin/migration/switch")
    def admin_migration_switch(body: MigrationSwitchIn, request: Request) -> dict[str, Any]:
        admin = require_admin(request)
        counts = store.instance_counts()
        all_verified = counts["active"] > 0 and counts["verified"] >= counts["active"]
        if body.revoke_legacy:
            if not body.force and not all_verified:
                raise _error(
                    409,
                    "gateway_migration_not_verified",
                    "仍有 active 实例未通过新链验证，不能撤销 legacy（可 force 仅用于离线演练）",
                )
            store.set_setting("legacy_tenant_admin_enabled", "false")
            store.set_setting("legacy_oauth_fallback_enabled", "false")
            store.set_setting("legacy_revoked_at", utc_now())
            store.audit(
                action="migration.revoke_legacy",
                actor=admin,
                summary="撤销 legacy 租户管理员令牌访问",
                payload={"force": body.force, "active": counts["active"], "verified": counts["verified"]},
            )
        if body.instance_auth_enabled is not None:
            store.set_setting("instance_auth_enabled", "true" if body.instance_auth_enabled else "false")
        if body.legacy_tenant_admin_enabled is not None:
            store.set_setting(
                "legacy_tenant_admin_enabled",
                "true" if body.legacy_tenant_admin_enabled else "false",
            )
            if body.legacy_tenant_admin_enabled:
                store.set_setting("legacy_revoked_at", "")
        if body.legacy_oauth_fallback_enabled is not None:
            store.set_setting(
                "legacy_oauth_fallback_enabled",
                "true" if body.legacy_oauth_fallback_enabled else "false",
            )
        store.set_setting("migration_switched_at", utc_now())
        store.audit(
            action="migration.switch",
            actor=admin,
            summary="调整网关迁移/回滚开关",
            payload=body.model_dump(),
        )
        return migration_payload()

    @application.get("/api/v1/gateway/admin/audit-logs")
    def admin_audit_logs(
        request: Request, limit: int = Query(default=100, ge=1, le=500)
    ) -> dict[str, Any]:
        require_admin(request)
        return {"items": store.list_audit(limit=limit)}

    return application


# --------------------------------------------------------------------------- 校验
def _validate_channel_env(cfg: GatewayConfig, channel: str, environment: str) -> None:
    if not cfg.channel_supported(channel):
        raise _error(400, "gateway_channel_unsupported", "不支持的渠道")
    if not cfg.environment_supported(environment):
        raise _error(400, "gateway_environment_unsupported", "不支持的环境")


def _validate_scopes(
    cfg: GatewayConfig, channels: list[str], environments: list[str]
) -> None:
    if not channels or not environments:
        raise _error(422, "gateway_instance_scope_empty", "渠道与环境白名单不能为空")
    for channel in channels:
        if not cfg.channel_supported(channel):
            raise _error(422, "gateway_channel_unsupported", f"不支持的渠道：{channel}")
    for environment in environments:
        if not cfg.environment_supported(environment):
            raise _error(422, "gateway_environment_unsupported", f"不支持的环境：{environment}")


__all__ = ["GatewayError", "create_app"]
