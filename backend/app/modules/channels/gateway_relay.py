"""租户侧接入独立网关：OAuth relay（一次性 state）与令牌刷新互斥锁。

开关均来自环境变量（不改生产配置，默认关闭 OAuth relay / 刷新锁）：
* ``GATEWAY_OAUTH_RELAY_ENABLED``：OAuth 发起改走网关 ``/instance/oauth/states``，
  回调由网关校验后转到实例 callback_url，再由租户回调 ``/instance/oauth/resolve`` 换上下文；
* ``GATEWAY_OAUTH_RELAY_LEGACY_FALLBACK_ENABLED``：网关解析失败时回退租户本地签名 state；
* ``GATEWAY_REFRESH_LOCK_ENABLED``：令牌刷新使用网关 ``/instance/refresh-locks`` 互斥；
* ``GATEWAY_REFRESH_LOCK_LEGACY_FALLBACK_ENABLED``：网关锁不可用时回退本地 Redis 锁；
* ``GATEWAY_PUBLIC_URL``：浏览器可达的网关外部地址（用于 OAuth callback URL，缺省 gateway_base_url）。

```text
start:  tenant open link -> gateway create state -> channel authorize URL (gateway callback)
channel: -> gateway callback (consume state) -> redirect instance callback_url?code&state
tenant:  callback -> gateway resolve state (one-time) -> existing persist_result
```
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.modules.gateway.client import GatewayCallError, GatewayClient

logger = logging.getLogger("ventasync.channels.gateway_relay")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _instance_ready() -> bool:
    return bool(
        settings.gateway_channel_config
        and settings.gateway_base_url
        and settings.gateway_instance_id
        and settings.gateway_instance_token
    )


def relay_enabled() -> bool:
    return _instance_ready() and _env_flag("GATEWAY_OAUTH_RELAY_ENABLED", False)


def oauth_legacy_fallback_enabled() -> bool:
    return _env_flag("GATEWAY_OAUTH_RELAY_LEGACY_FALLBACK_ENABLED", True)


def refresh_lock_enabled() -> bool:
    return _instance_ready() and _env_flag("GATEWAY_REFRESH_LOCK_ENABLED", False)


def refresh_lock_fallback_enabled() -> bool:
    return _env_flag("GATEWAY_REFRESH_LOCK_LEGACY_FALLBACK_ENABLED", True)


def public_base_url() -> str:
    return (os.environ.get("GATEWAY_PUBLIC_URL") or settings.gateway_base_url or "").rstrip("/")


def gateway_callback_url(channel: str) -> str:
    return f"{public_base_url()}/api/v1/gateway/oauth/{channel}/callback"


def _client() -> GatewayClient:
    return GatewayClient(settings.gateway_base_url, timeout=settings.gateway_timeout_seconds)


def _gateway_error(exc: GatewayCallError, action: str) -> AppError:
    logger.warning("网关%s失败：status=%s code=%s message=%s", action, exc.status_code, exc.code, exc)
    return AppError(ErrorCode.SERVICE_UNAVAILABLE, f"网关{action}失败，请稍后重试")


def create_oauth_state(row) -> dict[str, Any]:
    """为长期授权链接创建网关一次性 state（绑定实例/租户/店铺/渠道/环境）。"""
    context = dict(getattr(row, "context", None) or {})
    allowed = ("tenant_id", "member_id", "environment", "redirect_path", "store_id", "store_code", "channel")
    gateway_context = {key: context[key] for key in allowed if key in context}
    gateway_context["authorization_link_id"] = int(row.id)
    # 补齐回调需要的租户字段（长期链接 context 可能缺少 channel）
    gateway_context.setdefault("tenant_id", int(row.tenant_id))
    gateway_context.setdefault("channel", str(row.channel))
    client = _client()
    try:
        response = client.create_oauth_state(
            instance_id=settings.gateway_instance_id,
            instance_token=settings.gateway_instance_token,
            channel=row.channel,
            environment=str(context.get("environment") or "production"),
            store_id=int(row.store_id or 0),
            redirect_path=str(context.get("redirect_path") or ""),
            context=gateway_context,
        )
    except GatewayCallError as exc:
        raise _gateway_error(exc, "创建 OAuth state") from exc
    finally:
        client.close()
    return response


def resolve_oauth_state(state: str) -> dict[str, Any]:
    """回调完成后用网关 state 一次性换取绑定上下文；失败抛 GatewayCallError（由调用方决定回退）。"""
    client = _client()
    try:
        return client.resolve_oauth_state(
            instance_id=settings.gateway_instance_id,
            instance_token=settings.gateway_instance_token,
            state=state,
        )
    finally:
        client.close()


class GatewayRefreshLock:
    """网关刷新锁句柄；释放时关闭客户端。"""

    def __init__(self, *, client: GatewayClient, lock_key: str, owner: str) -> None:
        self.client = client
        self.lock_key = lock_key
        self.owner = owner

    def release(self) -> None:
        try:
            self.client.release_refresh_lock(
                instance_id=settings.gateway_instance_id,
                instance_token=settings.gateway_instance_token,
                lock_key=self.lock_key,
                owner=self.owner,
            )
        except GatewayCallError as exc:  # 释放失败不影响业务结果，锁会按 TTL 过期
            logger.warning("释放网关刷新锁失败：key=%s status=%s", self.lock_key, exc.status_code)
        finally:
            self.client.close()


def acquire_refresh_lock(
    *, tenant_id: int, store_id: int, channel: str, ttl_seconds: int
) -> GatewayRefreshLock | str | None:
    """返回锁句柄 / ``"busy"``（409，其他执行者持有）/ ``None``（未启用或网关异常已回退）。

    网关异常且 legacy fallback 开启时返回 ``None``，调用方沿用本地 Redis 锁；
    关闭回退则抛 503。
    """
    if not refresh_lock_enabled():
        return None
    lock_key = f"token:{channel}:{int(tenant_id)}:{int(store_id)}"
    owner = secrets.token_urlsafe(18)
    client = _client()
    try:
        client.acquire_refresh_lock(
            instance_id=settings.gateway_instance_id,
            instance_token=settings.gateway_instance_token,
            lock_key=lock_key,
            owner=owner,
            ttl_seconds=ttl_seconds,
        )
    except GatewayCallError as exc:
        client.close()
        if exc.status_code == 409:
            return "busy"
        if refresh_lock_fallback_enabled():
            logger.warning(
                "网关刷新锁不可用，回退本地锁：status=%s code=%s message=%s",
                exc.status_code,
                exc.code,
                exc,
            )
            return None
        raise _gateway_error(exc, "获取刷新锁") from exc
    except Exception:
        client.close()
        raise
    return GatewayRefreshLock(client=client, lock_key=lock_key, owner=owner)


__all__ = [
    "GatewayRefreshLock",
    "acquire_refresh_lock",
    "create_oauth_state",
    "gateway_callback_url",
    "oauth_legacy_fallback_enabled",
    "public_base_url",
    "refresh_lock_enabled",
    "refresh_lock_fallback_enabled",
    "relay_enabled",
    "resolve_oauth_state",
]
