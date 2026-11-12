"""渠道模块仓储层。

职责边界（docs/architecture.md）：

* 只做 SQLAlchemy 查询与模型读写，不写业务规则、不开启 / 提交事务；
* 所有租户库查询必须显式带 ``tenant_id`` 条件；
* 事务由 Service 控制（一个业务用例一个事务）。

实现负责人：backend-channels。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.modules.channels.constants import (
    LINK_STATUSES,
    LINK_STATUS_MAPPED,
    LISTING_FILTER_UNMAPPED,
    STORE_STATUS_CONNECTED,
)
from app.modules.channels.models_center import ChannelAccountRegistry, PlatformChannelApp
from app.modules.channels.models_tenant import (
    ChannelListing,
    ChannelListingLink,
    Store,
    StoreCredential,
)

# --------------------------------------------------------------------------- 中心库


def get_channel_app(center: Session, channel: str, environment: str) -> PlatformChannelApp | None:
    """按渠道 + 环境读取平台应用配置。"""
    stmt = select(PlatformChannelApp).where(
        PlatformChannelApp.channel == channel,
        PlatformChannelApp.environment == environment,
    )
    return center.execute(stmt).scalar_one_or_none()


def list_channel_apps(center: Session, channel: str | None = None) -> list[PlatformChannelApp]:
    """读取渠道应用配置列表（平台管理页用）。"""
    stmt: Select = select(PlatformChannelApp)
    if channel:
        stmt = stmt.where(PlatformChannelApp.channel == channel)
    stmt = stmt.order_by(PlatformChannelApp.channel, PlatformChannelApp.environment)
    return list(center.execute(stmt).scalars().all())


def get_registry_entry(
    center: Session, channel: str, environment: str, external_account_id: str
) -> ChannelAccountRegistry | None:
    """按渠道 + 环境 + 外部账号读取归属登记。"""
    stmt = select(ChannelAccountRegistry).where(
        ChannelAccountRegistry.channel == channel,
        ChannelAccountRegistry.environment == environment,
        ChannelAccountRegistry.external_account_id == external_account_id,
    )
    return center.execute(stmt).scalar_one_or_none()


def list_registry_by_external(
    center: Session, channel: str, external_account_id: str
) -> list[ChannelAccountRegistry]:
    """按外部账号跨环境查找归属（推送回调只有 shop_id，需自行判断环境）。"""
    stmt = (
        select(ChannelAccountRegistry)
        .where(
            ChannelAccountRegistry.channel == channel,
            ChannelAccountRegistry.external_account_id == external_account_id,
        )
        .order_by(ChannelAccountRegistry.id)
    )
    return list(center.execute(stmt).scalars().all())


def get_registry_for_store(
    center: Session, channel: str, tenant_id: int, store_id: int
) -> ChannelAccountRegistry | None:
    """按租户 + 店铺读取归属登记（断开授权时释放）。"""
    stmt = (
        select(ChannelAccountRegistry)
        .where(
            ChannelAccountRegistry.channel == channel,
            ChannelAccountRegistry.tenant_id == tenant_id,
            ChannelAccountRegistry.store_id == store_id,
        )
        .order_by(ChannelAccountRegistry.id)
    )
    return center.execute(stmt).scalars().first()


# --------------------------------------------------------------------------- 店铺


def get_store(db: Session, tenant_id: int, store_id: int) -> Store | None:
    stmt = select(Store).where(Store.tenant_id == tenant_id, Store.id == store_id)
    return db.execute(stmt).scalar_one_or_none()


def find_store_by_external(
    db: Session, tenant_id: int, channel: str, external_store_id: str
) -> Store | None:
    stmt = select(Store).where(
        Store.tenant_id == tenant_id,
        Store.channel == channel,
        Store.external_store_id == external_store_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def find_store_by_code(
    db: Session, tenant_id: int, channel: str, store_code: str
) -> Store | None:
    stmt = select(Store).where(
        Store.tenant_id == tenant_id,
        Store.channel == channel,
        Store.store_code == store_code,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_stores(
    db: Session,
    tenant_id: int,
    *,
    store_ids: frozenset[int] | None = None,
    offset: int = 0,
    limit: int = 20,
) -> list[Store]:
    stmt = select(Store).where(Store.tenant_id == tenant_id)
    if store_ids is not None:
        if not store_ids:
            return []
        stmt = stmt.where(Store.id.in_(sorted(store_ids)))
    stmt = stmt.order_by(Store.id.asc()).offset(max(0, offset)).limit(max(1, limit))
    return list(db.execute(stmt).scalars().all())


def list_stores_all(
    db: Session, tenant_id: int, *, status: str | None = None
) -> list[Store]:
    """读取租户全部店铺（任务批量处理用，不受数据范围限制）。"""
    stmt = select(Store).where(Store.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(Store.status == status)
    stmt = stmt.order_by(Store.id.asc())
    return list(db.execute(stmt).scalars().all())


def list_stores_for_refresh(db: Session, tenant_id: int) -> list[Store]:
    """需要续期令牌的店铺：已连接或已标记需要重新授权。"""
    stmt = (
        select(Store)
        .where(
            Store.tenant_id == tenant_id,
            Store.status.in_([STORE_STATUS_CONNECTED, "reauth_required"]),
        )
        .order_by(Store.id.asc())
    )
    return list(db.execute(stmt).scalars().all())


def count_stores(db: Session, tenant_id: int, store_ids: frozenset[int] | None = None) -> int:
    stmt = select(func.count()).select_from(Store).where(Store.tenant_id == tenant_id)
    if store_ids is not None:
        if not store_ids:
            return 0
        stmt = stmt.where(Store.id.in_(sorted(store_ids)))
    return int(db.execute(stmt).scalar_one())


# --------------------------------------------------------------------------- 凭据


def get_credential(db: Session, tenant_id: int, store_id: int) -> StoreCredential | None:
    stmt = select(StoreCredential).where(
        StoreCredential.tenant_id == tenant_id,
        StoreCredential.store_id == store_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_credentials(
    db: Session, tenant_id: int, store_ids: list[int]
) -> dict[int, StoreCredential]:
    if not store_ids:
        return {}
    stmt = select(StoreCredential).where(
        StoreCredential.tenant_id == tenant_id,
        StoreCredential.store_id.in_(store_ids),
    )
    return {row.store_id: row for row in db.execute(stmt).scalars().all()}


def list_credentials_need_refresh(
    db: Session, tenant_id: int, *, before: datetime
) -> list[StoreCredential]:
    """访问令牌将在 ``before`` 之前过期（或已过期）的凭据。"""
    stmt = (
        select(StoreCredential)
        .where(
            StoreCredential.tenant_id == tenant_id,
            StoreCredential.status.in_(["active", "expired"]),
            StoreCredential.refresh_token_cipher != "",
            (StoreCredential.access_token_expires_at.is_(None))
            | (StoreCredential.access_token_expires_at <= before),
        )
        .order_by(StoreCredential.store_id.asc())
    )
    return list(db.execute(stmt).scalars().all())


# --------------------------------------------------------------------------- 平台商品


def get_listing(db: Session, tenant_id: int, listing_id: int) -> ChannelListing | None:
    stmt = select(ChannelListing).where(
        ChannelListing.tenant_id == tenant_id, ChannelListing.id == listing_id
    )
    return db.execute(stmt).scalar_one_or_none()


def find_listing_by_external(
    db: Session,
    tenant_id: int,
    store_id: int,
    external_item_id: str,
    external_sku_id: str,
) -> ChannelListing | None:
    stmt = select(ChannelListing).where(
        ChannelListing.tenant_id == tenant_id,
        ChannelListing.store_id == store_id,
        ChannelListing.external_item_id == external_item_id,
        ChannelListing.external_sku_id == external_sku_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def _listing_filters(
    tenant_id: int,
    *,
    store_ids: frozenset[int] | None = None,
    store_id: int | None = None,
    mapping_status: str | None = None,
) -> list:
    conditions: list = [ChannelListing.tenant_id == tenant_id]
    if store_id is not None:
        conditions.append(ChannelListing.store_id == store_id)
    elif store_ids is not None:
        if not store_ids:
            return [ChannelListing.tenant_id == tenant_id, ChannelListing.id == -1]
        conditions.append(ChannelListing.store_id.in_(sorted(store_ids)))
    if mapping_status == LISTING_FILTER_UNMAPPED:
        conditions.append(ChannelListingLink.id.is_(None))
    elif mapping_status:
        if mapping_status not in LINK_STATUSES:
            raise ValueError(f"未知映射状态: {mapping_status}")
        conditions.append(ChannelListingLink.link_status == mapping_status)
    return conditions


def list_listings_with_links(
    db: Session,
    tenant_id: int,
    *,
    store_ids: frozenset[int] | None = None,
    store_id: int | None = None,
    mapping_status: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[tuple[ChannelListing, ChannelListingLink | None]], int]:
    """平台商品分页（左连接映射表），客户列表与总数使用同一过滤条件。"""
    conditions = _listing_filters(
        tenant_id, store_ids=store_ids, store_id=store_id, mapping_status=mapping_status
    )
    link_join = and_(
        ChannelListingLink.listing_id == ChannelListing.id,
        ChannelListingLink.tenant_id == ChannelListing.tenant_id,
    )
    rows_stmt = (
        select(ChannelListing, ChannelListingLink)
        .outerjoin(ChannelListingLink, link_join)
        .where(*conditions)
        .order_by(ChannelListing.id.desc())
        .offset(max(0, offset))
        .limit(max(1, limit))
    )
    count_stmt = (
        select(func.count())
        .select_from(ChannelListing)
        .outerjoin(ChannelListingLink, link_join)
        .where(*conditions)
    )
    rows = [
        (row[0], row[1])
        for row in db.execute(rows_stmt).all()
    ]
    total = int(db.execute(count_stmt).scalar_one())
    return rows, total


def get_link_by_listing(db: Session, tenant_id: int, listing_id: int) -> ChannelListingLink | None:
    stmt = select(ChannelListingLink).where(
        ChannelListingLink.tenant_id == tenant_id,
        ChannelListingLink.listing_id == listing_id,
    )
    return db.execute(stmt).scalar_one_or_none()


def list_links_by_listing_ids(
    db: Session, tenant_id: int, listing_ids: list[int]
) -> dict[int, ChannelListingLink]:
    if not listing_ids:
        return {}
    stmt = select(ChannelListingLink).where(
        ChannelListingLink.tenant_id == tenant_id,
        ChannelListingLink.listing_id.in_(listing_ids),
    )
    return {row.listing_id: row for row in db.execute(stmt).scalars().all()}


def list_mapped_listings_for_sku(
    db: Session, tenant_id: int, store_id: int, sku_id: int
) -> list[ChannelListing]:
    """某店铺内已映射到指定本地 SKU 的平台商品（库存推送用）。"""
    link_join = and_(
        ChannelListingLink.listing_id == ChannelListing.id,
        ChannelListingLink.tenant_id == ChannelListing.tenant_id,
    )
    stmt = (
        select(ChannelListing)
        .join(ChannelListingLink, link_join)
        .where(
            ChannelListing.tenant_id == tenant_id,
            ChannelListing.store_id == store_id,
            ChannelListingLink.sku_id == sku_id,
            ChannelListingLink.link_status == LINK_STATUS_MAPPED,
        )
        .order_by(ChannelListing.external_item_id, ChannelListing.external_sku_id)
    )
    return list(db.execute(stmt).scalars().all())


def list_store_listings(
    db: Session, tenant_id: int, store_id: int
) -> list[ChannelListing]:
    """读取某店铺的全部平台商品（订单明细映射解析用）。"""
    stmt = (
        select(ChannelListing)
        .where(
            ChannelListing.tenant_id == tenant_id,
            ChannelListing.store_id == store_id,
        )
        .order_by(ChannelListing.id.asc())
    )
    return list(db.execute(stmt).scalars().all())


def list_mapped_sku_ids(db: Session, tenant_id: int, store_id: int) -> list[int]:
    """店铺内已映射的本地 SKU 去重列表（批量库存推送用）。"""
    stmt = (
        select(ChannelListingLink.sku_id)
        .join(
            ChannelListing,
            and_(
                ChannelListingLink.listing_id == ChannelListing.id,
                ChannelListingLink.tenant_id == ChannelListing.tenant_id,
            ),
        )
        .where(
            ChannelListingLink.tenant_id == tenant_id,
            ChannelListing.store_id == store_id,
            ChannelListingLink.link_status == LINK_STATUS_MAPPED,
            ChannelListingLink.sku_id.is_not(None),
        )
        .distinct()
        .order_by(ChannelListingLink.sku_id.asc())
    )
    return [int(row) for row in db.execute(stmt).scalars().all() if row is not None]


def count_listings_by_store(db: Session, tenant_id: int, store_id: int) -> int:
    stmt = (
        select(func.count())
        .select_from(ChannelListing)
        .where(ChannelListing.tenant_id == tenant_id, ChannelListing.store_id == store_id)
    )
    return int(db.execute(stmt).scalar_one())
