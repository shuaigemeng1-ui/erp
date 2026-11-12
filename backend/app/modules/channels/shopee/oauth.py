"""Shopee 店铺授权（OAuth）流程。

设计要点（plan.txt §4「统一应用与店铺授权」）：

* 平台统一配置生产 / 测试应用凭据，租户只执行店铺授权，不接触 Partner Key。
* 发起授权时生成一次性服务端授权会话：Redis 键必须包含 ``tenant_id``
  （``vs:shopee:oauth:{tenant_id}:{nonce}``），会话绑定租户、成员、应用环境与
  回调目标；``state`` 由租户 ID + 随机串组成，回调据此找回会话。
* 回调不依赖用户会话：校验 state（一次性、未过期、租户 / 环境匹配）后换取 Token，
  把加密后的令牌保存到该租户库，并在中心库 ``channel_account_registry`` 登记店铺归属。
* 同一环境同一外部店铺只能归属一个有效租户；重新授权更新原店铺，不重复创建。
* 外部 HTTP 请求在数据库写事务之前完成，避免长时间持有事务。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.redis import cache_redis
from app.core.security import utc_after, utc_now
from app.modules.channels.constants import (
    CHANNEL_SHOPEE,
    REFRESH_TOKEN_TTL_DAYS,
    normalize_environment,
    resolve_environment,
)
from app.modules.channels.shopee.client import (
    ShopeeApiError,
    ShopeeClient,
    resolve_base_url,
    token_expiry,
)
from app.modules.channels.shopee.signature import (
    append_query_params,
    build_authorize_url,
    safe_redirect_path,
)

logger = logging.getLogger("ventasync.channels.oauth")

OAUTH_SESSION_PREFIX = "vs:shopee:oauth"
OAUTH_SESSION_TTL_SECONDS = 600
STATE_NONCE_PATTERN = re.compile(r"^t(?P<tenant_id>[1-9][0-9]{0,18})\.(?P<nonce>[A-Za-z0-9_-]{16,96})$")


# --------------------------------------------------------------------------- state
def new_state_nonce() -> str:
    """生成 state 随机段（高熵，一次性）。"""
    return secrets.token_urlsafe(24)


def build_state(tenant_id: int) -> str:
    """构造 state：``t{tenant_id}.{nonce}``，回调据此定位含租户的 Redis 键。"""
    if int(tenant_id) <= 0:
        raise ValueError("tenant_id 必须为正整数")
    return f"t{int(tenant_id)}.{new_state_nonce()}"


def parse_state(state: str | None) -> tuple[int, str]:
    """解析 state，返回 ``(tenant_id, nonce)``；非法格式抛回调未知错误。"""
    match = STATE_NONCE_PATTERN.match((state or "").strip())
    if not match:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话无效或已过期")
    return int(match.group("tenant_id")), match.group("nonce")


def authorization_session_key(tenant_id: int, nonce: str) -> str:
    """授权会话 Redis 键：键必须包含租户 ID，避免跨租户串会话。"""
    return f"{OAUTH_SESSION_PREFIX}:{int(tenant_id)}:{nonce}"


# --------------------------------------------------------------------------- 会话
def create_authorization_session(
    *,
    tenant_id: int,
    member_id: int,
    environment: str,
    redirect_path: str,
    auth_base: str,
    partner_id: int,
    partner_key: str,
    redirect_url: str,
    store_id: int | None = None,
    store_code: str = "",
    ttl_seconds: int = OAUTH_SESSION_TTL_SECONDS,
) -> dict[str, Any]:
    """创建一次性授权会话并返回 ``{auth_url, expires_at, environment}``。"""
    env = normalize_environment(environment) or resolve_environment(settings.app_env)
    state = build_state(tenant_id)
    tenant_id_int, nonce = parse_state(state)
    expires_at = utc_after(max(60, int(ttl_seconds)))
    payload = {
        "tenant_id": tenant_id_int,
        "member_id": int(member_id),
        "environment": env,
        "redirect_path": safe_redirect_path(redirect_path, f"/tenants/{tenant_id_int}/stores"),
        "store_id": int(store_id) if store_id else None,
        "store_code": (store_code or "").strip(),
        "created_at": utc_now().isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    callback_url = append_query_params(redirect_url, {"state": state})
    auth_url = build_authorize_url(
        auth_base=auth_base,
        partner_id=partner_id,
        partner_key=partner_key,
        redirect_url=callback_url,
    )
    try:
        cache_redis().setex(
            authorization_session_key(tenant_id_int, nonce),
            max(60, int(ttl_seconds)),
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:  # noqa: BLE001 - 缓存不可用统一转 503
        logger.exception("保存 Shopee 授权会话失败")
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "缓存服务不可用") from exc
    return {"auth_url": auth_url, "expires_at": expires_at, "environment": env, "state": state}


def validate_authorization_session(
    payload: Mapping[str, Any] | None,
    *,
    expected_tenant_id: int | None = None,
    expected_environment: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """校验授权会话内容（纯逻辑，便于单元测试）。

    校验项：租户一致、环境合法且匹配、成员存在、回调路径为站内相对路径、未过期。
    """
    if not isinstance(payload, Mapping):
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话无效或已过期")
    tenant_id = int(payload.get("tenant_id") or 0)
    member_id = int(payload.get("member_id") or 0)
    environment = normalize_environment(str(payload.get("environment") or ""))
    if tenant_id <= 0 or member_id <= 0 or environment is None:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话内容不完整")
    if expected_tenant_id is not None and tenant_id != int(expected_tenant_id):
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话与租户不匹配")
    if expected_environment is not None and environment != normalize_environment(expected_environment):
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话与应用环境不匹配")
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
    redirect_path = safe_redirect_path(
        str(payload.get("redirect_path") or ""), f"/tenants/{tenant_id}/stores"
    )
    raw_store_id = payload.get("store_id")
    store_id = int(raw_store_id) if raw_store_id not in (None, "", 0, "0") else None
    return {
        "tenant_id": tenant_id,
        "member_id": member_id,
        "environment": environment,
        "redirect_path": redirect_path,
        "store_id": store_id,
        "store_code": str(payload.get("store_code") or ""),
    }


def consume_authorization_session(state: str | None) -> dict[str, Any]:
    """读取并立即删除一次性授权会话（Redis TTL 之外再保证一次有效）。"""
    tenant_id, nonce = parse_state(state)
    key = authorization_session_key(tenant_id, nonce)
    try:
        client = cache_redis()
        # Redis >= 6.2 支持 GETDEL，保证「读取即删除」的一次性语义
        if hasattr(client, "getdel"):
            raw = client.getdel(key)
        else:
            raw = client.get(key)
            if raw:
                client.delete(key)
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取 Shopee 授权会话失败")
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "缓存服务不可用") from exc
    if not raw:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话无效或已过期")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话已损坏") from exc
    return validate_authorization_session(payload, expected_tenant_id=tenant_id)


# --------------------------------------------------------------------------- 回调
def validate_callback_params(code: str | None, shop_id: str | None) -> tuple[str, str]:
    """校验 Shopee 回调参数，返回 ``(code, shop_id)``。"""
    code_value = (code or "").strip()
    shop_value = (shop_id or "").strip()
    if not code_value or not shop_value:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "缺少 code 或 shop_id")
    if not shop_value.isdigit():
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "shop_id 格式不正确")
    return code_value, shop_value


def normalize_token_payload(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """把 Shopee token 响应归一化（兼容 ``response`` 包装）。"""
    payload: Mapping[str, Any] = raw or {}
    nested = payload.get("response")
    if isinstance(nested, Mapping):
        payload = nested
    access_token = str(payload.get("access_token") or "").strip()
    refresh_token = str(payload.get("refresh_token") or "").strip()
    expire_in = _to_int(payload.get("expire_in"))
    refresh_expire_in = _to_int(payload.get("refresh_token_expire_in") or payload.get("refresh_expire_in"))
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expire_in": expire_in,
        "refresh_token_expire_in": refresh_expire_in,
    }


def _to_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def token_expiry_times(
    token: Mapping[str, Any], now: datetime | None = None
) -> tuple[datetime | None, datetime | None]:
    """由 token 响应的 ``expire_in`` 推导 access / refresh 过期时间（UTC naive）。"""
    current = now or utc_now()
    access_expires = token_expiry(current, token.get("expire_in"))
    refresh_seconds = _to_int(token.get("refresh_token_expire_in"))
    if refresh_seconds > 0:
        refresh_expires: datetime | None = current + timedelta(seconds=refresh_seconds)
    else:
        refresh_expires = current + timedelta(days=REFRESH_TOKEN_TTL_DAYS)
    return access_expires, refresh_expires


def build_store_display_name(shop_id: str, profile: Mapping[str, Any] | None) -> str:
    """店铺展示名：优先平台返回的店铺名，缺失时给可读兜底。"""
    data = profile or {}
    for key in ("shop_name", "shop_name_local", "name"):
        value = str(data.get(key) or "").strip()
        if value:
            return value[:128]
    return f"Shopee 店铺 {shop_id}"


def build_store_region(profile: Mapping[str, Any] | None) -> str | None:
    """从平台店铺信息提取站点区域。"""
    data = profile or {}
    region = str(data.get("region") or data.get("shop_region") or "").strip().upper()
    return region[:16] if region else None


def exchange_authorization_code(
    *,
    environment: str,
    partner_id: int,
    partner_key: str,
    code: str,
    shop_id: str,
    access_token: str = "",
) -> dict[str, Any]:
    """用授权码换取 Token，并尽量读取店铺资料（失败不影响授权结果）。"""
    client = ShopeeClient(
        partner_id=partner_id,
        partner_key=partner_key,
        access_token=access_token,
        shop_id=int(shop_id),
        base_url=resolve_base_url(environment),
    )
    try:
        raw = client.request_access_token(code, int(shop_id))
        token = normalize_token_payload(raw)
        profile: dict[str, Any] = {}
        if token["access_token"]:
            client.access_token = token["access_token"]
            try:
                profile = client.get_shop_info()
            except ShopeeApiError as exc:  # 店铺资料只用于展示，不阻塞授权
                logger.warning("读取 Shopee 店铺资料失败（忽略）：%s", exc)
        return {"token": token, "profile": profile}
    finally:
        client.close()


def complete_authorization(
    center,
    tenant_db,
    *,
    session: Mapping[str, Any],
    shop_id: str,
    token_data: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把授权结果写入租户库与中心库（由调用方控制两个 Session）。

    * 租户库：新建店铺或更新原店铺 + 加密令牌；
    * 中心库：登记外部店铺归属（冲突抛 ``STORE_ALREADY_BOUND``）；
    * 事务：先写租户库并 flush，再登记归属，最后分别 commit；任一步失败全部回滚。
    """
    from app.modules.channels import service as channel_service

    tenant_id = int(session["tenant_id"])
    environment = str(session["environment"])
    token = normalize_token_payload(token_data)
    if not token["access_token"]:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "平台未返回访问令牌")
    access_expires, refresh_expires = token_expiry_times(token)
    try:
        store_id = channel_service.upsert_store_from_authorization(
            tenant_db,
            tenant_id=tenant_id,
            channel=CHANNEL_SHOPEE,
            environment=environment,
            external_store_id=str(shop_id),
            store_name=build_store_display_name(shop_id, profile),
            region=build_store_region(profile),
            access_token=token["access_token"],
            refresh_token=token["refresh_token"],
            access_token_expires_at=access_expires,
            refresh_token_expires_at=refresh_expires,
            member_id=int(session.get("member_id") or 0) or None,
            store_id=int(session["store_id"]) if session.get("store_id") else None,
        )
        channel_service.claim_store_registry(
            center,
            channel=CHANNEL_SHOPEE,
            environment=environment,
            external_account_id=str(shop_id),
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
            summary=f"Shopee 店铺授权成功（{environment}）",
            payload={
                "channel": CHANNEL_SHOPEE,
                "environment": environment,
                "external_store_id": str(shop_id),
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
        logger.exception("保存 Shopee 授权结果失败")
        raise
    return {
        "tenant_id": tenant_id,
        "store_id": store_id,
        "environment": environment,
        "external_store_id": str(shop_id),
        "redirect_path": str(session.get("redirect_path") or f"/tenants/{tenant_id}/stores"),
    }


# --------------------------------------------------------------------------- 跳转
def frontend_redirect_url(
    redirect_path: str | None,
    *,
    origin: str | None = None,
    params: Mapping[str, Any] | None = None,
) -> str:
    """构造前端跳转地址：只允许站内路径，禁止开放重定向。"""
    base = (origin or settings.app_origin or "").rstrip("/")
    path = safe_redirect_path(redirect_path, "/")
    url = f"{base}{path}" if base else path
    if params:
        url = append_query_params(url, dict(params))
    return url


def callback_error_redirect(*, redirect_path: str | None = None, reason: str = "") -> str:
    """授权失败时跳转前端店铺页并带错误码（不暴露内部细节）。"""
    return frontend_redirect_url(
        redirect_path or "/stores",
        params={"shopee": "error", "reason": (reason or "")[:120]},
    )


__all__ = [
    "OAUTH_SESSION_PREFIX",
    "OAUTH_SESSION_TTL_SECONDS",
    "authorization_session_key",
    "build_state",
    "callback_error_redirect",
    "complete_authorization",
    "consume_authorization_session",
    "create_authorization_session",
    "exchange_authorization_code",
    "frontend_redirect_url",
    "new_state_nonce",
    "normalize_token_payload",
    "parse_state",
    "token_expiry_times",
    "validate_authorization_session",
    "validate_callback_params",
]
