"""迁移直接SQL必须满足旧审计表约束，而不依赖 ORM 默认值。"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from sqlalchemy import create_engine, text


def test_catalog_migration_audit_supplies_legacy_required_fields(monkeypatch):
    path=Path(__file__).resolve().parents[2]/"alembic/tenant/versions/0011_tenant_spu_sku_catalog.py"
    spec=importlib.util.spec_from_file_location("spu_migration",path)
    migration=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine=create_engine("sqlite://")
    with engine.begin() as db:
        db.exec_driver_sql("CREATE TABLE products (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL)")
        db.exec_driver_sql("INSERT INTO products VALUES (7,1)")
        db.exec_driver_sql("CREATE TABLE audit_logs (tenant_id INTEGER NOT NULL,product_id INTEGER,action TEXT NOT NULL,resource_type TEXT NOT NULL,resource_id INTEGER,summary TEXT NOT NULL,payload JSON,ip TEXT NOT NULL,request_id TEXT NOT NULL)")
        monkeypatch.setattr(migration,"op",SimpleNamespace(execute=db.exec_driver_sql))
        migration._record_migration_audit()
        row=db.execute(text("SELECT product_id,ip,request_id FROM audit_logs")).one()
        assert tuple(row)==(7,"","")
    engine.dispose()
