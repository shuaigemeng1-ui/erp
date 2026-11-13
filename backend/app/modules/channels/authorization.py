"""长期授权邀请与一次性 OAuth state 分离；不接受旧的无消费记录签名 state。

批次4：配置 ``GATEWAY_OAUTH_RELAY_ENABLED`` 时，发起改走独立网关一次性 state，
回调由网关校验后转实例 callback_url，租户回调再用 ``/instance/oauth/resolve`` 换取上下文；
网关失败可按 legacy fallback 开关回退本地签名 state（默认开启，便于回滚）。
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.redis import cache_redis
from app.core.security import random_token, sha256_hex, utc_now, encrypt_secret, decrypt_secret
from app.modules.channels.models_center import AuthorizationLink
from app.modules.channels.state import build_signed_state, parse_signed_state

logger = logging.getLogger("ventasync.channels.authorization")
TTL = 600


def issue_link(center, *, channel: str, payload: dict) -> dict:
    # 串行化同租户链接轮换；旧链接（以及由它签发的 state）立即失效。
    from app.modules.identity.models_center import Tenant
    center.execute(select(Tenant).where(Tenant.id == int(payload["tenant_id"])).with_for_update()).scalar_one()
    rows = center.execute(select(AuthorizationLink).where(
        AuthorizationLink.tenant_id == int(payload["tenant_id"]),
        AuthorizationLink.channel == channel,
        AuthorizationLink.store_id == payload.get("store_id"),
        AuthorizationLink.status.in_(["active", "pending_save"]),
    ).with_for_update().execution_options(populate_existing=True)).scalars()
    for row in rows:
        if row.status == "pending_save":
            raise AppError(ErrorCode.CONFLICT, "上次授权正在保存，请先通过原链接恢复")
        row.status = "revoked"
    from app.modules.channels.service import require_channel_app
    config = require_channel_app(center, payload["environment"], channel=channel)
    payload = {**payload, "application_id": str(config.app_key if channel == "tiktok" else config.partner_id)}
    token = random_token()
    row = AuthorizationLink(tenant_id=int(payload["tenant_id"]), member_id=int(payload["member_id"]),
        store_id=payload.get("store_id"), channel=channel, token_hash=sha256_hex(token),
        status="active", context=dict(payload))
    center.add(row)
    center.commit()
    url = f"{settings.app_origin.rstrip('/')}{settings.api_prefix}/integrations/authorization/{token}"
    return {"auth_url": url, "expires_at": None, "environment": payload["environment"],
            "store_id": payload.get("store_id"), "store_code": payload.get("store_code", "")}


def validate_link(center, link_id: int):
    from app.modules.identity.models_center import TenantMembership, Tenant, User
    from app.modules.identity.service import load_tenant_db_route
    from app.modules.access.service import load_access_snapshot
    from app.db.session import tenant_session
    row = center.get(AuthorizationLink, link_id)
    if row is None or row.status not in {"active", "pending_save"}:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权链接已失效")
    tenant = center.get(Tenant, row.tenant_id)
    member = center.get(TenantMembership, row.member_id)
    if tenant is None or tenant.status != "active" or member is None or member.tenant_id != row.tenant_id or member.status != "active":
        raise AppError(ErrorCode.PERMISSION_DENIED, "授权租户或发起成员已停用")
    user = center.get(User, member.user_id)
    if user is None or user.status != "active":
        raise AppError(ErrorCode.PERMISSION_DENIED)
    from app.modules.channels.service import require_channel_app
    config = require_channel_app(center, row.context["environment"], channel=row.channel)
    application_id = str(config.app_key if row.channel == "tiktok" else config.partner_id)
    if row.context.get("application_id") != application_id:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "平台应用已变更，请重新生成授权链接")
    route = load_tenant_db_route(center, row.tenant_id)
    if route is None:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE)
    with tenant_session(route) as db:
        access = load_access_snapshot(db, row.tenant_id, row.member_id)
        from app.modules.tasks.service import scope_covers_target
        if not scope_covers_target(access.effective, "store.connect", row.store_id):
            raise AppError(ErrorCode.PERMISSION_DENIED)
        if row.store_id is not None:
            from app.modules.channels.service import get_store_or_raise
            store = get_store_or_raise(db, row.tenant_id, row.store_id)
            if store.channel != row.channel or store.status == "disconnected":
                raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "目标店铺已失效")
    return row, route


def create_oauth_state(row) -> str:
    nonce = random_token()
    payload = {**row.context, "channel": row.channel, "authorization_link_id": row.id,
               "nonce": nonce, "expires_at": (utc_now() + timedelta(seconds=TTL)).isoformat()}
    token = build_signed_state(payload)
    try:
        cache_redis().set(f"vs:oauth:nonce:{sha256_hex(nonce)}", sha256_hex(token), ex=TTL)
    except Exception as exc:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE) from exc
    return token


def consume_state(state: str | None, channel: str) -> dict:
    """消费 OAuth state：优先网关 relay（一次性 state），失败按开关回退本地签名 state。"""
    from app.modules.channels import gateway_relay
    from app.modules.gateway.client import GatewayCallError

    if gateway_relay.relay_enabled() and state:
        try:
            resolved = gateway_relay.resolve_oauth_state(state)
            context = dict(resolved.get("context") or {})
            if str(resolved.get("channel") or "") != channel:
                raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权 state 与渠道不匹配")
            # 以网关绑定字段为准覆盖租户上下文，避免上下文被伪造。
            context["tenant_id"] = int(resolved["tenant_id"])
            context["channel"] = str(resolved["channel"])
            context["environment"] = str(resolved["environment"])
            store_id = int(resolved.get("store_id") or 0)
            if store_id > 0:
                context["store_id"] = store_id
            elif context.get("store_id") in (0, "0", ""):
                context["store_id"] = None
            if resolved.get("redirect_path"):
                context["redirect_path"] = str(resolved["redirect_path"])
            if not context.get("authorization_link_id"):
                raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "网关授权上下文缺少授权链接")
            context["authorization_link_id"] = int(context["authorization_link_id"])
            return context
        except (GatewayCallError, AppError) as exc:
            if not gateway_relay.oauth_legacy_fallback_enabled():
                logger.warning("网关 OAuth state 解析失败且 legacy fallback 关闭：%s", exc)
                raise AppError(
                    ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话已失效，请重新打开授权链接"
                ) from exc
            logger.warning("网关 OAuth state 解析失败，回退本地 state：%s", exc)
    payload = parse_signed_state(state)
    try:
        from datetime import datetime
        valid = payload["channel"] == channel and int(payload["authorization_link_id"]) > 0 and datetime.fromisoformat(payload["expires_at"]) > utc_now()
        nonce = payload["nonce"]
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话已失效，请重新打开授权链接")
    try:
        stored = cache_redis().getdel(f"vs:oauth:nonce:{sha256_hex(nonce)}")
    except Exception as exc:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE) from exc
    if isinstance(stored, bytes):
        stored = stored.decode()
    if stored != sha256_hex(state):
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权会话已消费或过期")
    return payload


def stage_result(center, payload: dict, result: dict) -> None:
    row = center.execute(select(AuthorizationLink).where(AuthorizationLink.id == int(payload["authorization_link_id"]))
                         .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None or row.status != "active":
        raise AppError(ErrorCode.CONFLICT, "授权链接已撤销或有待恢复结果")
    row, route = validate_link(center, row.id)
    from app.db.session import tenant_session
    from app.modules.channels import service
    external_id = str(result.get("shop_id") or (result.get("shop") or {}).get("id") or "")
    with tenant_session(route) as db:
        if row.store_id:
            store = service.get_store_or_raise(db, row.tenant_id, row.store_id)
            if store.status != "pending_authorization" and store.external_store_id != external_id:
                raise AppError(ErrorCode.CONFLICT, "已连接店铺不能更换平台身份")
    registry_id = service.claim_store_registry(center, channel=row.channel,
        environment=row.context["environment"], external_account_id=external_id,
        tenant_id=row.tenant_id, store_id=row.store_id or 0)
    from app.modules.channels.models_center import ChannelAccountRegistry
    center.get(ChannelAccountRegistry, registry_id).status = "reserved"
    # 明文凭据只在内存中，中心库存密文供租户库失败后恢复。
    row.result_cipher = encrypt_secret(json.dumps(result, ensure_ascii=False))
    row.status = "pending_save"
    center.commit()


def finish_result(center, link_id: int) -> None:
    row = center.get(AuthorizationLink, link_id)
    row.status = "active"
    row.result_cipher = None
    center.commit()


def saved_result(row):
    return json.loads(decrypt_secret(row.result_cipher)) if row.result_cipher else None


def persist_result(payload: dict, result: dict | None = None) -> dict:
    """中心库先保存可恢复结果与外部身份预留，再更新租户库；失败可从长期链接重试。"""
    from app.db.session import center_session, tenant_session
    from app.modules.channels.shopee import oauth as shopee
    from app.modules.channels.tiktok import oauth as tiktok
    link_id = int(payload["authorization_link_id"])
    with center_session() as center:
        row, route = validate_link(center, link_id)
        if result is not None:
            stage_result(center, payload, result)
        result = saved_result(row)
        if result is None:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "缺少授权结果")
        with tenant_session(route) as db:
            if row.channel == "shopee":
                output = shopee.complete_authorization(center, db, session=payload,
                    shop_id=result["shop_id"], token_data=result["token"], profile=result.get("profile"))
            else:
                output = tiktok.complete_authorization(center, db, session=payload,
                    token=result["token"], shop=result["shop"], warehouse_id=result.get("warehouse_id", ""))
        finish_result(center, link_id)
        return output


from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from html import escape

router = APIRouter(prefix="/integrations", tags=["authorization"])


@router.get("/authorization/{token}", include_in_schema=False)
def open_authorization_link(token: str):
    from app.db.session import center_session
    from app.modules.channels import service
    from app.modules.channels.shopee.signature import append_query_params, build_authorize_url
    from app.modules.channels.tiktok.oauth import build_authorization_url
    with center_session() as center:
        row = center.execute(select(AuthorizationLink).where(AuthorizationLink.token_hash == sha256_hex(token))).scalar_one_or_none()
        if row is None:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权链接已失效")
        row, _ = validate_link(center, row.id)
        payload = {**row.context, "authorization_link_id": row.id}
        if row.status == "pending_save":
            output = persist_result(payload)
            return RedirectResponse(f"{settings.app_origin.rstrip('/')}{output['redirect_path']}", status_code=302)
        config = service.require_channel_app(center, row.context["environment"], channel=row.channel)
        if row.context.get("application_id") != str(config.app_key if row.channel == "tiktok" else config.partner_id):
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "平台应用已变更，请重新生成授权链接")
        from app.modules.channels import gateway_relay
        from app.modules.gateway.client import GatewayCallError

        gateway_state: str | None = None
        if gateway_relay.relay_enabled():
            try:
                gateway_info = gateway_relay.create_oauth_state(row)
                gateway_state = str(gateway_info.get("state") or "")
                if not gateway_state:
                    raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "网关未返回授权 state")
            except (GatewayCallError, AppError) as exc:
                if not gateway_relay.oauth_legacy_fallback_enabled():
                    logger.warning("创建网关 OAuth state 失败且 legacy fallback 关闭：%s", exc)
                    raise
                logger.warning("创建网关 OAuth state 失败，回退本地 state：%s", exc)
                gateway_state = None
        state = gateway_state or create_oauth_state(row)
        if row.channel == "tiktok":
            url = build_authorization_url(service_id=config.app_id, state=state, market=config.region or "ROW")
        else:
            from app.modules.channels.shopee.client import resolve_base_url
            callback_base = (
                gateway_relay.gateway_callback_url(row.channel)
                if gateway_state
                else config.redirect_url
            )
            if not callback_base:
                raise AppError(ErrorCode.CHANNEL_APP_NOT_CONFIGURED, "平台未配置可用的 OAuth 回调地址")
            url = build_authorize_url(auth_base=resolve_base_url(row.context["environment"]),
                partner_id=int(config.partner_id), partner_key=config.partner_key,
                redirect_url=append_query_params(callback_base, {"state": state}))
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def selection_response(payload: dict, result: dict):
    """多店铺授权以短期加密候选结果供店主选择，不在页面输出 token。"""
    token = random_token()
    cache_redis().set(f"vs:oauth:selection:{sha256_hex(token)}",
        encrypt_secret(json.dumps({"session": payload, "result": result})), ex=TTL)
    choices = "".join(f'<button type="submit" name="shop_id" value="{escape(str(shop["id"]), quote=True)}">{escape(str(shop.get("name") or shop["id"]))}</button><br>' for shop in result["shops"])
    action = f"{settings.api_prefix}/integrations/authorization-selection"
    # GET 表单仅消费高熵一次性选择票据，无用户会话权限副作用。
    return HTMLResponse(f'<!doctype html><meta charset="utf-8"><title>选择授权店铺</title><h1>选择要连接的店铺</h1><form method="get" action="{action}"><input type="hidden" name="token" value="{token}">{choices}</form>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'"})


# 使用独立固定路径避免与 /authorization/{token} 冲突。
@router.get("/authorization-selection", include_in_schema=False)
def choose_authorized_shop(token: str = Query(min_length=20, max_length=256), shop_id: str = Query(max_length=64)):
    key = f"vs:oauth:selection:{sha256_hex(token)}"
    raw = cache_redis().getdel(key)
    if not raw:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "选择会话已过期或已使用")
    if isinstance(raw, bytes):
        raw = raw.decode()
    stored = json.loads(decrypt_secret(raw))
    result = stored["result"]
    shop = next((s for s in result["shops"] if str(s["id"]) == shop_id), None)
    if shop is None:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "请选择已授权的店铺")
    output = persist_result(stored["session"], {"token": result["token"], "shop": shop})
    return RedirectResponse(f"{settings.app_origin.rstrip('/')}{output['redirect_path']}", status_code=302,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
