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


def test_column_collation_does_not_truncate_table_or_hide_missing_comments():
    sql = """CREATE TABLE sku_identifiers (
    normalized_identifier VARCHAR(384) COLLATE utf8mb4_bin NOT NULL COMMENT '标准化编号（唯一）',
    kind VARCHAR(16) NOT NULL COMMENT '编号类型',
    incomplete VARCHAR(16),
    CONSTRAINT uq_identifiers UNIQUE (normalized_identifier)
    )ENGINE=InnoDB COMMENT='SKU别名表' COLLATE utf8mb4_0900_ai_ci;
    """
    parsed = parse_sql_tables(sql)["sku_identifiers"]
    assert parsed["columns"]["normalized_identifier"] == "标准化编号（唯一）"
    assert parsed["columns"]["kind"] == "编号类型"
    assert parsed["missing_columns"] == ["incomplete"]
