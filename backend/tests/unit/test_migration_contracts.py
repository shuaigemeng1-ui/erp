"""迁移契约回归：实际版本发现与 MySQL MODIFY 的字段注释丢失。"""
from scripts.verify_contracts import Checker, check_migration_files, parse_sql_tables


BASE = """CREATE TABLE stores (
store_code VARCHAR(32) NOT NULL COMMENT '店铺编码'
)ENGINE=InnoDB COMMENT='店铺表';
"""


def test_final_sql_comment_check_catches_modify_and_accepts_repair():
    dropped=BASE+"ALTER TABLE stores MODIFY store_code VARCHAR(32) NOT NULL DEFAULT '';"
    assert parse_sql_tables(dropped)['stores']['missing_columns'] == ['store_code']
    repaired=dropped+"ALTER TABLE stores MODIFY store_code VARCHAR(32) NOT NULL COMMENT '店铺编码';"
    assert parse_sql_tables(repaired)['stores']['missing_columns'] == []


def test_migration_discovery_does_not_trust_empty_documented_baseline():
    checker=Checker()
    check_migration_files(checker, [])
    assert any('迁移文档覆盖实际版本图' in error for error in checker.failures)
    assert checker.passed >= 2
