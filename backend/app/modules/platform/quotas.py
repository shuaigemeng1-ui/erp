"""套餐额度门禁：锁定同库配额锚点后检查，再由业务事务完成资源写入。

额度读取当前中心库配置，不缓存；NULL / 未配置额度表示不限制。
库存、订单等既有业务不因套餐降级被删除。归档记录仍占用存储资源额度。
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.core.security import utc_now
from app.db.session import center_session
from app.modules.access.models_tenant import TenantSettings
from app.modules.identity.models_center import Tenant, TenantMembership
from app.modules.platform.models_center import PlanFeature, Subscription


def current_limits(
    center: Session, tenant_id: int, *, lock_tenant: bool = True,
) -> dict[str, int | None]:
    """有效订阅优先；无订阅使用租户套餐，无套餐明确视为历史兼容无限制。"""
    tenant_query = select(Tenant).where(Tenant.id == tenant_id)
    if lock_tenant:
        tenant_query = tenant_query.with_for_update()
    tenant = center.execute(tenant_query.execution_options(populate_existing=True)).scalar_one_or_none()
    if tenant is None:
        raise AppError(ErrorCode.TENANT_NOT_FOUND)
    now = utc_now()
    subscription_query = select(Subscription).where(
            Subscription.tenant_id == tenant_id,
            Subscription.status == 'active', Subscription.starts_at <= now,
            or_(Subscription.ends_at.is_(None), Subscription.ends_at > now),
        ).order_by(Subscription.starts_at.desc(), Subscription.id.desc()).limit(1)
    if lock_tenant:
        subscription_query = subscription_query.with_for_update()
    subscription = center.execute(subscription_query).scalar_one_or_none()
    plan_id = subscription.plan_id if subscription is not None else tenant.plan_id
    if plan_id is None:
        return {}
    feature_query = select(PlanFeature.feature_code, PlanFeature.limit_value).where(PlanFeature.plan_id == plan_id)
    if lock_tenant:
        feature_query = feature_query.with_for_update()
    return {
        str(code): int(value) if value is not None else None
        for code, value in center.execute(feature_query).all()
    }


def _check(limits: Mapping[str, int | None], feature: str, used: int, added: int) -> None:
    maximum = limits.get(feature)
    if added > 0 and maximum is not None and used + added > maximum:
        raise AppError(
            ErrorCode.QUOTA_EXCEEDED, '已达到套餐额度，请联系平台管理员调整套餐',
            details={'feature': feature, 'limit': maximum, 'used': used, 'requested': added},
        )


def ensure_member_capacity(center: Session, tenant_id: int, user_id: int) -> None:
    """邀请和正常成员共占名额；已占名额的成员接受邀请不重复计数。"""
    limits = current_limits(center, tenant_id)
    rows = center.execute(
        select(TenantMembership.user_id).where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.status.in_(['invited', 'active']),
        ).with_for_update()
    ).scalars().all()
    _check(limits, 'max_members', len(rows), 0 if user_id in rows else 1)


def ensure_tenant_capacity(db: Session, tenant_id: int, additions: Mapping[str, int]) -> None:
    """租户资源创建与额度检查共享 TenantSettings 行锁，不单独提交事务。"""
    from app.modules.catalog.models_tenant import Product, ProductSku
    from app.modules.channels.models_tenant import Store

    models = {'max_products': Product, 'max_skus': ProductSku, 'max_stores': Store}
    requested = {key: int(value) for key, value in additions.items() if int(value) > 0}
    if not requested:
        return
    if requested.keys() - models.keys():
        raise ValueError('Unknown tenant quota')
    anchor = db.execute(
        select(TenantSettings).where(TenantSettings.tenant_id == tenant_id).with_for_update()
    ).scalar_one_or_none()
    if anchor is None:
        raise AppError(ErrorCode.TENANT_DATABASE_UNAVAILABLE, '租户设置未初始化')
    with center_session() as center:
        # 这里只读配置；不能在持有租户锁时再锁中心租户行，避免跨库锁顺序倒置。
        limits = current_limits(center, tenant_id, lock_tenant=False)
    for feature, added in requested.items():
        if limits.get(feature) is None:
            continue
        model = models[feature]
        # 锁定读取返回当前已提交值，避免 MySQL REPEATABLE READ 的旧快照少计资源。
        used = len(db.execute(
            select(model.id).where(model.tenant_id == tenant_id).with_for_update()
        ).scalars().all())
        _check(limits, feature, used, added)
