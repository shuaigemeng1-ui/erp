"""成员编号的租户隔离、高水位、幂等和历史回填，不启动服务。"""
from datetime import datetime
import importlib.util
from pathlib import Path
import re
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.base import CenterBase
from app.modules.identity import provisioning, repository
from app.modules.identity.models_center import Tenant, TenantMembership
from tests.unit.test_identity import _sqlite_metadata


def center_db():
    engine = create_engine("sqlite:///:memory:")
    _sqlite_metadata(CenterBase.metadata, ("tenants", "tenant_memberships")).create_all(engine)
    return engine, Session(engine, expire_on_commit=False)


def test_each_tenant_starts_at_one_and_existing_members_keep_their_number():
    engine, center = center_db()
    try:
        center.add_all([Tenant(id=1, code="one", name="一"), Tenant(id=2, code="two", name="二")])
        center.commit()
        first = provisioning._ensure_owner_membership(center, 1, 10)
        second_tenant = provisioning._ensure_owner_membership(center, 2, 10)
        following = provisioning._ensure_owner_membership(center, 1, 11)
        highest = provisioning._ensure_owner_membership(center, 1, 12)
        center.commit()
        assert (first.member_no, second_tenant.member_no, following.member_no, highest.member_no) == (1, 1, 2, 3)
        assert first.id != second_tenant.id
        original_id = first.id
        first.status = "disabled"
        center.commit()
        retried = provisioning._ensure_owner_membership(center, 1, 10)
        center.commit()
        assert (retried.id, retried.member_no) == (original_id, 1)
        assert center.get(Tenant, 1).next_member_no == 4
        # 即使未来增加硬删除能力，高水位也不重用已分配过的末尾编号。
        center.delete(highest)
        center.commit()
        replacement = provisioning._ensure_owner_membership(center, 1, 13)
        center.commit()
        assert replacement.member_no == 4
        assert repository.allocate_member_no(center, 1) == 5
        center.rollback()
        assert center.get(Tenant, 1).next_member_no == 5
    finally:
        center.close()
        engine.dispose()


def test_migration_ranks_first_creation_per_tenant_without_renumbering_internal_ids(monkeypatch):
    filename = Path(__file__).parents[2] / "alembic/center/versions/0009_center_tenant_member_numbers.py"
    spec = importlib.util.spec_from_file_location("member_number_migration", filename)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    statements = []
    monkeypatch.setattr(migration, "op", SimpleNamespace(
        add_column=lambda *a, **k: None, alter_column=lambda *a, **k: None,
        create_unique_constraint=lambda *a, **k: None,
        execute=lambda statement: statements.append(str(statement)),
    ))
    migration.upgrade()
    assert (migration.revision, migration.down_revision) == ("center_0009", "center_0008")
    ranked = re.search(r"SELECT id, ROW_NUMBER\(\).*?FROM tenant_memberships", statements[0], re.S).group()
    engine, center = center_db()
    try:
        center.add_all([
            TenantMembership(id=10, tenant_id=1, user_id=1, member_no=30, is_owner=False, created_at=datetime(2026, 1, 1)),
            TenantMembership(id=20, tenant_id=2, user_id=2, member_no=40, created_at=datetime(2026, 1, 3)),
            TenantMembership(id=30, tenant_id=1, user_id=3, member_no=50, is_owner=True, created_at=datetime(2026, 1, 2)),
        ])
        center.commit()
        result = dict(center.execute(text(ranked)).all())
        assert result == {10: 1, 30: 2, 20: 1}
        assert "SET target.member_no = numbered.assigned_member_no" in statements[0]
        assert "SET target.id" not in statements[0]
        assert "MAX(member_no) + 1" in statements[1]
    finally:
        center.close()
        engine.dispose()
