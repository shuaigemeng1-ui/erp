"""中心库：备份记录扩展（批次3「备份加密异地与恢复演练」）。

* ``backup_records`` 新增触发来源、本地生成/校验、异地上传、恢复验证等状态列，
  让「本地生成 → 完整性校验 → 加密上传私有存储 → 隔离恢复验证」四个阶段可追踪；
* 备份文件迁出 Git 工作目录（``/var/lib/ventasync-backups``），由部署脚本负责
  目录 700 / 文件 600，本迁移只记录元数据，不保存任何密钥或备份内容；
* 全部新列可空或带默认值，旧记录读取兼容；无外键、字段中文注释。

Revision ID: center_0018
Revises: center_0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0018"
down_revision = "center_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backup_records",
        sa.Column(
            "trigger",
            sa.String(16),
            nullable=False,
            server_default="scheduled",
            comment="触发来源：manual=手工，scheduled=每日定时，pre_upgrade=升级前额外备份",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "local_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="本地生成状态：pending=待生成，generated=已生成，verified=已校验，failed=失败",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column("verified_at", sa.DateTime(), nullable=True, comment="本地完整性校验通过时间（UTC）"),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "offsite_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="异地上传状态：pending=待上传，uploaded=已上传，failed=失败，skipped=未配置异地",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "offsite_key",
            sa.String(500),
            nullable=False,
            server_default="",
            comment="异地私有存储对象键",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "offsite_bucket",
            sa.String(255),
            nullable=False,
            server_default="",
            comment="异地私有存储桶（不复用商品图片公开目录）",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "encryption",
            sa.String(32),
            nullable=False,
            server_default="",
            comment="异地包加密算法（如 aes-256-cbc/pbkdf2）",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "local_path",
            sa.String(500),
            nullable=False,
            server_default="",
            comment="本地备份目录（Git 工作目录之外）",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "checksum_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
            comment="校验状态：pending=待校验，verified=校验通过，failed=校验失败",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "checksum",
            sa.String(128),
            nullable=False,
            server_default="",
            comment="加密包/归档校验值（sha256:<hex>）",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "restore_status",
            sa.String(16),
            nullable=False,
            server_default="not_tested",
            comment="恢复验证状态：not_tested=未演练，verified=隔离恢复验证通过，failed=验证失败",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "restore_verified_at", sa.DateTime(), nullable=True, comment="隔离环境恢复验证通过时间（UTC）"
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "restore_notes",
            sa.String(500),
            nullable=False,
            server_default="",
            comment="恢复演练结论/证据摘要",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "failure_reason",
            sa.String(500),
            nullable=False,
            server_default="",
            comment="失败原因（本地/校验/异地/恢复任一阶段）",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "stages",
            sa.JSON(),
            nullable=True,
            comment="阶段状态流水（生成/校验/上传/恢复，JSON 数组）",
        ),
    )
    op.add_column(
        "backup_records",
        sa.Column(
            "manifest_json",
            sa.JSON(),
            nullable=True,
            comment="备份清单（数据库/配置/本地文件/校验结果 JSON）",
        ),
    )
    op.create_index(
        "ix_backup_records_trigger_status",
        "backup_records",
        ["trigger", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_backup_records_trigger_status", table_name="backup_records")
    for column in (
        "stages",
        "failure_reason",
        "manifest_json",
        "restore_notes",
        "restore_verified_at",
        "restore_status",
        "encryption",
        "offsite_bucket",
        "offsite_key",
        "offsite_status",
        "checksum",
        "checksum_status",
        "local_path",
        "verified_at",
        "local_status",
        "trigger",
    ):
        op.drop_column("backup_records", column)
