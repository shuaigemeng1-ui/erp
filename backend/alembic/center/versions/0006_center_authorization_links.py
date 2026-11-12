"""中心库：可撤销授权邀请和跨库写入恢复记录，禁止外键。"""
from alembic import op
import sqlalchemy as sa

revision = "center_0006"
down_revision = "center_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "channel_authorization_links",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, comment="主键ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="更新时间（UTC）"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="授权租户ID"),
        sa.Column("member_id", sa.BigInteger(), nullable=False, comment="发起成员ID"),
        sa.Column("store_id", sa.BigInteger(), nullable=True, comment="目标店铺ID"),
        sa.Column("channel", sa.String(32), nullable=False, comment="授权渠道编码"),
        sa.Column("token_hash", sa.String(64), nullable=False, comment="链接随机凭据摘要"),
        sa.Column("status", sa.String(32), nullable=False, comment="状态：active/revoked/pending_save"),
        sa.Column("context", sa.JSON(), nullable=False, comment="授权上下文，不含平台密钥"),
        sa.Column("result_cipher", sa.Text(), nullable=True, comment="待恢复的授权结果密文"),
        sa.UniqueConstraint("token_hash", name="uq_authorization_link_token"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="渠道长期授权邀请表",
    )
    op.create_index("ix_authorization_link_target", "channel_authorization_links", ["tenant_id", "channel", "store_id"])


def downgrade():
    op.drop_table("channel_authorization_links")
