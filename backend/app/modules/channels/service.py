"""渠道模块业务服务。

覆盖（plan.txt §4「统一应用与店铺授权」「商品与订单」）：

* 平台渠道应用（Shopee）凭据的读取 / 加密保存，GET 永不明文回显；
* 店铺列表 / 发起授权 / 断开，凭据失效标记 ``reauth_required`` 并暂停依赖任务；
* 平台商品读取、入库、自动匹配（只接受唯一明确结果）与人工映射；
* Token 刷新（按店铺串行）与库存推送（按 store+sku 串行、执行时读最新库存、最低 0）；
* Shopee 推送验签、事件防重与订单同步触发（跨模块函数延迟 import）；
* 审计写入（``app.modules.audit.service.record`` 延迟 import）。

分层约定：Router 不写业务规则，Repository 不开事务，事务边界在本模块 Service。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AppError, ErrorCode
from app.core.permissions import SCOPE_STORES
from app.core.redis import acquire_lock, release_lock
from app.core.security import decrypt_secret, encrypt_secret, utc_now
from app.modules.channels import repository as repo
from app.modules.channels.constants import (
    CHANNEL_SHOPEE,
    CHANNEL_TIKTOK,
    CREDENTIAL_STATUS_ACTIVE,
    CREDENTIAL_STATUS_INVALID,
    CREDENTIAL_STATUS_REVOKED,
    ENV_TEST,
    LINK_STATUS_AMBIGUOUS,
    LINK_STATUS_MAPPED,
    LINK_STATUS_PENDING,
    LISTING_STATUS_ACTIVE,
    LISTING_STATUS_INACTIVE,
    MAPPING_SOURCE_AUTO,
    MAPPING_SOURCE_MANUAL,
    NO_EXTERNAL_SKU,
    STORE_STATUS_CONNECTED,
    SUPPORTED_CHANNELS,
    STORE_STATUS_DISCONNECTED,
    STORE_STATUS_REAUTH_REQUIRED,
    TOKEN_REFRESH_SKEW_SECONDS,
    normalize_environment,
    normalize_external_id,
    normalize_sku_code,
    resolve_environment,
)
from app.modules.channels.models_center import ChannelAccountRegistry, PlatformChannelApp
from app.modules.channels.models_tenant import (
    ChannelListing,
    ChannelListingLink,
    Store,
    StoreCredential,
)
from app.modules.channels.shopee import oauth
from app.modules.channels.shopee.client import (
    ShopeeApiError,
    ShopeeClient,
    build_client_for_environment,
    is_auth_invalid_error_code,
    resolve_base_url,
)
from app.modules.channels.tiktok.client import (
    TikTokApiError,
    TikTokClient,
    normalize_token_payload as normalize_tiktok_token,
    token_expiry_times as tiktok_token_expiry_times,
)
from app.modules.channels.shopee.signature import (
    is_expired,
    safe_redirect_path,
    verify_webhook_signature,
)

logger = logging.getLogger("ventasync.channels")

# 授权回调默认路径（平台管理员未显式配置 redirect_url 时使用）
DEFAULT_CALLBACK_PATH = f"{settings.api_prefix}/integrations/shopee/oauth/callback"
TIKTOK_CALLBACK_PATH = f"{settings.api_prefix}/integrations/tiktok/oauth/callback"
# 商品同步分页安全上限（防止异常平台数据造成死循环）
MAX_ITEM_PAGES = 40
ITEM_PAGE_SIZE = 50
# 库存推送 Redis 锁 TTL
STOCK_PUSH_LOCK_TTL_SECONDS = 120
TOKEN_REFRESH_LOCK_TTL_SECONDS = 120

# --------------------------------------------------------------------------- Webhook 补偿
# 未处理（status=received）的订单类推送事件超过该秒数后由渠道补偿任务安全重投
WEBHOOK_COMPENSATION_MIN_AGE_SECONDS = 120
# 单个事件最多补偿次数，超过标记 failed（避免毒事件无限重试）
WEBHOOK_COMPENSATION_MAX_ATTEMPTS = 5
# 事件 payload 中保存补偿元数据的键（webhook_events 表归 tasks/sales 模块，不新增列）
WEBHOOK_COMPENSATION_META_KEY = "_channel_compensation"
# 只有这些订单类事件需要重投订单同步（item_status 等由任务模块自行处理）
COMPENSATION_ORDER_EVENT_TYPES: frozenset[str] = frozenset(
    {"order_status", "order_tracking", "return_update"}
)


# --------------------------------------------------------------------------- 领域数据结构
@dataclass(frozen=True, slots=True)
class StoreInfo:
    """销售 / 库存模块使用的店铺最小信息（契约见 docs/architecture.md）。"""

    id: int
    channel: str
    name: str
    status: str
    region: str | None = None
    external_store_id: str = ""
    environment: str = ENV_TEST


@dataclass(frozen=True, slots=True)
class ChannelAppConfig:
    """平台渠道应用配置（含解密后的密钥，仅服务端内部使用）。"""

    channel: str
    environment: str
    partner_id: int | None
    partner_key: str
    partner_secret: str
    redirect_url: str
    status: str
    updated_at: datetime | None = None
    # TikTok Shop 使用 App Key / App Secret；Shopee 使用 partner_* 字段
    app_key: str = ""
    app_secret: str = ""

    @property
    def configured(self) -> bool:
        if self.status != "active":
            return False
        if self.channel == CHANNEL_TIKTOK:
            return bool(self.app_key and self.app_secret)
        return bool(self.partner_id and self.partner_key)


@dataclass(frozen=True, slots=True)
class StoreSummary:
    """店铺列表展示项（不含任何明文凭据）。"""

    id: int
    channel: str
    name: str
    external_store_id: str
    region: str | None
    timezone: str
    status: str
    environment: str
    authorized_at: datetime | None
    last_synced_at: datetime | None
    disconnected_at: datetime | None
    last_error: str | None
    credential_status: str | None
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class SkuCandidate:
    """自动匹配候选：本地启用 SKU。"""

    sku_id: int
    product_id: int
    sku_code: str
    specs: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkuMatch:
    """自动匹配结果：``mapped`` / ``pending`` / ``ambiguous``。"""

    status: str
    sku_id: int | None = None
    product_id: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RemoteListing:
    """从平台读取到的一条平台商品（item 或 model）快照。"""

    external_item_id: str
    external_sku_id: str
    title: str
    price: Decimal | None
    currency: str
    remote_stock: int | None
    status: str
    specs: Mapping[str, str]
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class WebhookContext:
    """推送回调定位到的租户 / 店铺 / 应用环境。"""

    tenant_id: int
    store_id: int
    environment: str
    app: ChannelAppConfig


# --------------------------------------------------------------------------- 审计
def record_audit(
    db: Session,
    *,
    tenant_id: int,
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    actor_id: int | None = None,
    summary: str = "",
    payload: dict[str, Any] | None = None,
    request_id: str = "",
    ip: str = "",
) -> None:
    """写租户审计（延迟 import，identity 未实现时只记录日志，不阻断业务）。"""
    try:
        from app.modules.audit.service import record as audit_record
    except ImportError:  # pragma: no cover - 模块按计划一定存在
        logger.warning("audit 模块未就绪，跳过审计：%s", action)
        return
    try:
        audit_record(
            db,
            tenant_id=tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_id=actor_id,
            summary=summary,
            payload=payload,
            request_id=request_id,
            ip=ip,
        )
    except NotImplementedError:
        logger.warning("audit.service.record 尚未实现，跳过审计：%s", action)
    except ImportError:  # pragma: no cover
        logger.warning("audit 模块不可导入，跳过审计：%s", action)


# --------------------------------------------------------------------------- 租户状态
def load_tenant_status(center: Session, tenant_id: int) -> str | None:
    """读取中心库租户状态（授权回调 / 推送不依赖用户会话，需自行核对租户状态）。"""
    try:
        row = center.execute(
            text("SELECT status FROM tenants WHERE id = :tenant_id"),
            {"tenant_id": tenant_id},
        ).first()
    except SQLAlchemyError:  # pragma: no cover - 中心库表未就绪
        logger.warning("读取租户状态失败：tenant=%s", tenant_id)
        return None
    return str(row[0]) if row is not None else None


def ensure_tenant_active(center: Session, tenant_id: int) -> None:
    """回调 / 任务入口校验租户可用状态（暂停或未激活直接拒绝）。"""
    status = load_tenant_status(center, tenant_id)
    if status is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    if status == "suspended":
        raise AppError(ErrorCode.TENANT_SUSPENDED)
    if status != "active":
        raise AppError(ErrorCode.TENANT_NOT_ACTIVE)


# --------------------------------------------------------------------------- 平台应用配置
def _require_channel_environment(channel: str, environment: str) -> tuple[str, str]:
    channel_value = (channel or "").strip().lower()
    environment_value = normalize_environment(environment)
    if channel_value not in SUPPORTED_CHANNELS:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"不支持的渠道：{channel}")
    if environment_value is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "环境必须是 production 或 test")
    return channel_value, environment_value


def _config_from_row(row: PlatformChannelApp) -> ChannelAppConfig:
    """把密文行还原为内部配置对象（解密失败按未配置处理，避免 500）。"""
    partner_key = ""
    partner_secret = ""
    if row.partner_key_cipher:
        try:
            partner_key = decrypt_secret(row.partner_key_cipher)
        except Exception:  # noqa: BLE001 - 密钥轮换 / 数据损坏时按未配置处理
            logger.warning("渠道应用 partner_key 解密失败：%s/%s", row.channel, row.environment)
    if row.partner_secret_cipher:
        try:
            partner_secret = decrypt_secret(row.partner_secret_cipher)
        except Exception:  # noqa: BLE001
            logger.warning("渠道应用 partner_secret 解密失败：%s/%s", row.channel, row.environment)
    app_secret = ""
    if row.app_secret_cipher:
        try:
            app_secret = decrypt_secret(row.app_secret_cipher)
        except Exception:  # noqa: BLE001
            logger.warning("渠道应用 app_secret 解密失败：%s/%s", row.channel, row.environment)
    return ChannelAppConfig(
        channel=row.channel,
        environment=row.environment,
        partner_id=int(row.partner_id) if row.partner_id is not None else None,
        partner_key=partner_key,
        partner_secret=partner_secret,
        redirect_url=row.redirect_url or "",
        status=row.status or "active",
        updated_at=row.updated_at,
        app_key=row.app_key or "",
        app_secret=app_secret,
    )


def load_channel_app(
    center: Session, channel: str, environment: str
) -> ChannelAppConfig | None:
    """读取平台渠道应用配置（含解密密钥，仅供服务端内部使用）。

    平台 GET 接口必须使用 :func:`channel_app_public_view`，不得回显密钥。
    """
    row = repo.get_channel_app(center, channel, environment)
    if row is None:
        return None
    return _config_from_row(row)


def channel_app_public_view(
    config: ChannelAppConfig | None, *, channel: str = CHANNEL_SHOPEE
) -> dict[str, Any]:
    """平台管理页展示用结构：只说明密钥是否已配置，绝不回显明文。"""
    if config is None:
        return {
            "channel": channel,
            "environment": "",
            "partner_id": None,
            "app_key": "",
            "has_partner_key": False,
            "has_partner_secret": False,
            "has_app_secret": False,
            "redirect_url": "",
            "status": "unconfigured",
            "configured": False,
            "updated_at": None,
        }
    return {
        "channel": config.channel,
        "environment": config.environment,
        "partner_id": str(config.partner_id) if config.partner_id is not None else None,
        "app_key": config.app_key,
        "has_partner_key": bool(config.partner_key),
        "has_partner_secret": bool(config.partner_secret),
        "has_app_secret": bool(config.app_secret),
        "redirect_url": config.redirect_url,
        "status": config.status,
        "configured": config.configured,
        "updated_at": config.updated_at.isoformat() if config.updated_at else None,
    }


def save_channel_app(
    center: Session,
    channel: str,
    environment: str,
    *,
    partner_id: int | None = None,
    partner_key: str | None = None,
    partner_secret: str | None = None,
    app_key: str | None = None,
    app_secret: str | None = None,
    redirect_url: str | None = None,
    actor_id: int | None = None,
) -> ChannelAppConfig:
    """保存平台渠道应用配置（密钥 AES 加密；``None`` 表示不修改，空串表示清空）。

    Shopee 使用 partner_id/partner_key/partner_secret；TikTok Shop 使用
    app_key/app_secret（App Secret 加密入库）。不提交事务，由调用方 commit。
    """
    channel_value, environment_value = _require_channel_environment(channel, environment)
    row = repo.get_channel_app(center, channel_value, environment_value)
    now = utc_now()
    if row is None:
        row = PlatformChannelApp(
            channel=channel_value,
            environment=environment_value,
            status="active",
            created_at=now,
            updated_at=now,
        )
        center.add(row)

    if channel_value == CHANNEL_TIKTOK:
        next_app_key = (app_key if app_key is not None else (row.app_key or "")).strip()
        if not next_app_key:
            raise AppError(ErrorCode.VALIDATION_ERROR, "TikTok App Key 不能为空")
        next_secret_set = bool(app_secret is not None and app_secret.strip()) or bool(
            row.app_secret_cipher
        )
        if not next_secret_set:
            raise AppError(ErrorCode.VALIDATION_ERROR, "TikTok App Secret 不能为空")
        row.app_key = next_app_key
        if app_secret is not None:
            row.app_secret_cipher = (
                encrypt_secret(app_secret.strip()) if app_secret.strip() else ""
            )
    else:
        if partner_id is None or int(partner_id) <= 0:
            raise AppError(ErrorCode.VALIDATION_ERROR, "partner_id 必须为正整数")
        row.partner_id = int(partner_id)
        if partner_key is not None:
            row.partner_key_cipher = (
                encrypt_secret(partner_key.strip()) if partner_key.strip() else ""
            )
        if partner_secret is not None:
            row.partner_secret_cipher = (
                encrypt_secret(partner_secret.strip()) if partner_secret.strip() else ""
            )

    row.status = "active"
    row.updated_at = now
    if redirect_url is not None:
        row.redirect_url = redirect_url.strip() or None
    center.flush()
    logger.info(
        "平台渠道应用已保存：channel=%s environment=%s app_key=%s operator=%s",
        channel_value,
        environment_value,
        row.app_key or row.partner_id,
        actor_id,
    )
    return _config_from_row(row)


def require_channel_app(
    center: Session, environment: str, *, channel: str = CHANNEL_SHOPEE
) -> ChannelAppConfig:
    """读取并校验渠道应用可用；未配置抛 ``CHANNEL_APP_NOT_CONFIGURED``。"""
    channel_value = (channel or "").strip().lower()
    config = load_channel_app(center, channel_value, environment)
    if config is None or not config.configured:
        label = "TikTok Shop" if channel_value == CHANNEL_TIKTOK else "Shopee"
        raise AppError(ErrorCode.CHANNEL_APP_NOT_CONFIGURED, f"平台 {label} 应用未配置")
    return config


# --------------------------------------------------------------------------- 店铺契约（供 sales 调用）
def load_store(db: Session, tenant_id: int, store_id: int) -> StoreInfo | None:
    """加载店铺最小信息；不存在或不属于该租户返回 ``None``。"""
    store = repo.get_store(db, tenant_id, store_id)
    if store is None:
        return None
    return StoreInfo(
        id=store.id,
        channel=store.channel,
        name=store.name,
        status=store.status,
        region=store.region,
        external_store_id=store.external_store_id,
        environment=store.environment,
    )


def mark_store_synced(
    db: Session, tenant_id: int, store_id: int, at: datetime | None = None
) -> None:
    """更新店铺最近同步时间（不提交事务，由调用方事务控制）。"""
    db.execute(
        update(Store)
        .where(Store.tenant_id == tenant_id, Store.id == store_id)
        .values(last_synced_at=at or utc_now(), updated_at=utc_now())
    )


def get_store_or_raise(db: Session, tenant_id: int, store_id: int) -> Store:
    store = repo.get_store(db, tenant_id, store_id)
    if store is None:
        raise AppError(ErrorCode.NOT_FOUND, "店铺不存在或不可见")
    return store


def list_store_summaries(
    db: Session,
    tenant_id: int,
    *,
    store_ids: frozenset[int] | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[StoreSummary], int]:
    """店铺分页（凭据只返回状态与过期时间，不返回 Token）。"""
    stores = repo.list_stores(db, tenant_id, store_ids=store_ids, offset=offset, limit=limit)
    total = repo.count_stores(db, tenant_id, store_ids)
    credentials = repo.list_credentials(db, tenant_id, [store.id for store in stores])
    items = [
        StoreSummary(
            id=store.id,
            channel=store.channel,
            name=store.name,
            external_store_id=store.external_store_id,
            region=store.region,
            timezone=store.timezone,
            status=store.status,
            environment=store.environment,
            authorized_at=store.authorized_at,
            last_synced_at=store.last_synced_at,
            disconnected_at=store.disconnected_at,
            last_error=store.last_error,
            credential_status=(credentials[store.id].status if store.id in credentials else None),
            access_token_expires_at=(
                credentials[store.id].access_token_expires_at if store.id in credentials else None
            ),
            refresh_token_expires_at=(
                credentials[store.id].refresh_token_expires_at if store.id in credentials else None
            ),
        )
        for store in stores
    ]
    return items, total


# --------------------------------------------------------------------------- 授权 / 断开
def default_callback_url(*, channel: str = CHANNEL_SHOPEE) -> str:
    """默认授权回调地址（平台应用未显式配置 redirect_url 时使用）。"""
    if channel == CHANNEL_TIKTOK:
        if settings.tiktok_auth_redirect_url:
            return settings.tiktok_auth_redirect_url
        origin = (settings.app_origin or "").rstrip("/")
        return f"{origin}{TIKTOK_CALLBACK_PATH}"
    if settings.shopee_auth_redirect_url:
        return settings.shopee_auth_redirect_url
    origin = (settings.app_origin or "").rstrip("/")
    return f"{origin}{DEFAULT_CALLBACK_PATH}"


def start_shopee_authorization(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    member_id: int,
    environment: str | None = None,
    redirect_path: str | None = None,
    actor_id: int | None = None,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """发起 Shopee 店铺授权，返回一次性授权会话与 ``auth_url``。"""
    env = normalize_environment(environment) or resolve_environment(settings.app_env)
    config = require_channel_app(center, env)
    callback_url = config.redirect_url or default_callback_url()
    if not callback_url.startswith("http"):
        raise AppError(ErrorCode.CHANNEL_APP_NOT_CONFIGURED, "平台未配置可用的 OAuth 回调地址")
    session = oauth.create_authorization_session(
        tenant_id=tenant_id,
        member_id=member_id,
        environment=env,
        redirect_path=safe_redirect_path(redirect_path, f"/tenants/{tenant_id}/stores"),
        auth_base=resolve_base_url(env),
        partner_id=int(config.partner_id or 0),
        partner_key=config.partner_key,
        redirect_url=callback_url,
    )
    record_audit(
        db,
        tenant_id=tenant_id,
        action="store.authorize.started",
        resource_type="store",
        actor_id=actor_id,
        summary=f"发起 Shopee 店铺授权（{env}）",
        payload={"channel": CHANNEL_SHOPEE, "environment": env},
        request_id=request_id,
        ip=ip,
    )
    db.commit()
    return {
        "auth_url": session["auth_url"],
        "expires_at": session["expires_at"],
        "environment": session["environment"],
    }


def _channel_label(channel: str) -> str:
    return "TikTok Shop" if channel == CHANNEL_TIKTOK else "Shopee"


def upsert_store_from_authorization(
    db: Session,
    *,
    tenant_id: int,
    channel: str,
    environment: str,
    external_store_id: str,
    store_name: str,
    region: str | None,
    access_token: str,
    refresh_token: str,
    access_token_expires_at: datetime | None,
    refresh_token_expires_at: datetime | None,
    member_id: int | None = None,
    shop_cipher: str | None = None,
    warehouse_id: str | None = None,
) -> int:
    """授权成功后写入店铺与加密凭据；重新授权更新原店铺，不重复创建。"""
    now = utc_now()
    external_id = normalize_external_id(external_store_id)
    if not external_id:
        raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "店铺ID为空")
    store = repo.find_store_by_external(db, tenant_id, channel, external_id)
    if store is None:
        store = Store(
            tenant_id=tenant_id,
            channel=channel,
            environment=environment,
            name=store_name or f"{_channel_label(channel)} 店铺 {external_id}",
            external_store_id=external_id,
            region=region,
            timezone="UTC",
            status=STORE_STATUS_CONNECTED,
            authorized_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(store)
        db.flush()
    else:
        store.environment = environment
        store.name = store_name or store.name
        store.region = region or store.region
        store.status = STORE_STATUS_CONNECTED
        store.authorized_at = now
        store.disconnected_at = None
        store.last_error = None
        store.updated_at = now
    if warehouse_id:
        store.warehouse_id = str(warehouse_id)[:64]
    credential = repo.get_credential(db, tenant_id, store.id)
    if credential is None:
        credential = StoreCredential(
            tenant_id=tenant_id,
            store_id=store.id,
            created_at=now,
            updated_at=now,
        )
        db.add(credential)
    credential.access_token_cipher = encrypt_secret(access_token or "")
    credential.refresh_token_cipher = encrypt_secret(refresh_token or "")
    credential.access_token_expires_at = access_token_expires_at
    credential.refresh_token_expires_at = refresh_token_expires_at
    credential.status = CREDENTIAL_STATUS_ACTIVE
    credential.refreshed_at = now
    credential.last_error = None
    credential.updated_at = now
    if shop_cipher is not None:
        credential.shop_cipher_cipher = encrypt_secret(shop_cipher or "")
    db.flush()
    logger.info(
        "店铺授权写入：tenant=%s store=%s channel=%s environment=%s operator=%s",
        tenant_id,
        store.id,
        channel,
        environment,
        member_id,
    )
    return store.id


def claim_store_registry(
    center: Session,
    *,
    channel: str,
    environment: str,
    external_account_id: str,
    tenant_id: int,
    store_id: int,
) -> int:
    """在中心库登记外部店铺归属；同一环境同一店铺已被其他租户占用时抛冲突。"""
    external_id = normalize_external_id(external_account_id)
    entry = repo.get_registry_entry(center, channel, environment, external_id)
    now = utc_now()
    if entry is not None:
        if entry.status == "bound" and entry.tenant_id not in (None, tenant_id):
            raise AppError(
                ErrorCode.STORE_ALREADY_BOUND,
                "该 Shopee 店铺已归属其他租户，请先在原租户断开授权",
            )
        entry.tenant_id = tenant_id
        entry.store_id = store_id
        entry.status = "bound"
        entry.bound_at = now
        entry.released_at = None
        entry.updated_at = now
        center.flush()
        return entry.id
    entry = ChannelAccountRegistry(
        channel=channel,
        environment=environment,
        external_account_id=external_id,
        tenant_id=tenant_id,
        store_id=store_id,
        status="bound",
        bound_at=now,
        created_at=now,
        updated_at=now,
    )
    center.add(entry)
    center.flush()
    return entry.id


def release_store_registry(
    center: Session, *, channel: str, tenant_id: int, store_id: int
) -> None:
    """断开授权时释放外部店铺归属（保留行用于追溯）。"""
    entry = repo.get_registry_for_store(center, channel, tenant_id, store_id)
    if entry is None:
        return
    entry.status = "released"
    entry.released_at = utc_now()
    entry.updated_at = utc_now()
    center.flush()


def disconnect_store(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    actor_id: int | None = None,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """断开店铺授权：停止依赖该凭据的任务、清空令牌、释放归属登记。"""
    store = get_store_or_raise(db, tenant_id, store_id)
    now = utc_now()
    store.status = STORE_STATUS_DISCONNECTED
    store.disconnected_at = now
    store.last_error = None
    store.updated_at = now
    credential = repo.get_credential(db, tenant_id, store_id)
    if credential is not None:
        credential.access_token_cipher = ""
        credential.refresh_token_cipher = ""
        credential.access_token_expires_at = None
        credential.refresh_token_expires_at = None
        credential.status = CREDENTIAL_STATUS_REVOKED
        credential.last_error = None
        credential.updated_at = now
    release_store_registry(center, channel=store.channel, tenant_id=tenant_id, store_id=store_id)
    record_audit(
        db,
        tenant_id=tenant_id,
        action="store.disconnected",
        resource_type="store",
        resource_id=store.id,
        actor_id=actor_id,
        summary="断开 Shopee 店铺授权",
        payload={"channel": store.channel, "external_store_id": store.external_store_id},
        request_id=request_id,
        ip=ip,
    )
    db.commit()
    center.commit()
    return {"store_id": store.id, "status": store.status}


# --------------------------------------------------------------------------- 凭据 / Token
def _decrypt_tokens(credential: StoreCredential | None) -> tuple[str, str]:
    if credential is None:
        return "", ""
    access_token = ""
    refresh_token = ""
    if credential.access_token_cipher:
        try:
            access_token = decrypt_secret(credential.access_token_cipher)
        except Exception:  # noqa: BLE001
            logger.warning("店铺 access_token 解密失败：store=%s", credential.store_id)
    if credential.refresh_token_cipher:
        try:
            refresh_token = decrypt_secret(credential.refresh_token_cipher)
        except Exception:  # noqa: BLE001
            logger.warning("店铺 refresh_token 解密失败：store=%s", credential.store_id)
    return access_token, refresh_token


def _token_is_usable(credential: StoreCredential, access_token: str) -> bool:
    if not access_token or credential.status != CREDENTIAL_STATUS_ACTIVE:
        return False
    return not is_expired(
        credential.access_token_expires_at, utc_now(), TOKEN_REFRESH_SKEW_SECONDS
    )


def mark_store_reauth_required(
    db: Session, tenant_id: int, store_id: int, reason: str
) -> None:
    """凭据失效：标记需要重新授权并暂停依赖该凭据的任务。"""
    now = utc_now()
    store = repo.get_store(db, tenant_id, store_id)
    if store is not None and store.status != STORE_STATUS_DISCONNECTED:
        store.status = STORE_STATUS_REAUTH_REQUIRED
        store.last_error = (reason or "凭据失效，请重新授权")[:500]
        store.updated_at = now
    credential = repo.get_credential(db, tenant_id, store_id)
    if credential is not None and credential.status != CREDENTIAL_STATUS_REVOKED:
        credential.status = CREDENTIAL_STATUS_INVALID
        credential.last_error = (reason or "凭据失效，请重新授权")[:500]
        credential.updated_at = now
    logger.warning("店铺凭据失效，标记重新授权：tenant=%s store=%s", tenant_id, store_id)


def raise_store_api_error(
    db: Session, tenant_id: int, store_id: int, exc: ShopeeApiError
) -> None:
    """统一处理店铺接口错误：鉴权失效标记重新授权，其余原样抛出（由任务决定重试）。"""
    if is_auth_invalid_error_code(exc.error_code) or exc.status_code == 401:
        mark_store_reauth_required(
            db, tenant_id, store_id, f"平台鉴权失败：{exc.error_code or 'unauthorized'}"
        )
        db.commit()
        raise AppError(ErrorCode.STORE_REAUTH_REQUIRED, "店铺凭据已失效，请重新授权") from exc
    raise exc


def ensure_access_token(
    center: Session,
    db: Session,
    tenant_id: int,
    store_id: int,
    *,
    force: bool = False,
) -> str:
    """确保店铺有可用 access token；必要时串行刷新（失败的店铺标记重新授权）。

    刷新成功后提交事务，避免调用方只读场景丢失新令牌。
    """
    store = get_store_or_raise(db, tenant_id, store_id)
    if store.status == STORE_STATUS_DISCONNECTED:
        raise AppError(ErrorCode.STORE_NOT_CONNECTED)
    credential = repo.get_credential(db, tenant_id, store_id)
    access_token, refresh_token = _decrypt_tokens(credential)
    if (
        not force
        and credential is not None
        and _token_is_usable(credential, access_token)
        and store.status == STORE_STATUS_CONNECTED
    ):
        return access_token

    lock_name = f"shopee:token:{tenant_id}:{store_id}"
    try:
        lock_token = acquire_lock(lock_name, ttl_seconds=TOKEN_REFRESH_LOCK_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - 缓存不可用统一转 503
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "缓存服务不可用") from exc
    if lock_token is None:
        # 其他任务正在刷新：重新读取一次，避免重复刷新
        credential = repo.get_credential(db, tenant_id, store_id)
        access_token, _ = _decrypt_tokens(credential)
        if credential is not None and _token_is_usable(credential, access_token):
            return access_token
        raise AppError(ErrorCode.JOB_IN_PROGRESS, "店铺令牌正在刷新，请稍后重试")
    try:
        credential = repo.get_credential(db, tenant_id, store_id)
        access_token, refresh_token = _decrypt_tokens(credential)
        if not force and credential is not None and _token_is_usable(credential, access_token):
            return access_token
        config = require_channel_app(center, store.environment)
        if not refresh_token:
            mark_store_reauth_required(db, tenant_id, store_id, "刷新令牌缺失")
            db.commit()
            raise AppError(ErrorCode.STORE_REAUTH_REQUIRED)
        client = build_client_for_environment(
            environment=store.environment,
            partner_id=int(config.partner_id or 0),
            partner_key=config.partner_key,
            access_token=access_token,
            shop_id=store.external_store_id,
        )
        try:
            raw = client.refresh_access_token(refresh_token)
        finally:
            client.close()
        token = oauth.normalize_token_payload(raw)
        if not token["access_token"]:
            mark_store_reauth_required(db, tenant_id, store_id, "刷新令牌未返回访问令牌")
            db.commit()
            raise AppError(ErrorCode.STORE_REAUTH_REQUIRED)
        access_expires, refresh_expires = oauth.token_expiry_times(token)
        credential = repo.get_credential(db, tenant_id, store_id)
        if credential is None:  # pragma: no cover - 授权写入保证存在
            raise AppError(ErrorCode.STORE_NOT_CONNECTED)
        credential.access_token_cipher = encrypt_secret(token["access_token"])
        if token["refresh_token"]:
            credential.refresh_token_cipher = encrypt_secret(token["refresh_token"])
        credential.access_token_expires_at = access_expires
        credential.refresh_token_expires_at = refresh_expires
        credential.status = CREDENTIAL_STATUS_ACTIVE
        credential.refreshed_at = utc_now()
        credential.last_error = None
        credential.updated_at = utc_now()
        store.status = STORE_STATUS_CONNECTED
        store.last_error = None
        store.updated_at = utc_now()
        db.commit()
        logger.info("店铺令牌刷新成功：tenant=%s store=%s", tenant_id, store_id)
        return token["access_token"]
    except ShopeeApiError as exc:
        if exc.transient:
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "Shopee 接口暂不可用，请稍后重试") from exc
        reason = f"刷新令牌失败：{exc.error_code or 'invalid_token'}"
        mark_store_reauth_required(db, tenant_id, store_id, reason)
        db.commit()
        raise AppError(ErrorCode.STORE_REAUTH_REQUIRED, "店铺凭据已失效，请重新授权") from exc
    finally:
        try:
            release_lock(lock_name, lock_token)
        except Exception:  # noqa: BLE001 - 释放锁失败不影响业务结果
            logger.warning("释放店铺令牌刷新锁失败：%s", lock_name)


def refresh_store_token(
    center: Session, db: Session, tenant_id: int, store_id: int
) -> str:
    """强制刷新店铺令牌（凭据刷新任务 / 管理操作）。"""
    return ensure_access_token(center, db, tenant_id, store_id, force=True)


def build_store_client(
    center: Session,
    db: Session,
    tenant_id: int,
    store: Store,
    *,
    force_refresh: bool = False,
) -> ShopeeClient:
    """用店铺凭据构造 Shopee 客户端（凭据已解密、只存在内存）。"""
    token = ensure_access_token(center, db, tenant_id, store.id, force=force_refresh)
    config = require_channel_app(center, store.environment)
    return build_client_for_environment(
        environment=store.environment,
        partner_id=int(config.partner_id or 0),
        partner_key=config.partner_key,
        access_token=token,
        shop_id=store.external_store_id,
    )


# --------------------------------------------------------------------------- TikTok Shop
def _decrypt_shop_cipher(credential: StoreCredential | None) -> str:
    if credential is None or not credential.shop_cipher_cipher:
        return ""
    try:
        return decrypt_secret(credential.shop_cipher_cipher)
    except Exception:  # noqa: BLE001
        logger.warning("店铺 shop_cipher 解密失败")
        return ""


def _tiktok_error(exc: TikTokApiError) -> AppError:
    if exc.transient:
        return AppError(ErrorCode.SERVICE_UNAVAILABLE, "TikTok 接口暂不可用，请稍后重试")
    return AppError(ErrorCode.STORE_REAUTH_REQUIRED, "TikTok 授权已失效，请重新授权")


def build_tiktok_client(
    center: Session,
    db: Session,
    tenant_id: int,
    store: Store,
    *,
    force_refresh: bool = False,
) -> TikTokClient:
    """构造 TikTok 客户端：凭据已解密，必要时自动刷新 access token。"""
    credential = repo.get_credential(db, tenant_id, store.id)
    access_token, refresh_token = _decrypt_tokens(credential)
    config = require_channel_app(center, store.environment, channel=CHANNEL_TIKTOK)
    shop_cipher = _decrypt_shop_cipher(credential)

    def make_client(token: str) -> TikTokClient:
        return TikTokClient(
            app_key=config.app_key,
            app_secret=config.app_secret,
            access_token=token,
            shop_cipher=shop_cipher,
        )

    if not force_refresh and credential is not None and _token_is_usable(credential, access_token):
        return make_client(access_token)
    if not refresh_token:
        mark_store_reauth_required(db, tenant_id, store.id, "刷新令牌缺失")
        db.commit()
        raise AppError(ErrorCode.STORE_REAUTH_REQUIRED)
    refresh_client = TikTokClient(
        app_key=config.app_key,
        app_secret=config.app_secret,
        access_token=access_token,
        shop_cipher=shop_cipher,
    )
    try:
        raw = refresh_client.refresh_token(refresh_token)
    except TikTokApiError as exc:
        raise _tiktok_error(exc) from exc
    finally:
        refresh_client.close()
    token = normalize_tiktok_token(raw)
    if not token["access_token"]:
        mark_store_reauth_required(db, tenant_id, store.id, "刷新令牌未返回访问令牌")
        db.commit()
        raise AppError(ErrorCode.STORE_REAUTH_REQUIRED)
    credential = repo.get_credential(db, tenant_id, store.id)
    if credential is None:  # pragma: no cover
        raise AppError(ErrorCode.STORE_NOT_CONNECTED)
    access_expires, refresh_expires = tiktok_token_expiry_times(token)
    credential.access_token_cipher = encrypt_secret(token["access_token"])
    if token["refresh_token"]:
        credential.refresh_token_cipher = encrypt_secret(token["refresh_token"])
    credential.access_token_expires_at = access_expires
    credential.refresh_token_expires_at = refresh_expires
    credential.status = CREDENTIAL_STATUS_ACTIVE
    credential.refreshed_at = utc_now()
    credential.last_error = None
    db.commit()
    return make_client(token["access_token"])


def _tiktok_listing_status(product: Mapping[str, Any]) -> str:
    status = str(product.get("status") or "").strip().upper()
    return LISTING_STATUS_ACTIVE if status in {"ACTIVATE", "ACTIVE", "LIVE"} else LISTING_STATUS_INACTIVE


def _tiktok_price(sku: Mapping[str, Any], product: Mapping[str, Any]) -> Decimal | None:
    price = sku.get("price")
    if isinstance(price, Mapping):
        for key in ("sale_price", "original_price", "tax_exclusive_price"):
            value = price.get(key)
            if value is not None:
                parsed = _decimal_price(value)
                if parsed is not None:
                    return parsed
    return _decimal_price(sku.get("price") or product.get("price"))


def _tiktok_stock(sku: Mapping[str, Any]) -> int | None:
    inventory = sku.get("inventory")
    if isinstance(inventory, Sequence):
        total = 0
        found = False
        for item in inventory:
            if isinstance(item, Mapping) and item.get("quantity") is not None:
                total += int(item.get("quantity") or 0)
                found = True
        if found:
            return total
    quantity = sku.get("quantity")
    if quantity is not None:
        try:
            return int(quantity)
        except (TypeError, ValueError):
            return None
    return None


def _tiktok_specs(sku: Mapping[str, Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    attributes = sku.get("sales_attributes") or sku.get("attributes")
    if isinstance(attributes, Sequence):
        for item in attributes:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or item.get("attribute_name") or "").strip()
            value = str(
                item.get("value_name") or item.get("value") or item.get("attribute_value") or ""
            ).strip()
            if name and value:
                specs[name] = value
    return specs


def parse_tiktok_listings(products: Sequence[Mapping[str, Any]]) -> list[RemoteListing]:
    """把 TikTok 商品搜索响应解析为平台商品行（纯逻辑）。"""
    rows: list[RemoteListing] = []
    for product in products:
        if not isinstance(product, Mapping):
            continue
        product_id = normalize_external_id(product.get("id") or product.get("product_id"))
        if not product_id:
            continue
        title = str(product.get("title") or product.get("product_name") or "")[:255]
        status = _tiktok_listing_status(product)
        raw_currency = product.get("currency")
        skus = product.get("skus") or product.get("skus_info") or []
        if not isinstance(skus, Sequence) or not skus:
            skus = [{}]
        for sku in skus:
            if not isinstance(sku, Mapping):
                continue
            # 映射键优先用 seller_sku（与本地 SKU 编码匹配）；API 调用仍使用原始 sku.id
            sku_id = normalize_external_id(
                sku.get("seller_sku") or sku.get("sku_id") or sku.get("id")
            )
            specs = _tiktok_specs(sku)
            rows.append(
                RemoteListing(
                    external_item_id=product_id,
                    external_sku_id=sku_id,
                    title=title,
                    price=_tiktok_price(sku, product),
                    currency=str(raw_currency or "")[:8],
                    remote_stock=_tiktok_stock(sku),
                    status=status,
                    specs=specs,
                    raw={
                        "channel": CHANNEL_TIKTOK,
                        "product": dict(product),
                        "sku": dict(sku),
                        "specs": specs,
                    },
                )
            )
    return rows


def _fetch_tiktok_listings(
    center: Session, db: Session, tenant_id: int, store: Store
) -> list[RemoteListing]:
    client = build_tiktok_client(center, db, tenant_id, store)
    try:
        rows: list[RemoteListing] = []
        page_token = ""
        for _ in range(MAX_ITEM_PAGES):
            payload = client.search_products(page_size=100, page_token=page_token)
            data = payload.get("data") if isinstance(payload, Mapping) else None
            products = data.get("products") if isinstance(data, Mapping) else None
            if not isinstance(products, Sequence):
                products = []
            rows.extend(parse_tiktok_listings(products))
            page_token = str((data or {}).get("next_page_token") or "") if isinstance(data, Mapping) else ""
            if not page_token:
                break
        return rows
    except TikTokApiError as exc:
        raise _tiktok_error(exc) from exc
    finally:
        client.close()


def _fetch_tiktok_orders(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    since: datetime | None,
    cursor: str | None,
    page_size: int,
) -> dict[str, Any]:
    store = get_store_or_raise(db, tenant_id, store_id)
    client = build_tiktok_client(center, db, tenant_id, store)
    try:
        now = utc_now()
        start = since or (now - timedelta(days=1))
        if start.tzinfo is not None:
            start = start.astimezone(timezone.utc).replace(tzinfo=None)
        payload = client.search_orders(
            create_time_ge=int(start.replace(tzinfo=timezone.utc).timestamp()),
            create_time_lt=int(now.replace(tzinfo=timezone.utc).timestamp()),
            page_size=max(1, min(int(page_size), 100)),
            next_page_token=cursor or "",
        )
        data = payload.get("data") if isinstance(payload, Mapping) else None
        orders = data.get("orders") if isinstance(data, Mapping) else None
        if not isinstance(orders, list):
            orders = []
        next_cursor = str((data or {}).get("next_page_token") or "") if isinstance(data, Mapping) else ""
        return {
            "orders": [dict(order) for order in orders if isinstance(order, Mapping)],
            "next_cursor": next_cursor or None,
            "has_more": bool(next_cursor),
        }
    except TikTokApiError as exc:
        raise _tiktok_error(exc) from exc
    finally:
        client.close()


def _push_tiktok_stock(
    center: Session,
    db: Session,
    tenant_id: int,
    store: Store,
    listings: Sequence[ChannelListing],
    stock: int,
) -> dict[str, Any]:
    client = build_tiktok_client(center, db, tenant_id, store)
    try:
        warehouse_id = (store.warehouse_id or "").strip()
        if not warehouse_id:
            warehouses = client.get_warehouses()
            sales = [item for item in warehouses if str(item.get("type") or "") == "SALES_WAREHOUSE"]
            chosen = (sales or warehouses or [{}])[0]
            warehouse_id = str(chosen.get("id") or "")
            if warehouse_id:
                store.warehouse_id = warehouse_id[:64]
        if not warehouse_id:
            raise AppError(ErrorCode.CHANNEL_CALLBACK_UNKNOWN, "TikTok 店铺未返回可用仓库")
        pushed = 0
        for listing in listings:
            raw = listing.raw if isinstance(listing.raw, Mapping) else {}
            raw_sku = raw.get("sku") if isinstance(raw.get("sku"), Mapping) else {}
            api_sku_id = normalize_external_id(
                raw_sku.get("id") or raw_sku.get("sku_id") or listing.external_sku_id
            )
            if not api_sku_id:
                continue
            client.update_inventory(
                listing.external_item_id,
                [{"id": api_sku_id, "inventory": [{"warehouse_id": warehouse_id, "quantity": clamp_stock_quantity(stock)}]}],
            )
            listing.remote_stock = stock
            listing.synced_at = utc_now()
            listing.updated_at = utc_now()
            pushed += 1
        db.commit()
        return {"stock": stock, "pushed": pushed, "skipped": False}
    except TikTokApiError as exc:
        raise _tiktok_error(exc) from exc
    finally:
        client.close()


def list_refresh_targets(
    db: Session, tenant_id: int, *, within_seconds: int = TOKEN_REFRESH_SKEW_SECONDS
) -> list[int]:
    """返回访问令牌即将过期的店铺 ID，供刷新任务按店铺串行处理。"""
    before = utc_now() + timedelta(seconds=max(0, int(within_seconds)))
    credentials = repo.list_credentials_need_refresh(db, tenant_id, before=before)
    return [row.store_id for row in credentials]


# --------------------------------------------------------------------------- 平台商品：匹配（纯逻辑）
def normalize_specs(raw_specs: Any) -> dict[str, str]:
    """把规格统一为 ``{规格名: 规格值}``（去空白、键值小写），便于精确比较。"""
    data: Mapping[str, Any]
    if raw_specs is None:
        return {}
    if isinstance(raw_specs, str):
        try:
            parsed = json.loads(raw_specs)
        except (TypeError, ValueError):
            return {}
        return normalize_specs(parsed)
    if isinstance(raw_specs, Mapping):
        data = raw_specs
    elif isinstance(raw_specs, Sequence) and not isinstance(raw_specs, (str, bytes)):
        result: dict[str, str] = {}
        for item in raw_specs:
            if isinstance(item, Mapping):
                name = str(item.get("name") or item.get("key") or "").strip().lower()
                value = str(item.get("value") or item.get("option") or "").strip().lower()
                if name and value:
                    result[name] = value
        return result
    else:
        return {}
    normalized: dict[str, str] = {}
    for key, value in data.items():
        name = str(key or "").strip().lower()
        option = str(value or "").strip().lower()
        if name and option:
            normalized[name] = option
    return normalized


def build_listing_specs(
    item_info: Mapping[str, Any] | None, model: Mapping[str, Any] | None = None
) -> dict[str, str]:
    """由平台 item 的 ``tier_variation`` + model 的 ``tier_index`` 推导规格键值。

    平台推送 / 商品接口不直接给规格名与值，需要按 tier 顺序映射；无法可靠推导时
    返回空字典（不参与自动匹配，宁可进入待处理也不猜）。
    """
    if not item_info or not model:
        return {}
    tier_variation = item_info.get("tier_variation") or []
    tier_index = model.get("tier_index") or []
    if not isinstance(tier_variation, Sequence) or not isinstance(tier_index, Sequence):
        return {}
    if not tier_variation or not tier_index:
        return {}
    specs: dict[str, str] = {}
    for position, index in enumerate(tier_index):
        if position >= len(tier_variation):
            return {}
        tier = tier_variation[position]
        if not isinstance(tier, Mapping):
            return {}
        options = tier.get("option_list") or []
        try:
            option = options[int(index)]
        except (IndexError, TypeError, ValueError):
            return {}
        if not isinstance(option, Mapping):
            return {}
        name = str(tier.get("name") or "").strip()
        value = str(option.get("option") or "").strip()
        if not name or not value:
            return {}
        specs[name] = value
    return specs


def match_listing_to_skus(
    *,
    external_sku_id: str | None,
    listing_specs: Mapping[str, str] | None,
    candidates: Sequence[SkuCandidate],
) -> SkuMatch:
    """SKU 自动匹配：只接受唯一、明确的结果。

    规则（见 plan.txt §4「SKU 自动匹配只接受唯一、明确结果」）：

    1. 平台 SKU 编码与本地 ``sku_code`` 精确相等，命中 1 条 → mapped；多条 → ambiguous；
    2. 无平台 SKU 编码时，规格键值完全相等且命中 1 条 → mapped；多条 → ambiguous；
    3. 无命中 → pending（进入待处理列表，绝不猜测）。
    """
    code = normalize_sku_code(external_sku_id)
    if code:
        code_matches = [item for item in candidates if item.sku_code == code]
        if len(code_matches) == 1:
            return SkuMatch(
                status=LINK_STATUS_MAPPED,
                sku_id=code_matches[0].sku_id,
                product_id=code_matches[0].product_id,
                reason="SKU 编码精确匹配",
            )
        if len(code_matches) > 1:
            return SkuMatch(
                status=LINK_STATUS_AMBIGUOUS,
                reason=f"SKU 编码 {code} 命中 {len(code_matches)} 条本地 SKU",
            )
    specs = normalize_specs(listing_specs)
    if specs:
        spec_matches = [
            item
            for item in candidates
            if item.specs and normalize_specs(item.specs) == specs
        ]
        if len(spec_matches) == 1:
            return SkuMatch(
                status=LINK_STATUS_MAPPED,
                sku_id=spec_matches[0].sku_id,
                product_id=spec_matches[0].product_id,
                reason="规格键值精确匹配",
            )
        if len(spec_matches) > 1:
            return SkuMatch(
                status=LINK_STATUS_AMBIGUOUS,
                reason=f"规格键值命中 {len(spec_matches)} 条本地 SKU",
            )
    return SkuMatch(status=LINK_STATUS_PENDING, reason="未找到唯一匹配的本地 SKU")


def listing_unique_key(
    store_id: int, external_item_id: str, external_sku_id: str | None
) -> tuple[int, str, str]:
    """平台商品唯一键：无规格商品的 ``external_sku_id`` 归一为空串。"""
    return (
        int(store_id),
        normalize_external_id(external_item_id),
        normalize_external_id(external_sku_id) or NO_EXTERNAL_SKU,
    )


def clamp_stock_quantity(value: Any) -> int:
    """推送平台的可售库存最低为 0（绝不下发负数）。"""
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, quantity)


def extract_listing_model_id(raw: Mapping[str, Any] | None) -> int:
    """从平台原始数据提取 model_id（用于 update_stock）；无规格商品返回 0。"""
    if not raw:
        return 0
    model = raw.get("model")
    if isinstance(model, Mapping):
        return _to_positive_int(model.get("model_id"))
    return _to_positive_int(raw.get("model_id"))


def _to_positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def build_stock_update_payload(
    item_id: int | str, model_id: int, stock: Any
) -> dict[str, Any]:
    """构造 Shopee ``update_stock`` 请求体；model_id 为 0 时不带规格维度。"""
    stock_item: dict[str, Any] = {
        "seller_stock": [{"stock": clamp_stock_quantity(stock)}],
    }
    if int(model_id) > 0:
        stock_item["model_id"] = int(model_id)
    return {"item_id": int(item_id), "stock_list": [stock_item]}


# --------------------------------------------------------------------------- 平台商品：读取 / 同步
def _decimal_price(value: Any) -> Decimal | None:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price >= 0 else None


def _stock_from_payload(payload: Mapping[str, Any]) -> int | None:
    """读取平台商品 / 规格的可售库存快照（兼容 v1 / v2 结构）。"""
    stock_v2 = payload.get("stock_info_v2")
    if isinstance(stock_v2, Mapping):
        summary = stock_v2.get("summary_info")
        if isinstance(summary, Mapping) and summary.get("total_available_stock") is not None:
            return int(summary.get("total_available_stock") or 0)
        seller = stock_v2.get("seller_stock")
        if isinstance(seller, Sequence) and seller and isinstance(seller[0], Mapping):
            if seller[0].get("stock") is not None:
                return int(seller[0].get("stock") or 0)
    stock_v1 = payload.get("stock_info")
    if isinstance(stock_v1, Sequence) and stock_v1 and isinstance(stock_v1[0], Mapping):
        current = stock_v1[0].get("current_stock")
        if current is not None:
            return int(current or 0)
    return None


def _price_from_payload(payload: Mapping[str, Any]) -> Decimal | None:
    price_list = payload.get("price_info")
    if isinstance(price_list, Sequence) and price_list:
        first = price_list[0]
        if isinstance(first, Mapping):
            for key in ("current_price", "original_price"):
                if first.get(key) is not None:
                    return _decimal_price(first.get(key))
    for key in ("price", "price_amount"):
        if payload.get(key) is not None:
            return _decimal_price(payload.get(key))
    return None


def _item_status_value(item_status: Any) -> str:
    status = str(item_status or "NORMAL").strip().upper()
    return LISTING_STATUS_ACTIVE if status == "NORMAL" else LISTING_STATUS_INACTIVE


def parse_remote_listings(
    item_infos: Sequence[Mapping[str, Any]],
    models_by_item: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[RemoteListing]:
    """把平台 item / model 数据解析为平台商品行（纯逻辑，可单测）。"""
    rows: list[RemoteListing] = []
    for item in item_infos:
        item_id = normalize_external_id(item.get("item_id"))
        if not item_id:
            continue
        title = str(item.get("item_name") or "")[:255]
        status = _item_status_value(item.get("item_status"))
        currency = str(item.get("currency") or "")[:8]
        models = models_by_item.get(item_id) or []
        if models:
            for model in models:
                sku = normalize_external_id(model.get("model_sku"))
                rows.append(
                    RemoteListing(
                        external_item_id=item_id,
                        external_sku_id=sku,
                        title=title,
                        price=_price_from_payload(model) or _price_from_payload(item),
                        currency=currency,
                        remote_stock=_stock_from_payload(model),
                        status=status,
                        specs=build_listing_specs(item, model),
                        raw={"item": dict(item), "model": dict(model)},
                    )
                )
        else:
            rows.append(
                RemoteListing(
                    external_item_id=item_id,
                    external_sku_id=normalize_external_id(item.get("item_sku")),
                    title=title,
                    price=_price_from_payload(item),
                    currency=currency,
                    remote_stock=_stock_from_payload(item),
                    status=status,
                    specs={},
                    raw={"item": dict(item)},
                )
            )
    return rows


def attach_item_statuses(
    item_infos: Sequence[Mapping[str, Any]], statuses: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """把 ``get_item_list`` 的商品状态补到 ``get_item_base_info`` 结果里。

    ``get_item_base_info`` 不保证返回 ``item_status``，缺状态时会被误判为在售；
    以列表接口的状态为准，缺失时按 ``NORMAL`` 处理。
    """
    result: list[dict[str, Any]] = []
    for item in item_infos:
        payload = dict(item)
        item_id = normalize_external_id(payload.get("item_id"))
        if item_id and statuses.get(item_id):
            payload.setdefault("item_status", statuses[item_id])
        result.append(payload)
    return result


def fetch_remote_listings(
    center: Session, db: Session, tenant_id: int, store: Store
) -> list[RemoteListing]:
    """读取店铺全部商品与规格（HTTP 请求在事务之外，不在循环里写库）。"""
    if store.channel == CHANNEL_TIKTOK:
        return _fetch_tiktok_listings(center, db, tenant_id, store)
    client = build_store_client(center, db, tenant_id, store)
    try:
        item_statuses: dict[str, str] = {}
        item_ids: list[str] = []
        offset = 0
        for _ in range(MAX_ITEM_PAGES):
            page = client.get_item_list(offset=offset, page_size=ITEM_PAGE_SIZE)
            items = page.get("item") or []
            for item in items:
                if isinstance(item, Mapping):
                    item_id = normalize_external_id(item.get("item_id"))
                    if item_id:
                        item_ids.append(item_id)
                        item_statuses[item_id] = str(item.get("item_status") or "NORMAL")
            if not page.get("has_next_page"):
                break
            offset += ITEM_PAGE_SIZE
        unique_ids = list(dict.fromkeys(item_ids))
        item_infos: list[Mapping[str, Any]] = []
        for start in range(0, len(unique_ids), ITEM_PAGE_SIZE):
            item_infos.extend(client.get_item_base_info(unique_ids[start : start + ITEM_PAGE_SIZE]))
        item_infos = attach_item_statuses(item_infos, item_statuses)
        models_by_item: dict[str, list[Mapping[str, Any]]] = {}
        for item in item_infos:
            item_id = normalize_external_id(item.get("item_id"))
            if not item_id:
                continue
            if item.get("has_model"):
                try:
                    models_by_item[item_id] = client.get_model_list(item_id)
                except ShopeeApiError as exc:
                    logger.warning("读取 Shopee 店铺商品规格失败 item=%s：%s", item_id, exc)
                    models_by_item[item_id] = []
        return parse_remote_listings(item_infos, models_by_item)
    except ShopeeApiError as exc:
        raise_store_api_error(db, tenant_id, store.id, exc)
        raise  # pragma: no cover - raise_store_api_error 必定抛出
    finally:
        client.close()


def _upsert_listing_row(
    db: Session, tenant_id: int, store_id: int, remote: RemoteListing, now: datetime
) -> tuple[ChannelListing, bool]:
    listing = repo.find_listing_by_external(
        db, tenant_id, store_id, remote.external_item_id, remote.external_sku_id
    )
    created = listing is None
    if listing is None:
        listing = ChannelListing(
            tenant_id=tenant_id,
            store_id=store_id,
            external_item_id=remote.external_item_id,
            external_sku_id=remote.external_sku_id,
            title=remote.title,
            price=remote.price,
            currency=remote.currency,
            remote_stock=remote.remote_stock,
            status=remote.status,
            raw=dict(remote.raw),
            synced_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(listing)
        db.flush()
    else:
        listing.title = remote.title or listing.title
        listing.price = remote.price if remote.price is not None else listing.price
        listing.currency = remote.currency or listing.currency
        listing.remote_stock = remote.remote_stock
        listing.status = remote.status
        listing.raw = dict(remote.raw)
        listing.synced_at = now
        listing.updated_at = now
    return listing, created


def _match_and_save_link(
    db: Session,
    tenant_id: int,
    listing: ChannelListing,
    candidates: Sequence[SkuCandidate],
    *,
    member_id: int | None = None,
) -> SkuMatch:
    """自动匹配并写映射；人工映射永不被自动覆盖。"""
    existing = repo.get_link_by_listing(db, tenant_id, listing.id)
    if existing is not None and existing.mapping_source == MAPPING_SOURCE_MANUAL:
        return SkuMatch(
            status=existing.link_status,
            sku_id=existing.sku_id,
            product_id=existing.product_id,
            reason=existing.note or "人工映射，跳过自动匹配",
        )
    raw = listing.raw if isinstance(listing.raw, Mapping) else {}
    item_info = raw.get("item") if isinstance(raw.get("item"), Mapping) else None
    model_info = raw.get("model") if isinstance(raw.get("model"), Mapping) else None
    listing_specs = build_listing_specs(item_info, model_info)
    if not listing_specs and isinstance(raw.get("specs"), Mapping):
        listing_specs = {
            str(key): str(value)
            for key, value in raw["specs"].items()
            if str(key).strip() and str(value).strip()
        }
    match = match_listing_to_skus(
        external_sku_id=listing.external_sku_id,
        listing_specs=listing_specs,
        candidates=candidates,
    )
    if existing is None:
        existing = ChannelListingLink(
            tenant_id=tenant_id,
            listing_id=listing.id,
            link_status=match.status,
            mapping_source=MAPPING_SOURCE_AUTO,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.add(existing)
    existing.product_id = match.product_id
    existing.sku_id = match.sku_id
    existing.link_status = match.status
    existing.mapping_source = MAPPING_SOURCE_AUTO
    existing.note = match.reason[:255]
    existing.updated_at = utc_now()
    if member_id is not None:
        existing.updated_by = member_id
    return match


def find_stale_listings(
    existing: Sequence[Any], remote_keys: set[tuple[int, str, str]]
) -> list[Any]:
    """找出本次同步未再从平台返回的历史平台商品（纯函数）。

    平台已删除 / 下架的商品只标记为 ``inactive``，映射关系（含人工映射）保留，
    由人工确认后再处理，绝不物理删除。
    """
    stale: list[Any] = []
    for row in existing:
        key = listing_unique_key(
            int(getattr(row, "store_id")),
            getattr(row, "external_item_id"),
            getattr(row, "external_sku_id"),
        )
        if key not in remote_keys:
            stale.append(row)
    return stale


def sync_store_listings(
    center: Session,
    db: Session,
    tenant_id: int,
    store_id: int,
    *,
    actor_id: int | None = None,
) -> dict[str, Any]:
    """同步店铺平台商品与规格 → upsert 平台商品 → 自动匹配本地 SKU。"""
    store = get_store_or_raise(db, tenant_id, store_id)
    if store.status == STORE_STATUS_REAUTH_REQUIRED:
        raise AppError(ErrorCode.STORE_REAUTH_REQUIRED)
    if store.status != STORE_STATUS_CONNECTED:
        raise AppError(ErrorCode.STORE_NOT_CONNECTED)
    remote_rows = fetch_remote_listings(center, db, tenant_id, store)
    candidates = load_sku_candidates_optional(db, tenant_id)
    now = utc_now()
    created = updated = mapped = pending = ambiguous = 0
    for remote in remote_rows:
        listing, is_new = _upsert_listing_row(db, tenant_id, store_id, remote, now)
        created += 1 if is_new else 0
        updated += 0 if is_new else 1
        if candidates is None:
            # catalog 表暂不可用：只更新平台商品快照，不覆盖既有映射
            pending += 1
            continue
        match = _match_and_save_link(db, tenant_id, listing, candidates)
        if match.status == LINK_STATUS_MAPPED:
            mapped += 1
        elif match.status == LINK_STATUS_AMBIGUOUS:
            ambiguous += 1
        else:
            pending += 1
    # 远端已删除 / 不再返回的历史商品：标记 inactive（保留映射，不物理删除）
    remote_keys = {
        listing_unique_key(store_id, remote.external_item_id, remote.external_sku_id)
        for remote in remote_rows
    }
    stale_rows = find_stale_listings(
        repo.list_store_listings(db, tenant_id, store_id), remote_keys
    )
    stale = 0
    for stale_row in stale_rows:
        if stale_row.status != LISTING_STATUS_INACTIVE:
            stale += 1
        stale_row.status = LISTING_STATUS_INACTIVE
        stale_row.updated_at = now
    store.last_synced_at = now
    store.updated_at = now
    store.last_error = None
    result = {
        "fetched": len(remote_rows),
        "created": created,
        "updated": updated,
        "mapped": mapped,
        "pending": pending,
        "ambiguous": ambiguous,
        "stale": stale,
        "auto_match_skipped": candidates is None,
    }
    record_audit(
        db,
        tenant_id=tenant_id,
        action="listing.synced",
        resource_type="store",
        resource_id=store_id,
        actor_id=actor_id,
        summary=(
            f"同步平台商品 {result['fetched']} 条，新建 {created}，更新 {updated}，"
            f"自动映射 {mapped}，待处理 {pending}，歧义 {ambiguous}，"
            f"远端删除/下架 {stale}"
        ),
        payload=result,
    )
    record_audit(
        db,
        tenant_id=tenant_id,
        action="listing.synced",
        resource_type="store",
        resource_id=store_id,
        actor_id=actor_id,
        summary=(
            f"同步平台商品 {result['fetched']} 条，新建 {created}，更新 {updated}，"
            f"自动映射 {mapped}，待处理 {pending}，歧义 {ambiguous}"
        ),
        payload=result,
    )
    db.commit()
    return result


# --------------------------------------------------------------------------- 本地 SKU 查询（延迟依赖 catalog 表）
def _rows_from_text(
    db: Session, sql: str, params: dict[str, Any]
) -> list[Mapping[str, Any]] | None:
    """执行只读原生查询；表尚未迁移 / 数据库错误时返回 ``None`` 而不是空列表。"""
    try:
        return list(db.execute(text(sql), params).mappings().all())
    except SQLAlchemyError as exc:
        logger.warning("读取本地 catalog / inventory 数据失败（可能尚未迁移）：%s", exc)
        return None


def _candidates_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[SkuCandidate]:
    candidates: list[SkuCandidate] = []
    for row in rows:
        code = normalize_sku_code(row.get("sku_code"))
        if not code:
            continue
        candidates.append(
            SkuCandidate(
                sku_id=int(row["id"]),
                product_id=int(row["product_id"]),
                sku_code=code,
                specs=normalize_specs(row.get("specs")),
            )
        )
    return candidates


_SKU_CANDIDATES_SQL = (
    "SELECT id, product_id, sku_code, specs FROM product_skus "
    "WHERE tenant_id = :tenant_id AND status = 'active'"
)


def load_sku_candidates(db: Session, tenant_id: int) -> list[SkuCandidate]:
    """读取本租户启用 SKU 作为自动匹配候选（catalog 表未就绪时返回空列表）。"""
    rows = _rows_from_text(db, _SKU_CANDIDATES_SQL, {"tenant_id": tenant_id})
    return _candidates_from_rows(rows or [])


def load_sku_candidates_optional(db: Session, tenant_id: int) -> list[SkuCandidate] | None:
    """读取匹配候选；``None`` 表示 catalog 表不可用（此时不得覆盖既有自动映射）。"""
    rows = _rows_from_text(db, _SKU_CANDIDATES_SQL, {"tenant_id": tenant_id})
    return None if rows is None else _candidates_from_rows(rows)


def load_sku(db: Session, tenant_id: int, sku_id: int) -> Mapping[str, Any] | None:
    """读取本租户 SKU 及其商品状态（不存在返回 ``None``）。"""
    rows = _rows_from_text(
        db,
        "SELECT s.id, s.product_id, s.sku_code, s.status AS sku_status, "
        "p.name AS product_name, p.status AS product_status "
        "FROM product_skus s JOIN products p "
        "ON p.id = s.product_id AND p.tenant_id = s.tenant_id "
        "WHERE s.tenant_id = :tenant_id AND s.id = :sku_id",
        {"tenant_id": tenant_id, "sku_id": sku_id},
    )
    return rows[0] if rows else None


def load_sku_details(
    db: Session, tenant_id: int, sku_ids: Iterable[int]
) -> dict[int, Mapping[str, Any]]:
    """批量读取 SKU 编码 / 商品名（列表展示用；表未就绪时返回空）。"""
    ids = sorted({int(item) for item in sku_ids if item})
    if not ids:
        return {}
    placeholders = ", ".join(f":id_{index}" for index in range(len(ids)))
    params = {"tenant_id": tenant_id}
    params.update({f"id_{index}": value for index, value in enumerate(ids)})
    rows = _rows_from_text(
        db,
        "SELECT s.id, s.sku_code, p.name AS product_name "
        "FROM product_skus s JOIN products p "
        "ON p.id = s.product_id AND p.tenant_id = s.tenant_id "
        f"WHERE s.tenant_id = :tenant_id AND s.id IN ({placeholders})",
        params,
    )
    if not rows:
        return {}
    return {int(row["id"]): row for row in rows}


def read_available_stock(db: Session, tenant_id: int, sku_id: int) -> int | None:
    """读取 SKU 当前可售库存合计（执行时读取最新值，最低 0）。

    返回 ``None`` 表示库存表不可用（调用方应跳过推送而不是把 0 推给平台）。
    """
    rows = _rows_from_text(
        db,
        "SELECT COALESCE(SUM(available_quantity), 0) AS available "
        "FROM inventory_balances WHERE tenant_id = :tenant_id AND sku_id = :sku_id",
        {"tenant_id": tenant_id, "sku_id": sku_id},
    )
    if rows is None:
        return None
    if not rows:
        return 0
    return clamp_stock_quantity(rows[0].get("available"))


# --------------------------------------------------------------------------- 平台商品：列表 / 人工映射
def list_listing_summaries(
    db: Session,
    tenant_id: int,
    *,
    store_ids: frozenset[int] | None = None,
    store_id: int | None = None,
    mapping_status: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """平台商品分页（含映射状态与本地 SKU 编码），列表与总数使用同一过滤。"""
    try:
        rows, total = repo.list_listings_with_links(
            db,
            tenant_id,
            store_ids=store_ids,
            store_id=store_id,
            mapping_status=mapping_status,
            offset=offset,
            limit=limit,
        )
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, str(exc)) from exc
    sku_ids = [
        link.sku_id
        for _, link in rows
        if link is not None and link.sku_id is not None
    ]
    sku_details = load_sku_details(db, tenant_id, sku_ids)
    items: list[dict[str, Any]] = []
    for listing, link in rows:
        details = sku_details.get(int(link.sku_id)) if link and link.sku_id else None
        items.append(
            {
                "id": listing.id,
                "store_id": listing.store_id,
                "external_item_id": listing.external_item_id,
                "external_sku_id": listing.external_sku_id,
                "title": listing.title,
                "price": listing.price,
                "currency": listing.currency,
                "remote_stock": listing.remote_stock,
                "status": listing.status,
                "synced_at": listing.synced_at,
                "link_status": link.link_status if link else None,
                "mapping_source": link.mapping_source if link else None,
                "product_id": link.product_id if link else None,
                "sku_id": link.sku_id if link else None,
                "sku_code": str(details["sku_code"]) if details else None,
                "product_name": str(details["product_name"]) if details else None,
            }
        )
    return items, total


def get_listing_or_raise(db: Session, tenant_id: int, listing_id: int) -> ChannelListing:
    listing = repo.get_listing(db, tenant_id, listing_id)
    if listing is None:
        raise AppError(ErrorCode.NOT_FOUND, "平台商品不存在或不可见")
    return listing


def link_listing(
    db: Session,
    tenant_id: int,
    *,
    listing_id: int,
    product_id: int,
    sku_id: int,
    actor_id: int | None = None,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """人工建立平台商品 → 本地 SKU 映射；SKU 必须属于本租户且商品有效。"""
    listing = get_listing_or_raise(db, tenant_id, listing_id)
    sku = load_sku(db, tenant_id, sku_id)
    if sku is None:
        raise AppError(ErrorCode.SKU_NOT_FOUND, "SKU 不存在或不属于本租户")
    if int(sku["product_id"]) != int(product_id):
        raise AppError(ErrorCode.SKU_NOT_FOUND, "SKU 不属于所选商品")
    if str(sku.get("product_status") or "") == "archived":
        raise AppError(ErrorCode.CONFLICT, "商品已归档，不能建立映射")
    now = utc_now()
    link = repo.get_link_by_listing(db, tenant_id, listing_id)
    if link is None:
        link = ChannelListingLink(
            tenant_id=tenant_id,
            listing_id=listing_id,
            created_by=actor_id,
            created_at=now,
            updated_at=now,
        )
        db.add(link)
    link.product_id = int(product_id)
    link.sku_id = int(sku_id)
    link.link_status = LINK_STATUS_MAPPED
    link.mapping_source = MAPPING_SOURCE_MANUAL
    link.note = "人工映射"
    link.updated_by = actor_id
    link.updated_at = now
    record_audit(
        db,
        tenant_id=tenant_id,
        action="listing.linked",
        resource_type="channel_listing",
        resource_id=listing_id,
        actor_id=actor_id,
        summary=f"平台商品映射到本地 SKU {sku.get('sku_code')}",
        payload={"product_id": int(product_id), "sku_id": int(sku_id)},
        request_id=request_id,
        ip=ip,
    )
    db.commit()
    return {
        "listing_id": listing.id,
        "product_id": int(product_id),
        "sku_id": int(sku_id),
        "link_status": link.link_status,
        "mapping_source": link.mapping_source,
    }


def unlink_listing(
    db: Session,
    tenant_id: int,
    *,
    listing_id: int,
    actor_id: int | None = None,
    request_id: str = "",
    ip: str = "",
) -> dict[str, Any]:
    """解除映射：保留人工标记，避免自动匹配立刻又绑回去。"""
    listing = get_listing_or_raise(db, tenant_id, listing_id)
    link = repo.get_link_by_listing(db, tenant_id, listing_id)
    now = utc_now()
    if link is None:
        link = ChannelListingLink(
            tenant_id=tenant_id,
            listing_id=listing_id,
            created_by=actor_id,
            created_at=now,
            updated_at=now,
        )
        db.add(link)
    link.product_id = None
    link.sku_id = None
    link.link_status = LINK_STATUS_PENDING
    link.mapping_source = MAPPING_SOURCE_MANUAL
    link.note = "人工解除映射"
    link.updated_by = actor_id
    link.updated_at = now
    record_audit(
        db,
        tenant_id=tenant_id,
        action="listing.unlinked",
        resource_type="channel_listing",
        resource_id=listing_id,
        actor_id=actor_id,
        summary="解除平台商品与本地 SKU 的映射",
        payload={"listing_id": listing_id},
        request_id=request_id,
        ip=ip,
    )
    db.commit()
    return {
        "listing_id": listing.id,
        "product_id": None,
        "sku_id": None,
        "link_status": link.link_status,
        "mapping_source": link.mapping_source,
    }


def resolve_listing_sku(
    db: Session,
    tenant_id: int,
    store_id: int,
    external_item_id: str,
    external_sku_id: str,
) -> tuple[int | None, int | None]:
    """按平台商品键解析本地 SKU（供 sales 归一化订单明细使用）。"""
    mapping = resolve_listings_sku(
        db,
        tenant_id,
        store_id,
        [(normalize_external_id(external_item_id), normalize_external_id(external_sku_id))],
    )
    return mapping.get(
        (
            normalize_external_id(external_item_id),
            normalize_external_id(external_sku_id),
        ),
        (None, None),
    )


def resolve_listings_sku(
    db: Session,
    tenant_id: int,
    store_id: int,
    keys: Sequence[tuple[str, str]],
) -> dict[tuple[str, str], tuple[int | None, int | None]]:
    """批量解析平台商品键 → ``(sku_id, product_id)``；只返回已人工 / 自动映射的行。"""
    normalized = {
        (normalize_external_id(item), normalize_external_id(sku)) for item, sku in keys
    }
    if not normalized:
        return {}
    listings = repo.list_store_listings(db, tenant_id, store_id)
    links = repo.list_links_by_listing_ids(db, tenant_id, [row.id for row in listings])
    result: dict[tuple[str, str], tuple[int | None, int | None]] = {}
    for listing in listings:
        key = (listing.external_item_id, listing.external_sku_id)
        if key not in normalized:
            continue
        link = links.get(listing.id)
        if link is not None and link.link_status == LINK_STATUS_MAPPED and link.sku_id:
            result[key] = (int(link.sku_id), int(link.product_id) if link.product_id else None)
    return result


# --------------------------------------------------------------------------- 库存推送
def push_stock_for_sku(
    center: Session,
    db: Session,
    tenant_id: int,
    store_id: int,
    sku_id: int,
    *,
    actor_id: int | None = None,
) -> dict[str, Any]:
    """推送单个 SKU 的库存到店铺（按 store+sku 串行，执行时读取最新库存）。"""
    lock_name = f"shopee:stock:{tenant_id}:{store_id}:{sku_id}"
    try:
        lock_token = acquire_lock(lock_name, ttl_seconds=STOCK_PUSH_LOCK_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 - 缓存不可用统一转 503
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "缓存服务不可用") from exc
    if lock_token is None:
        return {"sku_id": sku_id, "pushed": 0, "skipped": True, "reason": "同一 SKU 推送进行中"}
    try:
        store = get_store_or_raise(db, tenant_id, store_id)
        if store.status == STORE_STATUS_REAUTH_REQUIRED:
            raise AppError(ErrorCode.STORE_REAUTH_REQUIRED)
        if store.status != STORE_STATUS_CONNECTED:
            return {"sku_id": sku_id, "pushed": 0, "skipped": True, "reason": "店铺未连接"}
        listings = repo.list_mapped_listings_for_sku(db, tenant_id, store_id, sku_id)
        if not listings:
            return {"sku_id": sku_id, "pushed": 0, "skipped": True, "reason": "SKU 未映射平台商品"}
        stock = read_available_stock(db, tenant_id, sku_id)
        if stock is None:
            return {"sku_id": sku_id, "pushed": 0, "skipped": True, "reason": "库存数据不可用"}
        if store.channel == CHANNEL_TIKTOK:
            item = _push_tiktok_stock(center, db, tenant_id, store, listings, stock)
            return {"sku_id": sku_id, **item}
        client = build_store_client(center, db, tenant_id, store)
        pushed = 0
        try:
            for listing in listings:
                payload = build_stock_update_payload(
                    listing.external_item_id,
                    extract_listing_model_id(
                        listing.raw if isinstance(listing.raw, Mapping) else None
                    ),
                    stock,
                )
                client.update_stock(listing.external_item_id, payload["stock_list"])
                listing.remote_stock = stock
                listing.synced_at = utc_now()
                listing.updated_at = utc_now()
                pushed += 1
        finally:
            client.close()
        db.commit()
        return {"sku_id": sku_id, "stock": stock, "pushed": pushed, "skipped": False}
    except ShopeeApiError as exc:
        if exc.transient:
            raise AppError(ErrorCode.SERVICE_UNAVAILABLE, "Shopee 接口暂不可用，请稍后重试") from exc
        raise_store_api_error(db, tenant_id, store_id, exc)
        raise  # pragma: no cover - raise_store_api_error 必定抛出
    finally:
        try:
            release_lock(lock_name, lock_token)
        except Exception:  # noqa: BLE001 - 释放锁失败不影响业务结果
            logger.warning("释放库存推送锁失败：%s", lock_name)


def stock_push_sku_ids(
    db: Session, tenant_id: int, store_id: int, sku_ids: Sequence[int] | None = None
) -> list[int]:
    """待推送 SKU 列表（优先复用 sales 的库存推送项构建器，未就绪时回落到映射表）。"""
    if sku_ids is not None:
        return sorted({int(item) for item in sku_ids})
    try:
        from app.modules.sales.service import build_stock_push_items
    except ImportError:
        return repo.list_mapped_sku_ids(db, tenant_id, store_id)
    try:
        items = build_stock_push_items(db, tenant_id=tenant_id, store_id=store_id, sku_ids=None)
    except NotImplementedError:
        return repo.list_mapped_sku_ids(db, tenant_id, store_id)
    except Exception:  # noqa: BLE001 - sales 构建器异常不影响推送兜底
        logger.exception("sales.build_stock_push_items 调用失败，回落到渠道映射表")
        return repo.list_mapped_sku_ids(db, tenant_id, store_id)
    resolved: set[int] = set()
    for item in items or []:
        value = getattr(item, "sku_id", None)
        if value is None and isinstance(item, Mapping):
            value = item.get("sku_id")
        if value:
            resolved.add(int(value))
    return sorted(resolved) or repo.list_mapped_sku_ids(db, tenant_id, store_id)


def run_stock_push(
    center: Session,
    db: Session,
    tenant_id: int,
    store_id: int,
    *,
    sku_ids: Sequence[int] | None = None,
    actor_id: int | None = None,
) -> dict[str, Any]:
    """批量库存推送：按 SKU 升序逐个串行执行（同一 store+sku 不并发）。"""
    targets = stock_push_sku_ids(db, tenant_id, store_id, sku_ids)
    result: dict[str, Any] = {"total": 0, "pushed": 0, "skipped": 0, "failed": 0, "errors": []}
    for sku_id in sorted(set(targets)):
        result["total"] += 1
        try:
            item = push_stock_for_sku(
                center, db, tenant_id, store_id, sku_id, actor_id=actor_id
            )
            if item.get("skipped"):
                result["skipped"] += 1
            elif item.get("pushed"):
                result["pushed"] += 1
        except AppError as exc:
            result["failed"] += 1
            result["errors"].append(
                {"sku_id": sku_id, "code": exc.code.value, "message": exc.message}
            )
            # 凭据失效 / 店铺未连接：后续 SKU 也必然失败，停止整批避免重复打平台
            if exc.code in {
                ErrorCode.STORE_REAUTH_REQUIRED,
                ErrorCode.STORE_NOT_CONNECTED,
                ErrorCode.CHANNEL_APP_NOT_CONFIGURED,
            }:
                break
        except Exception as exc:  # noqa: BLE001 - 单 SKU 失败不阻断整批
            logger.exception("SKU 库存推送失败：tenant=%s store=%s sku=%s", tenant_id, store_id, sku_id)
            result["failed"] += 1
            result["errors"].append({"sku_id": sku_id, "code": "INTERNAL_ERROR", "message": str(exc)})
    return result


# --------------------------------------------------------------------------- 订单读取（供 sales 拉单）
def fetch_orders(
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    since: datetime | None = None,
    cursor: str | None = None,
    page_size: int = 50,
    center: Session | None = None,
) -> dict[str, Any]:
    """读取店铺订单原始明细（只读远端，不写库），供 sales 归一化入库。"""
    from app.db.session import center_session as _center_session

    if center is not None:
        return _fetch_orders(
            center, db, tenant_id=tenant_id, store_id=store_id, since=since, cursor=cursor,
            page_size=page_size,
        )
    with _center_session() as session:
        return _fetch_orders(
            session, db, tenant_id=tenant_id, store_id=store_id, since=since, cursor=cursor,
            page_size=page_size,
        )


def _fetch_orders(
    center: Session,
    db: Session,
    *,
    tenant_id: int,
    store_id: int,
    since: datetime | None,
    cursor: str | None,
    page_size: int,
) -> dict[str, Any]:
    store = get_store_or_raise(db, tenant_id, store_id)
    if store.status == STORE_STATUS_REAUTH_REQUIRED:
        raise AppError(ErrorCode.STORE_REAUTH_REQUIRED)
    if store.status != STORE_STATUS_CONNECTED:
        raise AppError(ErrorCode.STORE_NOT_CONNECTED)
    if store.channel == CHANNEL_TIKTOK:
        return _fetch_tiktok_orders(
            center,
            db,
            tenant_id=tenant_id,
            store_id=store_id,
            since=since,
            cursor=cursor,
            page_size=page_size,
        )
    client = build_store_client(center, db, tenant_id, store)
    try:
        now = utc_now()
        start = since or (now - timedelta(days=1))
        if start.tzinfo is not None:
            start = start.astimezone(timezone.utc).replace(tzinfo=None)
        time_from = int(start.replace(tzinfo=timezone.utc).timestamp())
        time_to = int(now.replace(tzinfo=timezone.utc).timestamp())
        page = client.get_order_list(
            time_from=time_from,
            time_to=time_to,
            page_size=max(1, min(int(page_size), 50)),
            cursor=cursor or "",
        )
        order_numbers = [
            str(item.get("order_sn") or "")
            for item in (page.get("order_list") or [])
            if isinstance(item, Mapping) and item.get("order_sn")
        ]
        orders: list[dict[str, Any]] = []
        for start_index in range(0, len(order_numbers), 50):
            orders.extend(client.get_order_detail(order_numbers[start_index : start_index + 50]))
        next_cursor = str(page.get("next_cursor") or "") or None
        has_more = bool(page.get("more"))
        return {"orders": orders, "next_cursor": next_cursor, "has_more": has_more}
    except ShopeeApiError as exc:
        raise_store_api_error(db, tenant_id, store.id, exc)
        raise  # pragma: no cover - raise_store_api_error 必定抛出
    finally:
        client.close()


# --------------------------------------------------------------------------- Webhook
def webhook_event_type(payload: Mapping[str, Any] | None) -> str:
    """识别 Shopee 推送类型（参考 ventasync 的 code / ordersn 判别规则）。"""
    data = (payload or {}).get("data")
    data = data if isinstance(data, Mapping) else {}
    code = (payload or {}).get("code")
    if data.get("item_id") is not None and data.get("item_status") is not None:
        return "item_status"
    if str(code) == "4" and data.get("ordersn"):
        return "order_tracking"
    if str(code) == "29":
        return "return_update"
    if data.get("ordersn"):
        return "order_status"
    if code is not None:
        return f"code_{code}"
    return "unknown"


def webhook_dedupe_key(payload: Mapping[str, Any] | None) -> str:
    """推送事件防重键：同店铺同业务状态重复推送只处理一次（不含时间戳）。"""
    data = (payload or {}).get("data")
    data = data if isinstance(data, Mapping) else {}
    business_fields = {
        key: data.get(key)
        for key in (
            "ordersn",
            "order_status",
            "status",
            "return_sn",
            "return_status",
            "item_id",
            "item_status",
        )
        if data.get(key) is not None
    }
    canonical = {
        "channel": CHANNEL_SHOPEE,
        "code": (payload or {}).get("code"),
        "shop_id": (payload or {}).get("shop_id"),
        "data": business_fields,
    }
    raw = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def webhook_candidate_urls(request_url: str) -> list[str]:
    """推送验签可能的回调地址写法（含 http/https 互换、去掉查询串）。"""
    urls: list[str] = []
    for url in (request_url, request_url.split("?")[0]):
        if not url:
            continue
        urls.append(url)
        if url.startswith("http://"):
            urls.append("https://" + url[len("http://") :])
        elif url.startswith("https://"):
            urls.append("http://" + url[len("https://") :])
    return list(dict.fromkeys(urls))


def verify_shopee_webhook(
    config: ChannelAppConfig,
    *,
    request_url: str,
    raw_body: bytes,
    received_signature: str,
) -> bool:
    """用平台应用密钥校验推送签名（推送密钥优先，缺省回退合作方密钥）。"""
    secrets = [item for item in (config.partner_secret, config.partner_key) if item]
    for secret in secrets:
        if verify_webhook_signature(
            secret, webhook_candidate_urls(request_url), raw_body, received_signature
        ):
            return True
    return False


def resolve_webhook_store(
    center: Session, *, shop_id: str
) -> WebhookContext | None:
    """按推送里的 shop_id 从中心库归属登记定位租户 / 店铺（不信任请求体租户）。"""
    external_id = normalize_external_id(shop_id)
    if not external_id:
        return None
    entries = repo.list_registry_by_external(center, CHANNEL_SHOPEE, external_id)
    for entry in entries:
        if entry.status != "bound" or not entry.tenant_id or not entry.store_id:
            continue
        config = load_channel_app(center, CHANNEL_SHOPEE, entry.environment)
        if config is None or not config.configured:
            continue
        return WebhookContext(
            tenant_id=int(entry.tenant_id),
            store_id=int(entry.store_id),
            environment=entry.environment,
            app=config,
        )
    return None


# --------------------------------------------------------------------------- Webhook 补偿（纯逻辑）
def webhook_event_due(
    received_at: datetime | None,
    *,
    now: datetime | None = None,
    min_age_seconds: int = WEBHOOK_COMPENSATION_MIN_AGE_SECONDS,
) -> bool:
    """事件是否已超过「未处理宽限时间」，可以进入补偿（纯函数）。"""
    if received_at is None:
        return False
    current = now or utc_now()
    if received_at.tzinfo is not None:
        received_at = received_at.astimezone(timezone.utc).replace(tzinfo=None)
    cutoff = current - timedelta(seconds=max(0, int(min_age_seconds)))
    return received_at <= cutoff


def compensation_meta(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """读取事件 payload 中的补偿元数据（无则返回空字典）。"""
    if not isinstance(payload, Mapping):
        return {}
    meta = payload.get(WEBHOOK_COMPENSATION_META_KEY)
    return dict(meta) if isinstance(meta, Mapping) else {}


def plan_compensation_attempt(
    attempts: Any,
    error: str = "",
    *,
    max_attempts: int = WEBHOOK_COMPENSATION_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """计算下一次补偿结果：返回新的 attempts、是否达到上限及目标状态。"""
    current = 0
    try:
        current = max(0, int(attempts or 0))
    except (TypeError, ValueError):
        current = 0
    next_attempts = current + 1
    limit = max(1, int(max_attempts))
    failed = next_attempts >= limit
    return {
        "attempts": next_attempts,
        "failed": failed,
        "status": "failed" if failed else "received",
        "last_error": (error or "")[:500],
    }


def merge_compensation_meta(
    payload: Mapping[str, Any] | None,
    *,
    attempts: int,
    last_error: str = "",
    attempted_at: datetime | None = None,
    last_result: str = "",
) -> dict[str, Any]:
    """把补偿元数据合并回事件 payload（保留原始业务字段，不新增表列）。"""
    base: dict[str, Any] = dict(payload) if isinstance(payload, Mapping) else {}
    meta = compensation_meta(base)
    meta["attempts"] = max(0, int(attempts))
    meta["last_error"] = (last_error or "")[:500]
    meta["last_attempt_at"] = (attempted_at or utc_now()).isoformat()
    if last_result:
        meta["last_result"] = last_result
    base[WEBHOOK_COMPENSATION_META_KEY] = meta
    return base


def record_webhook_event(
    db: Session,
    tenant_id: int,
    *,
    store_id: int,
    event_type: str,
    external_event_id: str,
    payload: Mapping[str, Any],
    signature_ok: bool = True,
) -> tuple[int | None, bool]:
    """写入 ``webhook_events`` 并防重；返回 ``(event_id, 是否重复)``。

    优先调用 tasks 模块的 ``record_webhook_event``；未实现时回落到模型写入，
    两者都不可用时只记录日志（回调仍返回 200，由平台重试机制兜底）。
    """
    try:
        from app.modules.tasks.service import record_webhook_event as tasks_record

        event_id, duplicate = tasks_record(
            db,
            tenant_id=tenant_id,
            channel=CHANNEL_SHOPEE,
            store_id=store_id,
            event_type=event_type,
            external_event_id=external_event_id,
            signature_ok=signature_ok,
            payload=dict(payload),
        )
        return int(event_id), bool(duplicate)
    except (ImportError, NotImplementedError):
        pass
    except TypeError as exc:
        logger.warning("tasks.service.record_webhook_event 签名不匹配，回落到模型写入：%s", exc)
    except Exception:  # noqa: BLE001 - 回调不因任务模块异常而丢失
        logger.exception("写 webhook_events 失败（tasks.service）")
        db.rollback()
        return None, False
    try:
        from app.modules.tasks.models_tenant import WebhookEvent
    except (ImportError, AttributeError):
        logger.warning("webhook_events 模型尚未就绪，跳过事件落库")
        return None, False
    try:
        existing = db.execute(
            select(WebhookEvent).where(
                WebhookEvent.tenant_id == tenant_id,
                WebhookEvent.channel == CHANNEL_SHOPEE,
                WebhookEvent.external_event_id == external_event_id,
            )
        ).scalars().first()
        if existing is not None:
            return int(existing.id), True
        event = WebhookEvent(
            tenant_id=tenant_id,
            channel=CHANNEL_SHOPEE,
            store_id=store_id,
            event_type=event_type,
            external_event_id=external_event_id,
            signature_ok=signature_ok,
            status="received",
            payload=dict(payload),
            received_at=utc_now(),
        )
        db.add(event)
        db.flush()
        return int(event.id), False
    except Exception:  # noqa: BLE001 - 表未迁移 / 字段不一致时不阻塞回调
        logger.exception("写 webhook_events 失败（模型回落）")
        db.rollback()
        return None, False


def enqueue_order_sync(
    db: Session, tenant_id: int, store_id: int, *, triggered_by: str = "webhook"
) -> int | None:
    """验签 + 防重后触发订单同步（sales 未实现时记录日志，不崩溃）。"""
    try:
        from app.modules.sales.service import enqueue_order_sync as sales_enqueue
    except ImportError:
        logger.warning("sales 模块未就绪，跳过订单同步触发")
        return None
    try:
        result = sales_enqueue(
            db, tenant_id=tenant_id, store_id=store_id, triggered_by=triggered_by
        )
        return int(result) if result else None
    except NotImplementedError:
        logger.warning("sales.service.enqueue_order_sync 尚未实现，跳过订单同步触发")
        return None


# --------------------------------------------------------------------------- 权限范围工具
def allowed_store_ids(
    effective: Any, permission: str = "store.read"
) -> frozenset[int] | None:
    """从 ``EffectivePermissions`` 计算店铺范围：``None`` 表示全部，空集表示无授权。"""
    if effective is None or not hasattr(effective, "allowed_ids"):
        return frozenset()
    return effective.allowed_ids(permission, SCOPE_STORES)


def store_in_scope(store_id: int, allowed: frozenset[int] | None) -> bool:
    """判断店铺是否在数据范围内（``None`` 表示全部）。"""
    return allowed is None or int(store_id) in allowed
