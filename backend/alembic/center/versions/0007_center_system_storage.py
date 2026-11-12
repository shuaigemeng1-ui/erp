"""平台系统配置与云对象存储账号；不使用外键。"""
from alembic import op
import sqlalchemy as sa

revision = "center_0007"
down_revision = "center_0006"
branch_labels = None
depends_on = None


def _base():
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="更新时间（UTC）"),
    ]


def upgrade():
    op.create_table(
        "storage_profiles", *_base(),
        sa.Column("name", sa.String(100), nullable=False, comment="存储账号名称"),
        sa.Column("provider", sa.String(32), nullable=False, comment="云厂商编码：OSS、COS、OBS、S3或兼容服务"),
        sa.Column("endpoint", sa.String(500), nullable=False, comment="对象存储服务HTTPS地址"),
        sa.Column("bucket", sa.String(255), nullable=False, comment="存储桶名称"),
        sa.Column("region", sa.String(100), nullable=False, comment="存储区域编码"),
        sa.Column("access_key_id_cipher", sa.Text(), nullable=False, comment="访问密钥ID密文"),
        sa.Column("secret_access_key_cipher", sa.Text(), nullable=False, comment="访问密钥Secret密文"),
        sa.Column("public_base_url", sa.String(1000), nullable=False, comment="文件长期访问域名或CDN基础地址"),
        sa.Column("key_prefix", sa.String(200), nullable=False, server_default="", comment="对象存储键前缀"),
        sa.Column("addressing_style", sa.String(16), nullable=False, server_default="auto", comment="存储桶寻址方式：自动、虚拟主机或路径"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1"), comment="是否启用此存储账号"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="存储配置编辑版本号"),
        sa.UniqueConstraint("name", name="uq_storage_profiles_name"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci", comment="平台对象存储账号配置表",
    )
    op.create_table(
        "platform_system_settings", *_base(),
        sa.Column("default_storage_profile_id", sa.BigInteger(), nullable=True, comment="当前默认对象存储账号ID（逻辑关联，无外键）"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="系统配置版本号"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci", comment="平台系统配置表（固定主键一）",
    )
    op.execute("INSERT INTO platform_system_settings (id,default_storage_profile_id,version) VALUES (1,NULL,1)")


def downgrade():
    op.drop_table("platform_system_settings")
    op.drop_table("storage_profiles")
