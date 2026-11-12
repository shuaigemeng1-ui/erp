"""记录云文件厂商和长期URL，保留历史本地文件；禁止外键。"""
from alembic import op, context
import sqlalchemy as sa

revision="tenant_0012"
down_revision="tenant_0011"
branch_labels=None
depends_on=None


def upgrade():
    op.add_column("files",sa.Column("storage_profile_id",sa.BigInteger(),nullable=True,comment="上传时使用的平台存储配置ID（无外键）"))
    op.add_column("files",sa.Column("storage_provider",sa.String(32),nullable=False,server_default="local",comment="文件存储厂商（历史文件为本地）"))
    op.add_column("files",sa.Column("url",sa.String(2048),nullable=True,comment="上传成功后保存的长期访问URL"))
    op.alter_column("product_images","url",existing_type=sa.String(500),type_=sa.String(2048),existing_nullable=False,comment="图片地址")
    op.alter_column("product_skus","image_url",existing_type=sa.String(500),type_=sa.String(2048),existing_nullable=True,comment="SKU 独立图片地址")


def downgrade():
    if not context.is_offline_mode():
        for table,column in (("product_images","url"),("product_skus","image_url")):
            if op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE CHAR_LENGTH({column}) > 500")).scalar_one():
                raise RuntimeError("已有较长云图片URL，不能安全缩短字段")
        if op.get_bind().execute(sa.text("SELECT COUNT(*) FROM files WHERE storage_provider <> 'local'")).scalar_one():
            raise RuntimeError("已有云文件记录，不能删除其访问地址信息")
    op.alter_column("product_skus","image_url",existing_type=sa.String(2048),type_=sa.String(500),existing_nullable=True,comment="SKU 独立图片地址")
    op.alter_column("product_images","url",existing_type=sa.String(2048),type_=sa.String(500),existing_nullable=False,comment="图片地址")
    for column in ("url","storage_provider","storage_profile_id"):
        op.drop_column("files",column)
