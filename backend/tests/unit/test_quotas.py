"""真实业务创建路径的额度回归，使用中心/租户两套内存数据库。"""

from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.access import EffectivePermissions, ScopeGrant
from app.core.errors import AppError, ErrorCode
from app.core.permissions import ALL_PERMISSIONS
from app.core.security import utc_now
from app.db.base import TenantBase
from app.modules.access.models_tenant import TenantSettings
from app.modules.catalog import service as catalog
from app.modules.catalog.models_tenant import Product, ProductSku
from app.modules.catalog.schemas import ProductCreate, ProductSkuCreate, ProductSkuUpdate, ProductUpdate
from app.modules.channels import service as channels
from app.modules.channels.models_tenant import Store
from app.modules.identity import repository, service as identity
from app.modules.platform import quotas
from app.modules.platform.models_center import Plan, PlanFeature, Subscription
from tests.unit.test_identity import _build_sqlite_sessions, _seed_tenant, _sqlite_metadata


@pytest.fixture
def data(monkeypatch):
    cf, tf, ce, te = _build_sqlite_sessions()
    _sqlite_metadata(TenantBase.metadata, ('products', 'product_skus', 'product_images', 'stores')).create_all(te)
    center, db = cf(), tf()
    roles = _seed_tenant(center, db)
    tenant = repository.get_tenant(center, 1)
    plan = Plan(code='limited', name='Limited', status='active', is_default=False)
    center.add(plan)
    center.flush()
    tenant.plan_id = plan.id
    for code, value in {'max_members': 2, 'max_stores': 1, 'max_products': 1, 'max_skus': 1}.items():
        center.add(PlanFeature(plan_id=plan.id, feature_code=code, limit_value=value))
    center.commit()
    db.add(TenantSettings(tenant_id=1, permission_version=1, timezone='UTC'))
    db.commit()

    @contextmanager
    def center_stub():
        yield center

    monkeypatch.setattr(quotas, 'center_session', center_stub)
    actor = EffectivePermissions(grants=(ScopeGrant(role_binding_id=1, role_id=1,
        scope_type='all', permissions=ALL_PERMISSIONS),))
    yield center, db, roles, plan, actor
    center.close()
    db.close()
    ce.dispose()
    te.dispose()


def product(code='P1'):
    return ProductCreate(code=code, name=code, base_price=Decimal('10'), skus=[
        ProductSkuCreate(sku_code=f'{code}-SKU', name='SKU', price=Decimal('10')),
    ])


def test_invited_and_active_members_share_quota(data):
    center, db, roles, _, actor = data
    quotas.ensure_member_capacity(center, 1, 10)  # 已占用的成员不重复计数
    with pytest.raises(AppError) as rejected:
        identity.create_member_invitation(center, db, tenant_id=1, email='new@example.test',
            actor_id=10, actor_effective=actor, bindings=[identity.RoleBindingChoice(
                role_id=int(roles['readonly'].id), scopes=(identity.ScopeChoice('all'),))])
    assert rejected.value.code is ErrorCode.QUOTA_EXCEEDED
    assert repository.get_user_by_email(center, 'new@example.test') is None


def test_catalog_creation_and_new_sku_update_enforce_limit(data):
    _, db, _, _, actor = data
    audit = catalog.AuditContext(actor_id=10)
    created = catalog.create_product(db, tenant_id=1, effective=actor, payload=product(), audit=audit)
    with pytest.raises(AppError) as rejected:
        catalog.create_product(db, tenant_id=1, effective=actor, payload=product('P2'), audit=audit)
    assert rejected.value.code is ErrorCode.QUOTA_EXCEEDED
    db.rollback()
    with pytest.raises(AppError) as sku_rejected:
        catalog.update_product(db, tenant_id=1, product_id=int(created.id), effective=actor,
            payload=ProductUpdate(skus=[ProductSkuUpdate(sku_code='EXTRA', name='Extra', price=10)]), audit=audit)
    assert sku_rejected.value.code is ErrorCode.QUOTA_EXCEEDED
    db.rollback()
    assert len(db.execute(select(Product)).scalars().all()) == 1
    assert len(db.execute(select(ProductSku)).scalars().all()) == 1
    # 降级或达到上限不妨碍修改既有数据。
    updated = catalog.update_product(db, tenant_id=1, product_id=int(created.id), effective=actor,
        payload=ProductUpdate(name='Renamed'), audit=audit)
    assert updated.name == 'Renamed'


def test_pending_store_occupies_slot_and_plan_change_applies_immediately(data):
    center, db, _, plan, _ = data
    channels.create_store(db, tenant_id=1, channel='tiktok', name='Store 1')
    with pytest.raises(AppError) as rejected:
        channels.create_store(db, tenant_id=1, channel='tiktok', name='Store 2')
    assert rejected.value.code is ErrorCode.QUOTA_EXCEEDED
    db.rollback()
    feature = center.execute(select(PlanFeature).where(
        PlanFeature.plan_id == plan.id, PlanFeature.feature_code == 'max_stores',
    )).scalar_one()
    feature.limit_value = 2
    center.commit()
    channels.create_store(db, tenant_id=1, channel='tiktok', name='Store 2')
    assert len(db.execute(select(Store)).scalars().all()) == 2


def test_valid_subscription_overrides_tenant_default_plan(data):
    center, _, _, _, _ = data
    larger = Plan(code='larger', name='Larger', status='active', is_default=False)
    center.add(larger)
    center.flush()
    center.add(PlanFeature(plan_id=larger.id, feature_code='max_stores', limit_value=5))
    subscription = Subscription(tenant_id=1, plan_id=larger.id, status='active',
        starts_at=utc_now() - timedelta(days=1), ends_at=utc_now() + timedelta(days=1))
    center.add(subscription)
    center.commit()
    assert quotas.current_limits(center, 1)['max_stores'] == 5
    subscription.ends_at = utc_now() - timedelta(seconds=1)
    center.commit()
    assert quotas.current_limits(center, 1)['max_stores'] == 1


def test_missing_tenant_is_not_treated_as_unlimited(data):
    center, _, _, _, _ = data
    with pytest.raises(AppError) as rejected:
        quotas.current_limits(center, 999)
    assert rejected.value.code is ErrorCode.TENANT_NOT_FOUND
