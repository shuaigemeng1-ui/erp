"""店铺与渠道路由。

包含两组路由（见 docs/api-contract.md）：

* 租户内（需要用户会话 + 租户上下文 + 权限）::

    GET  /tenants/{tenant_id}/stores                    店铺列表
    POST /tenants/{tenant_id}/stores/shopee/authorize   发起授权（返回 auth_url）
    POST /tenants/{tenant_id}/stores/{store_id}/disconnect  断开授权
    GET  /tenants/{tenant_id}/listings                  平台商品与映射状态
    POST /tenants/{tenant_id}/listings/sync             同步平台商品（202 + task_id）
    POST /tenants/{tenant_id}/listings/{listing_id}/link    人工映射
    POST /tenants/{tenant_id}/listings/{listing_id}/unlink  解除映射

* 平台回调（不使用用户会话，只信一次性授权会话 / 签名）::

    GET  /integrations/shopee/oauth/callback
    POST /integrations/shopee/webhook

实现负责人：backend-channels。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    PageParams,
    get_center_db,
    get_tenant_db,
    require_permission,
    tenant_context,
)
from app.core.context import TenantContext
from app.core.errors import AppError, ErrorCode
from app.core.http import accepted, paged, success
from app.core.permissions import Permission
from app.db.session import center_session, tenant_session
from app.modules.access.service import AccessSnapshot
from app.modules.channels import schemas, service
from app.modules.channels.constants import (
    CHANNEL_TIKTOK,
    LINK_STATUSES,
    LISTING_FILTER_UNMAPPED,
    STORE_STATUS_CONNECTED,
    STORE_STATUS_PENDING_AUTHORIZATION,
    STORE_STATUS_REAUTH_REQUIRED,
)
from app.modules.channels.models_tenant import ChannelListing
from app.modules.channels.shopee import oauth
from app.modules.channels.shopee.oauth import callback_error_redirect, exchange_authorization_code
from app.modules.channels.tiktok import oauth as tiktok_oauth

logger = logging.getLogger("ventasync.channels.router")

router = APIRouter(tags=["channels"])
integration_router = APIRouter(prefix="/integrations/shopee", tags=["shopee"])
tiktok_integration_router = APIRouter(prefix="/integrations/tiktok", tags=["tiktok"])

_VALID_MAPPING_FILTERS = frozenset({LISTING_FILTER_UNMAPPED}) | LINK_STATUSES


# --------------------------------------------------------------------------- 店铺
@router.get("/tenants/{tenant_id}/stores")
def list_stores(
    tenant_id: int = Path(ge=1, description="租户ID"),
    page: PageParams = Depends(),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    snapshot: AccessSnapshot = Depends(require_permission(Permission.STORE_READ.value)),
) -> dict[str, Any]:
    """店铺列表：凭据只返回状态与过期时间，按店铺数据范围过滤。"""
    allowed = service.allowed_store_ids(snapshot.effective, Permission.STORE_READ.value)
    items, total = service.list_store_summaries(
        db,
        tenant_id,
        store_ids=allowed,
        offset=(page.page - 1) * page.page_size,
        limit=page.page_size,
    )
    return paged(
        [schemas.store_payload(item) for item in items],
        total,
        page.page,
        page.page_size,
    )


@router.post("/tenants/{tenant_id}/stores")
def create_tenant_store(
    request: Request,
    body: schemas.StoreCreateIn,
    tenant_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    _: AccessSnapshot = Depends(require_permission(Permission.STORE_CONNECT.value)),
) -> dict[str, Any]:
    """新建店铺：选择渠道 + 填写 ERP 名称，状态为待授权，之后复制授权链接。"""
    store_id = service.create_store(
        db,
        tenant_id=tenant_id,
        channel=body.channel,
        name=body.name,
        environment=body.environment,
        region=body.region,
        actor_id=ctx.user_id,
        request_id=ctx.request_id,
        ip=_client_ip(request),
    )
    items, _total = service.list_store_summaries(
        db, tenant_id, store_ids=frozenset({store_id}), offset=0, limit=1
    )
    if not items:
        raise AppError(ErrorCode.INTERNAL_ERROR, "店铺创建成功但读取失败")
    return success(schemas.store_payload(items[0]))


@router.post("/tenants/{tenant_id}/stores/{store_id}/shopee/authorize")
def authorize_shopee_store_link(
    request: Request,
    tenant_id: int = Path(ge=1),
    store_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    center: Session = Depends(get_center_db),
    snapshot: AccessSnapshot = Depends(require_permission(Permission.STORE_CONNECT.value)),
) -> dict[str, Any]:
    """为「待授权」店铺生成 Shopee 授权会话。"""
    allowed = service.allowed_store_ids(snapshot.effective, Permission.STORE_CONNECT.value)
    if not service.store_in_scope(store_id, allowed):
        raise AppError(ErrorCode.NOT_FOUND, "店铺不存在或不可见")
    store = service.get_store_or_raise(db, tenant_id, store_id)
    if store.channel != "shopee":
        raise AppError(ErrorCode.VALIDATION_ERROR, "该店铺不是 Shopee 渠道")
    result = service.start_shopee_authorization(
        center,
        db,
        tenant_id=tenant_id,
        member_id=ctx.member_id,
        store_id=store_id,
        environment=store.environment,
        redirect_path=f"/tenants/{tenant_id}/stores",
        actor_id=ctx.user_id,
        request_id=ctx.request_id,
        ip=_client_ip(request),
    )
    return success(schemas.authorize_payload(result))


@router.post("/tenants/{tenant_id}/stores/shopee/authorize")
def authorize_shopee_store(
    request: Request,
    body: schemas.ShopeeAuthorizeIn | None = None,
    tenant_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    center: Session = Depends(get_center_db),
    _: AccessSnapshot = Depends(require_permission(Permission.STORE_CONNECT.value)),
) -> dict[str, Any]:
    """发起 Shopee 店铺授权：生成一次性授权会话并返回 auth_url。

    ``body`` 可选：前端只需默认环境时可以不传请求体（兼容旧客户端）。
    """
    result = service.start_shopee_authorization(
        center,
        db,
        tenant_id=tenant_id,
        member_id=ctx.member_id,
        environment=body.environment if body else None,
        redirect_path=body.redirect_path if body else None,
        actor_id=ctx.user_id,
        request_id=ctx.request_id,
        ip=_client_ip(request),
    )
    return success(schemas.authorize_payload(result))


@router.post("/tenants/{tenant_id}/stores/{store_id}/disconnect")
def disconnect_shopee_store(
    request: Request,
    tenant_id: int = Path(ge=1),
    store_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    center: Session = Depends(get_center_db),
    snapshot: AccessSnapshot = Depends(require_permission(Permission.STORE_CONNECT.value)),
) -> dict[str, Any]:
    """断开店铺授权：清空令牌、释放外部店铺归属、停止依赖任务。"""
    allowed = service.allowed_store_ids(snapshot.effective, Permission.STORE_CONNECT.value)
    if not service.store_in_scope(store_id, allowed):
        raise AppError(ErrorCode.NOT_FOUND, "店铺不存在或不可见")
    result = service.disconnect_store(
        center,
        db,
        tenant_id=tenant_id,
        store_id=store_id,
        actor_id=ctx.user_id,
        request_id=ctx.request_id,
        ip=_client_ip(request),
    )
    return success(
        {"store_id": schemas.id_string(result["store_id"]), "status": result["status"]}
    )


# --------------------------------------------------------------------------- 平台商品
@router.get("/tenants/{tenant_id}/listings")
def list_channel_listings(
    tenant_id: int = Path(ge=1),
    page: PageParams = Depends(),
    store_id: int | None = Query(default=None, ge=1, description="按店铺筛选"),
    mapping_status: str | None = Query(
        default=None, description="映射状态筛选：mapped/pending/ambiguous/missing/unmapped"
    ),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    snapshot: AccessSnapshot = Depends(
        require_permission(Permission.CHANNEL_LISTING_READ.value)
    ),
) -> dict[str, Any]:
    """平台商品列表（含映射状态），按店铺数据范围过滤。"""
    if mapping_status and mapping_status not in _VALID_MAPPING_FILTERS:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"不支持的映射状态：{mapping_status}")
    allowed = service.allowed_store_ids(
        snapshot.effective, Permission.CHANNEL_LISTING_READ.value
    )
    if store_id is not None and not service.store_in_scope(store_id, allowed):
        raise AppError(ErrorCode.NOT_FOUND, "店铺不存在或不可见")
    items, total = service.list_listing_summaries(
        db,
        tenant_id,
        store_ids=allowed,
        store_id=store_id,
        mapping_status=mapping_status,
        offset=(page.page - 1) * page.page_size,
        limit=page.page_size,
    )
    return paged(
        [schemas.listing_payload(item) for item in items],
        total,
        page.page,
        page.page_size,
    )


@router.post("/tenants/{tenant_id}/listings/sync")
def sync_channel_listings(
    body: schemas.ListingSyncIn,
    tenant_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    snapshot: AccessSnapshot = Depends(
        require_permission(Permission.CHANNEL_LISTING_MAP.value)
    ),
) -> Response:
    """同步平台商品（异步）：返回 202 + task_id。"""
    allowed = service.allowed_store_ids(snapshot.effective, Permission.CHANNEL_LISTING_MAP.value)
    if not service.store_in_scope(body.store_id_int, allowed):
        raise AppError(ErrorCode.NOT_FOUND, "店铺不存在或不可见")
    store = service.get_store_or_raise(db, tenant_id, body.store_id_int)
    if store.status == STORE_STATUS_REAUTH_REQUIRED:
        raise AppError(ErrorCode.STORE_REAUTH_REQUIRED)
    if store.status != STORE_STATUS_CONNECTED:
        raise AppError(ErrorCode.STORE_NOT_CONNECTED)
    task_id = _dispatch_task(
        "sync_store_listings",
        tenant_id=tenant_id,
        store_id=body.store_id_int,
        requested_by=ctx.user_id,
    )
    return accepted({"task_id": task_id})


@router.post("/tenants/{tenant_id}/listings/{listing_id}/link")
def link_channel_listing(
    request: Request,
    body: schemas.ListingLinkIn,
    tenant_id: int = Path(ge=1),
    listing_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    snapshot: AccessSnapshot = Depends(
        require_permission(Permission.CHANNEL_LISTING_MAP.value)
    ),
) -> dict[str, Any]:
    """人工建立平台商品 → 本地 SKU 映射（SKU 必须属于本租户）。"""
    listing = _scoped_listing(
        db, tenant_id, listing_id, snapshot, Permission.CHANNEL_LISTING_MAP.value
    )
    result = service.link_listing(
        db,
        tenant_id,
        listing_id=listing.id,
        product_id=body.product_id_int,
        sku_id=body.sku_id_int,
        actor_id=ctx.user_id,
        request_id=ctx.request_id,
        ip=_client_ip(request),
    )
    return success(schemas.link_payload(result))


@router.post("/tenants/{tenant_id}/listings/{listing_id}/unlink")
def unlink_channel_listing(
    request: Request,
    tenant_id: int = Path(ge=1),
    listing_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    snapshot: AccessSnapshot = Depends(
        require_permission(Permission.CHANNEL_LISTING_MAP.value)
    ),
) -> dict[str, Any]:
    """解除平台商品映射（保留人工标记，避免自动匹配立即重绑）。"""
    listing = _scoped_listing(
        db, tenant_id, listing_id, snapshot, Permission.CHANNEL_LISTING_MAP.value
    )
    result = service.unlink_listing(
        db,
        tenant_id,
        listing_id=listing.id,
        actor_id=ctx.user_id,
        request_id=ctx.request_id,
        ip=_client_ip(request),
    )
    return success(schemas.link_payload(result))


# --------------------------------------------------------------------------- 平台回调
@integration_router.get("/oauth/callback")
def shopee_oauth_callback(
    code: str | None = Query(default=None, description="平台授权码"),
    shop_id: str | None = Query(default=None, description="平台店铺ID"),
    state: str | None = Query(default=None, description="一次性授权会话 state"),
    error: str | None = Query(default=None, description="平台返回的错误码"),
) -> RedirectResponse:
    """Shopee 授权回调：校验一次性授权会话后换取 Token，成功 / 失败都 302 跳前端。"""
    redirect_path: str | None = None
    try:
        session = oauth.consume_authorization_session(state)
        redirect_path = str(session.get("redirect_path") or "")
        if error:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, f"平台拒绝授权：{error}")
        code_value, shop_value = oauth.validate_callback_params(code, shop_id)
        environment = str(session["environment"])
        tenant_id = int(session["tenant_id"])
        with center_session() as center:
            config = service.require_channel_app(center, environment)
            service.ensure_tenant_active(center, tenant_id)
            route = _load_tenant_route(center, tenant_id)
            partner_id = int(config.partner_id or 0)
            partner_key = config.partner_key
        # 外部 HTTP 请求在数据库事务之外执行
        exchanged = exchange_authorization_code(
            environment=environment,
            partner_id=partner_id,
            partner_key=partner_key,
            code=code_value,
            shop_id=shop_value,
        )
        with center_session() as center, tenant_session(route) as db:
            result = oauth.complete_authorization(
                center,
                db,
                session=session,
                shop_id=shop_value,
                token_data=exchanged["token"],
                profile=exchanged["profile"],
            )
        return RedirectResponse(
            oauth.frontend_redirect_url(
                str(result.get("redirect_path") or redirect_path),
                params={"shopee": "connected"},
            ),
            status_code=302,
        )
    except AppError as exc:
        logger.warning("Shopee 授权回调失败：%s %s", exc.code.value, exc.message)
        return RedirectResponse(
            callback_error_redirect(redirect_path=redirect_path, reason=exc.code.value),
            status_code=302,
        )
    except Exception:  # noqa: BLE001 - 回调必须返回 302，不把异常抛给平台页面
        logger.exception("Shopee 授权回调异常")
        return RedirectResponse(
            callback_error_redirect(redirect_path=redirect_path, reason="internal_error"),
            status_code=302,
        )


@integration_router.post("/webhook")
async def shopee_webhook(request: Request) -> dict[str, Any]:
    """Shopee 推送入口：读取原始报文后在线程池中做验签、防重与订单同步触发。"""
    raw_body = await request.body()
    signature = (
        request.headers.get("authorization")
        or request.headers.get("x-shopee-signature")
        or ""
    )
    request_url = str(request.url)
    return await run_in_threadpool(_handle_webhook, raw_body, signature, request_url)


# --------------------------------------------------------------------------- 内部工具
def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _dispatch_task(task_name: str, **kwargs: Any) -> str:
    """投递渠道 Celery 任务并返回 task_id（队列不可用转 503）。"""
    from app.modules.channels import tasks as channel_tasks

    task = getattr(channel_tasks, task_name, None)
    if task is None:  # pragma: no cover - 任务名是模块内常量
        raise AppError(ErrorCode.INTERNAL_ERROR, f"渠道任务不存在：{task_name}")
    try:
        return channel_tasks.dispatch(task, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 队列不可用统一转 503
        logger.exception("投递渠道任务失败：%s", task_name)
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "任务队列不可用，请稍后重试") from exc


def _scoped_listing(
    db: Session,
    tenant_id: int,
    listing_id: int,
    snapshot: AccessSnapshot,
    permission: str,
) -> ChannelListing:
    """读取平台商品并校验其店铺在数据范围内（不可见返回 404，不泄漏存在性）。"""
    listing = service.get_listing_or_raise(db, tenant_id, listing_id)
    allowed = service.allowed_store_ids(snapshot.effective, permission)
    if not service.store_in_scope(listing.store_id, allowed):
        raise AppError(ErrorCode.NOT_FOUND, "平台商品不存在或不可见")
    return listing


def _load_tenant_route(center: Session, tenant_id: int) -> Any:
    """读取租户库路由（identity 契约；未实现时返回 503 而不是 500）。"""
    from app.modules.identity.service import load_tenant_db_route

    try:
        route = load_tenant_db_route(center, tenant_id)
    except NotImplementedError as exc:
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "租户数据库路由尚未就绪") from exc
    if route is None:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE)
    return route


def _handle_webhook(raw_body: bytes, signature: str, request_url: str) -> dict[str, Any]:
    """推送处理：验签 → 防重写 webhook_events → 触发订单同步（均在线程池执行）。"""
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, "推送报文不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise AppError(ErrorCode.VALIDATION_ERROR, "推送报文格式不正确")
    shop_id = str(payload.get("shop_id") or "").strip()
    if not shop_id:
        logger.warning("Shopee 推送缺少 shop_id，忽略")
        return success({"received": False, "reason": "missing_shop_id"})
    with center_session() as center:
        context = service.resolve_webhook_store(center, shop_id=shop_id)
        if context is None:
            logger.warning("Shopee 推送的店铺未登记归属，忽略：shop_id=%s", shop_id)
            return success({"received": False, "reason": "unknown_shop"})
        if not service.verify_shopee_webhook(
            context.app,
            request_url=request_url,
            raw_body=raw_body,
            received_signature=signature,
        ):
            raise AppError(ErrorCode.CHANNEL_SIGNATURE_INVALID)
        tenant_status = service.load_tenant_status(center, context.tenant_id)
        route = _load_tenant_route(center, context.tenant_id)
    event_type = service.webhook_event_type(payload)
    dedupe_key = service.webhook_dedupe_key(payload)
    with tenant_session(route) as db:
        _, duplicate = service.record_webhook_event(
            db,
            context.tenant_id,
            store_id=context.store_id,
            event_type=event_type,
            external_event_id=dedupe_key,
            payload=payload,
            signature_ok=True,
        )
        # 租户暂停时：已验签回调只暂存事件，不改变库存 / 不触发订单同步
        if (
            tenant_status == "active"
            and not duplicate
            and event_type in {"order_status", "order_tracking", "return_update"}
        ):
            try:
                service.enqueue_order_sync(db, context.tenant_id, context.store_id)
            except AppError as exc:
                # 事件已落库（status=received），同步触发失败不向平台返回 5xx
                logger.warning(
                    "推送触发订单同步失败：tenant=%s store=%s code=%s",
                    context.tenant_id,
                    context.store_id,
                    exc.code.value,
                )
        db.commit()
    return success(
        {
            "received": True,
            "event_type": event_type,
            "duplicate": duplicate,
            "tenant_active": tenant_status == "active",
        }
    )

# --------------------------------------------------------------------------- TikTok Shop
@router.post("/tenants/{tenant_id}/stores/{store_id}/tiktok/authorize")
def authorize_tiktok_store_link(
    request: Request,
    tenant_id: int = Path(ge=1),
    store_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    center: Session = Depends(get_center_db),
    snapshot: AccessSnapshot = Depends(require_permission(Permission.STORE_CONNECT.value)),
) -> dict[str, Any]:
    """为「待授权」店铺生成 TikTok Shop 授权链接（Service ID + state）。"""
    allowed = service.allowed_store_ids(snapshot.effective, Permission.STORE_CONNECT.value)
    if not service.store_in_scope(store_id, allowed):
        raise AppError(ErrorCode.NOT_FOUND, "店铺不存在或不可见")
    store = service.get_store_or_raise(db, tenant_id, store_id)
    if store.channel != CHANNEL_TIKTOK:
        raise AppError(ErrorCode.VALIDATION_ERROR, "该店铺不是 TikTok Shop 渠道")
    result = tiktok_oauth.start_authorization(
        center,
        db,
        tenant_id=tenant_id,
        member_id=ctx.member_id,
        store_id=store_id,
        environment=store.environment,
        redirect_path=f"/tenants/{tenant_id}/stores",
        actor_id=ctx.user_id,
        request_id=ctx.request_id,
        ip=_client_ip(request),
    )
    return success(schemas.authorize_payload(result))


@router.post("/tenants/{tenant_id}/stores/tiktok/authorize")
def authorize_tiktok_store(
    request: Request,
    body: schemas.TikTokAuthorizeIn | None = None,
    tenant_id: int = Path(ge=1),
    ctx: TenantContext = Depends(tenant_context),
    db: Session = Depends(get_tenant_db),
    center: Session = Depends(get_center_db),
    _: AccessSnapshot = Depends(require_permission(Permission.STORE_CONNECT.value)),
) -> dict[str, Any]:
    """发起 TikTok Shop 店铺授权：生成一次性授权会话并返回 auth_url。"""
    result = tiktok_oauth.start_authorization(
        center,
        db,
        tenant_id=tenant_id,
        member_id=ctx.member_id,
        environment=body.environment if body else None,
        redirect_path=body.redirect_path if body else None,
        actor_id=ctx.user_id,
        request_id=ctx.request_id,
        ip=_client_ip(request),
    )
    return success(schemas.authorize_payload(result))


@tiktok_integration_router.get("/oauth/callback")
def tiktok_oauth_callback(
    code: str | None = Query(default=None, description="TikTok 授权码"),
    state: str | None = Query(default=None, description="一次性授权会话 state"),
    error: str | None = Query(default=None, description="TikTok 返回的错误码"),
) -> RedirectResponse:
    """TikTok Shop 授权回调：校验一次性会话后换 token、读取店铺并绑定，302 回前端。"""
    redirect_path: str | None = None
    try:
        session = tiktok_oauth.consume_authorization_session(state)
        redirect_path = str(session.get("redirect_path") or "")
        if error:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, f"平台拒绝授权：{error}")
        code_value = (code or "").strip()
        if not code_value:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "缺少授权码 code")
        environment = str(session["environment"])
        tenant_id = int(session["tenant_id"])
        target_store_id = int(session["store_id"]) if session.get("store_id") else None
        with center_session() as center:
            config = service.require_channel_app(center, environment, channel=CHANNEL_TIKTOK)
            service.ensure_tenant_active(center, tenant_id)
            route = _load_tenant_route(center, tenant_id)
            app_key = config.app_key
            app_secret = config.app_secret
        # 先确认 state 指向的店铺仍然有效，避免把 token 写到错误的店铺
        if target_store_id is not None:
            with tenant_session(route) as check_db:
                target_store = service.get_store_or_raise(check_db, tenant_id, target_store_id)
                if target_store.channel != CHANNEL_TIKTOK:
                    raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权目标店铺不是 TikTok Shop")
                if target_store.status not in {
                    STORE_STATUS_PENDING_AUTHORIZATION,
                    STORE_STATUS_REAUTH_REQUIRED,
                    STORE_STATUS_CONNECTED,
                }:
                    raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "授权目标店铺状态不允许授权")
                logger.info(
                    "TikTok 授权回调定位店铺：tenant=%s store=%s store_code=%s status=%s",
                    tenant_id,
                    target_store.id,
                    target_store.store_code,
                    target_store.status,
                )
        # 外部 HTTP 请求在数据库事务之外执行
        exchanged = tiktok_oauth.exchange_code(app_key, app_secret, code_value)
        with center_session() as center, tenant_session(route) as db:
            result = tiktok_oauth.complete_authorization(
                center,
                db,
                session=session,
                token=exchanged["token"],
                shop=exchanged["shop"],
                warehouse_id=exchanged.get("warehouse_id") or "",
            )
        return RedirectResponse(
            tiktok_oauth.frontend_redirect_url(
                str(result.get("redirect_path") or redirect_path),
                params={
                    "tiktok": "connected",
                    "store_code": str(result.get("store_code") or ""),
                },
            ),
            status_code=302,
        )
    except AppError as exc:
        logger.warning("TikTok 授权回调失败：%s %s", exc.code.value, exc.message)
        return RedirectResponse(
            tiktok_oauth.callback_error_redirect(
                redirect_path=redirect_path, reason=exc.code.value
            ),
            status_code=302,
        )
    except Exception:  # noqa: BLE001
        logger.exception("TikTok 授权回调异常")
        return RedirectResponse(
            tiktok_oauth.callback_error_redirect(
                redirect_path=redirect_path, reason="internal_error"
            ),
            status_code=302,
        )

