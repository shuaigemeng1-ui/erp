"""TikTok Shop 店铺授权（OAuth）流程。

* 发起授权：平台统一配置 TikTok App Key/Secret，租户只做店铺授权；
* 长期链接可撤销；网关签发绑定租户 / 店铺 / 渠道的 600 秒一次性 state；
* 回调不依赖用户会话：校验 state 后换取 token，再读取授权店铺（含 shop_cipher）；
* 店铺和加密凭据写入租户库，中心库登记店铺归属，写租户审计；
* 外部 HTTP 请求在数据库事务之外执行，避免长时间持有事务。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.redis import cache_redis
from app.core.security import utc_after, utc_now
from app.modules.channels.constants import (
    CHANNEL_TIKTOK,
    normalize_environment,
    resolve_environment,
)
from app.modules.channels.shopee.signature import append_query_params, safe_redirect_path
from app.modules.channels.state import build_signed_state, parse_signed_state
from app.modules.channels.tiktok.client import (
    TikTokApiError,
    TikTokClient,
    normalize_token_payload,
    token_expiry_times,
)

logger = logging.getLogger("ventasync.channels.tiktok.oauth")

AUTHORIZATION_BASE_URLS = {
    "ROW": "https://services.tiktokshop.com/open/authorize",
    "US": "https://services.us.tiktokshop.com/open/authorize",
}

OAUTH_SESSION_PREFIX = "vs:tiktok:oauth"
OAUTH_SESSION_TTL_SECONDS = 600
STATE_NONCE_PATTERN = re.compile(
    r"^t(?P<tenant_id>[1-9][0-9]{0,18})\.(?P<nonce>[A-Za-z0-9_-]{16,96})$"
)


# --------------------------------------------------------------------------- state / session
def build_state(tenant_id: int) -> str:
    if int(tenant_id) <= 0:
        raise ValueError("tenant_id 必须为正整数")
    return f"t{int(tenant_id)}.{secrets.token_urlsafe(24)}"


def parse_state(state: str | None) -> tuple[int, str]:
    match = STATE_NONCE_PATTERN.match((state or "").strip())
    if not match:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话无效或已过期")
    return int(match.group("tenant_id")), match.group("nonce")


def authorization_session_key(tenant_id: int, nonce: str) -> str:
    return f"{OAUTH_SESSION_PREFIX}:{int(tenant_id)}:{nonce}"


def build_authorization_url(
    *, service_id: str, state: str, market: str = "ROW"
) -> str:
    """TikTok Shop 卖家授权链接：Service ID + 短期一次性 state。"""
    normalized_service_id = (service_id or "").strip()
    normalized_market = (market or "ROW").strip().upper()
    if not normalized_service_id:
        raise AppError(ErrorCode.CHANNEL_APP_NOT_CONFIGURED, "平台未配置 TikTok App ID（Service ID）")
    if normalized_market not in AUTHORIZATION_BASE_URLS:
        raise AppError(ErrorCode.VALIDATION_ERROR, "TikTok 授权市场只能是 ROW 或 US")
    query = urlencode({"service_id": normalized_service_id, "state": state})
    return f"{AUTHORIZATION_BASE_URLS[normalized_market]}?{query}"


def create_authorization_session(
    *,
    tenant_id: int,
    member_id: int,
    environment: str,
    redirect_path: str,
    service_id: str,
    market: str = "ROW",
    store_id: int | None = None,
    store_code: str = "",
    ttl_seconds: int = OAUTH_SESSION_TTL_SECONDS,
) -> dict[str, Any]:
    env = normalize_environment(environment) or resolve_environment(settings.app_env)
    tenant_id_int = int(tenant_id)
    payload = {
        "tenant_id": tenant_id_int,
        "member_id": int(member_id),
        "environment": env,
        "redirect_path": safe_redirect_path(redirect_path, f"/tenants/{tenant_id_int}/stores"),
        "store_id": int(store_id) if store_id else None,
        "store_code": (store_code or "").strip(),
        "service_id": (service_id or "").strip(),
        "market": (market or "ROW").strip().upper(),
        "created_at": utc_now().isoformat(),
        "expires_at": utc_after(ttl_seconds).isoformat(),
        "nonce": secrets.token_urlsafe(18),
    }
    state = build_signed_state(payload)
    auth_url = build_authorization_url(
        service_id=payload["service_id"],
        state=state,
        market=payload["market"],
    )
    return {
        "auth_url": auth_url,
        "expires_at": payload["expires_at"],
        "environment": env,
        "state": state,
        "store_id": payload["store_id"],
        "store_code": payload["store_code"],
        "service_id": payload["service_id"],
        "market": payload["market"],
    }


def validate_authorization_session(
    payload: Mapping[str, Any] | None,
    *,
    expected_tenant_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话无效或已过期")
    tenant_id = int(payload.get("tenant_id") or 0)
    member_id = int(payload.get("member_id") or 0)
    environment = normalize_environment(str(payload.get("environment") or ""))
    if tenant_id <= 0 or member_id <= 0 or environment is None:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话内容不完整")
    if expected_tenant_id is not None and tenant_id != int(expected_tenant_id):
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话与租户不匹配")
    expires_raw = str(payload.get("expires_at") or "")
    if expires_raw:
        try:
            expires_at = datetime.fromisoformat(expires_raw)
        except ValueError as exc:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话已损坏") from exc
        current = now or utc_now()
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        if expires_at <= current:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话已过期，请重新发起授权")
    raw_store_id = payload.get("store_id")
    store_id = int(raw_store_id) if raw_store_id not in (None, "", 0, "0") else None
    return {
        "tenant_id": tenant_id,
        "member_id": member_id,
        "environment": environment,
        "redirect_path": safe_redirect_path(
            str(payload.get("redirect_path") or ""), f"/tenants/{tenant_id}/stores"
        ),
        "store_id": store_id,
        "store_code": str(payload.get("store_code") or ""),
        "service_id": str(payload.get("service_id") or ""),
        "market": str(payload.get("market") or "ROW"),
    }


def _consume_legacy_session(state: str | None) -> dict[str, Any]:
    tenant_id, nonce = parse_state(state)
    key = authorization_session_key(tenant_id, nonce)
    try:
        client = cache_redis()
        raw = client.getdel(key) if hasattr(client, "getdel") else client.get(key)
        if raw is None:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话无效或已过期")
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取 TikTok 授权会话失败")
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "缓存服务不可用") from exc
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话已损坏") from exc
    return validate_authorization_session(payload, expected_tenant_id=tenant_id)


def consume_authorization_session(state: str | None) -> dict[str, Any]:
    from app.modules.channels.authorization import consume_state
    payload = consume_state(state, "tiktok")
    return validate_authorization_session(payload) | {
        "authorization_link_id": int(payload["authorization_link_id"])
    }


# --------------------------------------------------------------------------- 发起授权
def start_authorization(
    center,
    db,
    *,
    tenant_id: int,
    member_id: int,
    store_id: int | None = None,
    environment: str | None = None,
    redirect_path: str | None = None,
    actor_id: int | None = None,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """发起 TikTok Shop 授权，返回可撤销、可轮换的长期授权邀请链接。

    ``store_id`` 存在时表示「先建店铺后授权」：链接只绑定该店铺，回调更新
    该店铺并置为已连接；不传则兼容旧流程，由回调按平台店铺 ID 创建。
    """
    from app.modules.channels import service as channel_service

    store = None
    if store_id is not None:
        store = channel_service.get_store_or_raise(db, tenant_id, int(store_id))
        if store.channel != CHANNEL_TIKTOK:
            raise AppError(ErrorCode.VALIDATION_ERROR, "该店铺不是 TikTok Shop 渠道")
        if store.status not in {
            channel_service.STORE_STATUS_PENDING_AUTHORIZATION,
            channel_service.STORE_STATUS_REAUTH_REQUIRED,
            channel_service.STORE_STATUS_CONNECTED,
        }:
            raise AppError(ErrorCode.STORE_NOT_CONNECTED, "当前店铺状态不允许发起授权")
        env = normalize_environment(environment) or store.environment
    else:
        env = normalize_environment(environment) or resolve_environment(settings.app_env)
    config = channel_service.require_channel_app(center, env, channel=CHANNEL_TIKTOK)
    if not config.app_id:
        raise AppError(ErrorCode.CHANNEL_APP_NOT_CONFIGURED, "平台未配置 TikTok App ID（Service ID）")
    market = (config.region or "ROW").strip().upper()
    if market not in {"ROW", "US"}:
        market = "ROW"
    session = create_authorization_session(
        tenant_id=tenant_id,
        member_id=member_id,
        environment=env,
        redirect_path=safe_redirect_path(redirect_path, f"/tenants/{tenant_id}/stores"),
        service_id=config.app_id,
        market=market,
        store_id=store.id if store is not None else None,
        store_code=store.store_code if store is not None else "",
    )
    from app.modules.channels.authorization import issue_link
    session = issue_link(center, channel=CHANNEL_TIKTOK, payload=parse_signed_state(session["state"]))
    channel_service.record_audit(
        db,
        tenant_id=tenant_id,
        action="store.authorize.started",
        resource_type="store",
        actor_id=actor_id,
        summary=f"发起 TikTok Shop 店铺授权（{env}）",
        payload={
            "channel": CHANNEL_TIKTOK,
            "environment": env,
            "store_id": session.get("store_id"),
            "store_code": session.get("store_code"),
            "service_id": session.get("service_id"),
            "market": session.get("market"),
        },
        request_id=request_id,
        ip=ip,
    )
    db.commit()
    return session


# --------------------------------------------------------------------------- 回调
def exchange_code(app_key: str, app_secret: str, code: str) -> dict[str, Any]:
    """用授权码换 token，并读取授权店铺（含 shop_cipher）。"""
    client = TikTokClient(app_key=app_key, app_secret=app_secret)
    try:
        raw = client.get_token(code)
        token = normalize_token_payload(raw)
        if not token["access_token"]:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "平台未返回访问令牌")
        client.access_token = token["access_token"]
        try:
            shops = client.get_shops()
        except TikTokApiError as exc:
            raise AppError(
                ErrorCode.CHANNEL_CALLBACK_UNKNOWN,
                f"读取 TikTok 店铺失败：{exc.message}"[:200],
            ) from exc
        if not shops:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "TikTok 未返回可用的店铺")
        if len(shops) > 1:
            return {"token": token, "shops": shops}
        shop = shops[0]
        client.shop_cipher = str(shop.get("cipher") or token.get("shop_cipher") or "")
        warehouse_id = ""
        try:
            warehouses = client.get_warehouses()
            sales = [item for item in warehouses if str(item.get("type") or "") == "SALES_WAREHOUSE"]
            chosen = (sales or warehouses or [{}])[0]
            warehouse_id = str(chosen.get("id") or "")
        except TikTokApiError:
            logger.warning("读取 TikTok 仓库失败，库存推送将在首次使用时重试")
        return {"token": token, "shop": shop, "warehouse_id": warehouse_id}
    except TikTokApiError as exc:
        raise AppError(
            ErrorCode.CHANNEL_CALLBACK_UNKNOWN,
            f"TikTok 授权失败：{exc.message}"[:200],
        ) from exc
    finally:
        client.close()


def complete_authorization(
    center,
    tenant_db,
    *,
    session: Mapping[str, Any],
    token: Mapping[str, Any],
    shop: Mapping[str, Any],
    warehouse_id: str = "",
) -> dict[str, Any]:
    """写店铺 + 加密凭据 + 中心库归属登记 + 审计。"""
    from app.modules.channels import service as channel_service

    tenant_id = int(session["tenant_id"])
    environment = str(session["environment"])
    external_id = str(shop.get("id") or token.get("shop_id") or "").strip()
    if not external_id:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "TikTok 店铺 ID 为空")
    shop_cipher = str(shop.get("cipher") or token.get("shop_cipher") or "").strip()
    if not shop_cipher:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "TikTok 未返回 shop_cipher")
    name = str(shop.get("name") or token.get("seller_name") or "").strip()
    region = str(shop.get("region") or token.get("region") or "").strip().upper() or None
    access_expires, refresh_expires = token_expiry_times(token)
    try:
        store_id = channel_service.upsert_store_from_authorization(
            tenant_db,
            tenant_id=tenant_id,
            channel=CHANNEL_TIKTOK,
            environment=environment,
            external_store_id=external_id,
            store_name=name or f"TikTok Shop 店铺 {external_id}",
            region=region,
            access_token=str(token.get("access_token") or ""),
            refresh_token=str(token.get("refresh_token") or ""),
            access_token_expires_at=access_expires,
            refresh_token_expires_at=refresh_expires,
            member_id=int(session.get("member_id") or 0) or None,
            shop_cipher=shop_cipher,
            warehouse_id=warehouse_id or None,
            store_id=int(session["store_id"]) if session.get("store_id") else None,
        )
        channel_service.claim_store_registry(
            center,
            channel=CHANNEL_TIKTOK,
            environment=environment,
            external_account_id=external_id,
            tenant_id=tenant_id,
            store_id=store_id,
        )
        channel_service.record_audit(
            tenant_db,
            tenant_id=tenant_id,
            action="store.authorized",
            resource_type="store",
            resource_id=store_id,
            actor_id=int(session.get("member_id") or 0) or None,
            summary=f"TikTok Shop 店铺授权成功（{environment}）",
            payload={
                "channel": CHANNEL_TIKTOK,
                "environment": environment,
                "external_store_id": external_id,
                "store_code": str(session.get("store_code") or ""),
            },
        )
        tenant_db.commit()
        center.commit()
    except AppError:
        tenant_db.rollback()
        center.rollback()
        raise
    except Exception:
        tenant_db.rollback()
        center.rollback()
        logger.exception("保存 TikTok 授权结果失败")
        raise AppError(ErrorCode.INTERNAL_ERROR, "保存授权结果失败") from None
    return {
        "tenant_id": tenant_id,
        "store_id": store_id,
        "environment": environment,
        "external_store_id": external_id,
        "store_code": str(session.get("store_code") or ""),
        "redirect_path": str(
            session.get("redirect_path") or f"/tenants/{tenant_id}/stores"
        ),
    }


# --------------------------------------------------------------------------- 跳转
def frontend_redirect_url(
    redirect_path: str | None,
    *,
    origin: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    base = (origin or settings.app_origin or "").rstrip("/")
    path = safe_redirect_path(redirect_path, "/")
    url = f"{base}{path}" if base else path
    if params:
        url = append_query_params(url, dict(params))
    return url


def callback_error_redirect(*, redirect_path: str | None = None, reason: str = "") -> str:
    return frontend_redirect_url(
        redirect_path or "/stores",
        params={"tiktok": "error", "reason": (reason or "")[:120]},
    )


__all__ = [
    "OAUTH_SESSION_PREFIX",
    "callback_error_redirect",
    "complete_authorization",
    "consume_authorization_session",
    "create_authorization_session",
    "exchange_code",
    "frontend_redirect_url",
    "start_authorization",
    "validate_authorization_session",
]
