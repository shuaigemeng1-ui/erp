"""SPU资料、SKU成本重量及唯一别名、商品操作日志；禁止外键。"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "tenant_0011"
down_revision = "tenant_0010"
branch_labels = None
depends_on = None

_TABLE_KW = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
_PRODUCT_NAMES = ["name_en", "preferred_supplier_id", "purchaser_id", "developer_id", "inquiry_buyer_id", "hs_code", "material_zh", "material_en", "usage", "feature_tags", "packaging_name", "length_cm", "width_cm", "height_cm", "package_length_cm", "package_width_cm", "package_height_cm", "sales_type", "remark", "features", "version"]
_SKU_NAMES = ["image_url", "guidance_cost", "average_cost", "weight_g", "sales_status", "version"]


def _base_columns():
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="更新时间（UTC）"),
    ]


def _legacy_identifiers():
    """在 DDL 前按运行时同样的 Python casefold 检查旧编号冲突。"""
    if context.is_offline_mode():
        return []
    rows = op.get_bind().execute(sa.text("SELECT id, tenant_id, sku_code, status FROM product_skus ORDER BY id")).mappings().all()
    seen = set()
    result = []
    for row in rows:
        normalized = row["sku_code"].strip().casefold()
        key = (int(row["tenant_id"]), normalized)
        if not normalized or key in seen:
            raise RuntimeError("历史SKU编号标准化后存在空值或冲突，请先核查后再执行迁移")
        seen.add(key)
        result.append({
            "tenant_id": row["tenant_id"], "sku_id": row["id"], "identifier": row["sku_code"],
            "normalized_identifier": normalized, "kind": "primary", "is_active": True,
        })
    return result


def upgrade():
    identifiers = _legacy_identifiers()
    op.add_column("order_items", sa.Column("platform_sku_id", sa.String(64), nullable=False,
        server_default="", comment="平台原生SKU ID（用于订单明细稳定识别）"))
    op.create_index("ix_order_items_tenant_order_platform_sku", "order_items",
        ["tenant_id", "order_id", "external_item_id", "platform_sku_id"])
    op.alter_column("channel_listings", "external_sku_id", existing_type=sa.String(64),
        type_=sa.String(128), existing_nullable=False, existing_server_default="",
        comment="平台规格ID或平台 SKU 编码；无规格商品为空串")
    for column in [
        sa.Column("name_en", sa.String(200), nullable=True, comment="商品英文名称"),
        sa.Column("preferred_supplier_id", sa.BigInteger(), nullable=True, comment="首选供应商ID"),
        sa.Column("purchaser_id", sa.BigInteger(), nullable=True, comment="采购员租户成员ID"),
        sa.Column("developer_id", sa.BigInteger(), nullable=True, comment="业务开发员租户成员ID"),
        sa.Column("inquiry_buyer_id", sa.BigInteger(), nullable=True, comment="采购询价员租户成员ID"),
        sa.Column("hs_code", sa.String(32), nullable=True, comment="海关编码"),
        sa.Column("material_zh", sa.String(500), nullable=True, comment="中文材质"),
        sa.Column("material_en", sa.String(500), nullable=True, comment="英文材质"),
        sa.Column("usage", sa.String(1000), nullable=True, comment="商品用途"),
        sa.Column("feature_tags", sa.JSON(), nullable=True, comment="商品特性标签列表"),
        sa.Column("packaging_name", sa.String(100), nullable=True, comment="包材名称"),
        sa.Column("length_cm", sa.Numeric(18, 4), nullable=True, comment="商品长度（厘米）"),
        sa.Column("width_cm", sa.Numeric(18, 4), nullable=True, comment="商品宽度（厘米）"),
        sa.Column("height_cm", sa.Numeric(18, 4), nullable=True, comment="商品高度（厘米）"),
        sa.Column("package_length_cm", sa.Numeric(18, 4), nullable=True, comment="包装后长度（厘米）"),
        sa.Column("package_width_cm", sa.Numeric(18, 4), nullable=True, comment="包装后宽度（厘米）"),
        sa.Column("package_height_cm", sa.Numeric(18, 4), nullable=True, comment="包装后高度（厘米）"),
        sa.Column("sales_type", sa.String(16), nullable=False, server_default="normal", comment="销售类型：普通、变参、捆绑、组装"),
        sa.Column("remark", sa.Text(), nullable=True, comment="商品备注"),
        sa.Column("features", sa.Text(), nullable=True, comment="产品特点"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="商品编辑版本号"),
    ]:
        op.add_column("products", column)
    for column in [
        sa.Column("image_url", sa.String(500), nullable=True, comment="SKU 独立图片地址"),
        sa.Column("guidance_cost", sa.Numeric(18, 4), nullable=True, comment="SKU 指导成本"),
        sa.Column("average_cost", sa.Numeric(18, 4), nullable=True, comment="SKU 平均成本（本轮人工维护）"),
        sa.Column("weight_g", sa.Numeric(18, 4), nullable=True, comment="SKU 重量（克）"),
        sa.Column("sales_status", sa.String(16), nullable=False, server_default="on_sale", comment="SKU 销售状态：在售、停售、试卖、清仓"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="SKU 编辑版本号"),
    ]:
        op.add_column("product_skus", column)
    op.add_column("audit_logs", sa.Column("product_id", sa.BigInteger(), nullable=True, comment="关联SPU商品ID"))
    op.add_column("audit_logs", sa.Column("sku_id", sa.BigInteger(), nullable=True, comment="关联SKU ID"))
    op.add_column("audit_logs", sa.Column("actor_name_snapshot", sa.String(200), nullable=True, comment="操作人姓名历史快照"))
    op.create_index("ix_audit_logs_tenant_product_created", "audit_logs", ["tenant_id", "product_id", "created_at", "id"])

    op.create_table(
        "sku_identifiers", *_base_columns(),
        sa.Column("sku_id", sa.BigInteger(), nullable=False, comment="所属SKU ID"),
        sa.Column("identifier", sa.String(128), nullable=False, comment="SKU编号或别名原文"),
        sa.Column("normalized_identifier", sa.String(384).with_variant(mysql.VARCHAR(384, collation="utf8mb4_bin"), "mysql"), nullable=False, comment="去首尾空白并统一大小写后的标识"),
        sa.Column("kind", sa.String(16), nullable=False, comment="标识类型：主编号或别名"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="是否启用该标识"),
        sa.UniqueConstraint("tenant_id", "normalized_identifier", name="uq_sku_identifiers_tenant_normalized"),
        comment="SKU编号与别名表", **_TABLE_KW,
    )
    op.create_index("ix_sku_identifiers_tenant_id", "sku_identifiers", ["tenant_id"])
    op.create_index("ix_sku_identifiers_tenant_sku", "sku_identifiers", ["tenant_id", "sku_id"])
    op.create_table(
        "suppliers", *_base_columns(),
        sa.Column("code", sa.String(64), nullable=False, comment="供应商编码（租户内唯一）"),
        sa.Column("name", sa.String(200), nullable=False, comment="供应商名称"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active", comment="供应商状态：启用或停用"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_suppliers_tenant_code"),
        comment="供应商简档表", **_TABLE_KW,
    )
    op.create_index("ix_suppliers_tenant_id", "suppliers", ["tenant_id"])
    op.create_table(
        "catalog_options", *_base_columns(),
        sa.Column("kind", sa.String(16), nullable=False, comment="字典种类：品牌、分类、标签、包材"),
        sa.Column("name", sa.String(100), nullable=False, comment="字典选项名称"),
        sa.Column("parent_id", sa.BigInteger(), nullable=True, comment="父级分类选项ID"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active", comment="选项状态：启用或停用"),
        sa.UniqueConstraint("tenant_id", "kind", "name", name="uq_catalog_options_tenant_kind_name"),
        comment="商品基础字典表", **_TABLE_KW,
    )
    op.create_index("ix_catalog_options_tenant_id", "catalog_options", ["tenant_id"])

    op.execute("UPDATE product_skus SET sales_status = 'discontinued' WHERE status = 'archived'")
    op.execute("UPDATE product_skus s JOIN products p ON p.tenant_id = s.tenant_id AND p.id = s.product_id SET s.cost_amount = p.cost_amount WHERE s.cost_amount IS NULL AND p.cost_amount IS NOT NULL")
    op.execute("UPDATE audit_logs SET product_id = resource_id WHERE resource_type = 'product'")
    op.execute("UPDATE product_skus s SET s.image_url = (SELECT i.url FROM product_images i WHERE i.tenant_id = s.tenant_id AND i.product_id = s.product_id AND i.sku_id = s.id ORDER BY i.sort_order, i.id LIMIT 1) WHERE s.image_url IS NULL")
    for kind in ("brand", "category"):
        op.execute(f"INSERT INTO catalog_options (tenant_id,kind,name,status) SELECT tenant_id,'{kind}',TRIM({kind}),'active' FROM products WHERE {kind} IS NOT NULL AND LENGTH(TRIM({kind})) > 0 GROUP BY tenant_id,TRIM({kind})")
    if context.is_offline_mode():
        # 离线 SQL 提供结构审阅；非 ASCII 编号的规范化须使用在线迁移的 casefold 路径。
        op.execute("INSERT INTO sku_identifiers (tenant_id,sku_id,identifier,normalized_identifier,kind,is_active) SELECT tenant_id,id,sku_code,LOWER(TRIM(sku_code)),'primary',1 FROM product_skus")
    elif identifiers:
        table = sa.table("sku_identifiers", sa.column("tenant_id"), sa.column("sku_id"), sa.column("identifier"), sa.column("normalized_identifier"), sa.column("kind"), sa.column("is_active"))
        op.get_bind().execute(table.insert(), identifiers)
    _record_migration_audit()


def _record_migration_audit():
    # 历史表的 ip/request_id 只有 ORM 默认值，直接执行 SQL 必须显式填写。
    op.execute("""INSERT INTO audit_logs (tenant_id,product_id,action,resource_type,resource_id,summary,payload,ip,request_id)
        SELECT p.tenant_id,p.id,'catalog.migration','product',p.id,'迁移SPU资料与SKU标识；旧主商品成本仅补齐空SKU成本',NULL,'','' FROM products p""")


def downgrade():
    if not context.is_offline_mode():
        too_long = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM channel_listings WHERE CHAR_LENGTH(external_sku_id) > 64")).scalar_one()
        if too_long:
            raise RuntimeError("已有超过64字符的平台SKU编码，不能安全缩短字段")
    op.alter_column("channel_listings", "external_sku_id", existing_type=sa.String(128),
        type_=sa.String(64), existing_nullable=False, existing_server_default="",
        comment="平台规格ID或平台 SKU 编码；无规格商品为空串")
    op.drop_index("ix_order_items_tenant_order_platform_sku", table_name="order_items")
    op.drop_column("order_items", "platform_sku_id")
    op.drop_table("catalog_options")
    op.drop_table("suppliers")
    op.drop_table("sku_identifiers")
    op.drop_index("ix_audit_logs_tenant_product_created", table_name="audit_logs")
    for name in ("actor_name_snapshot", "sku_id", "product_id"):
        op.drop_column("audit_logs", name)
    for name in reversed(_SKU_NAMES):
        op.drop_column("product_skus", name)
    for name in reversed(_PRODUCT_NAMES):
        op.drop_column("products", name)
