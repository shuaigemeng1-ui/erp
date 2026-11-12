"""中心库：发布版本、运维任务、任务步骤与备份记录。

Revision ID: center_0011
Revises: center_0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0011"
down_revision = "center_0010"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="创建时间UTC"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="更新时间UTC"),
    ]


def upgrade() -> None:
    op.create_table(
        "release_versions",
        *_base(),
        sa.Column("version", sa.String(64), nullable=False, comment="版本号"),
        sa.Column("image_tag", sa.String(255), nullable=False, server_default="", comment="镜像标签"),
        sa.Column("image_digest", sa.String(255), nullable=False, server_default="", comment="镜像摘要"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active", comment="draft/active/retired"),
        sa.Column("notes", sa.String(500), nullable=False, server_default="", comment="发布说明"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人用户ID"),
        sa.UniqueConstraint("version", name="uq_release_versions_version"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="发布版本表",
    )

    op.create_table(
        "operation_jobs",
        *_base(),
        sa.Column("instance_id", sa.BigInteger(), nullable=False, comment="实例ID"),
        sa.Column("node_id", sa.BigInteger(), nullable=True, comment="节点ID"),
        sa.Column("job_type", sa.String(32), nullable=False, comment="任务类型"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", comment="任务状态"),
        sa.Column("idempotency_key", sa.String(128), nullable=False, comment="幂等键"),
        sa.Column("target_version", sa.String(64), nullable=False, server_default="", comment="目标版本"),
        sa.Column("payload_json", sa.JSON(), nullable=True, comment="任务参数"),
        sa.Column("result_json", sa.JSON(), nullable=True, comment="执行结果"),
        sa.Column("error", sa.String(500), nullable=False, server_default="", comment="失败原因"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0", comment="已领取次数"),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True, comment="执行租约到期UTC"),
        sa.Column("requested_by", sa.BigInteger(), nullable=True, comment="发起人用户ID"),
        sa.Column("started_at", sa.DateTime(), nullable=True, comment="开始执行UTC"),
        sa.Column("finished_at", sa.DateTime(), nullable=True, comment="完成时间UTC"),
        sa.UniqueConstraint("idempotency_key", name="uq_operation_jobs_idempotency_key"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="运维任务表",
    )
    op.create_index("ix_operation_jobs_instance_status", "operation_jobs", ["instance_id", "status"])
    op.create_index("ix_operation_jobs_node_status", "operation_jobs", ["node_id", "status"])

    op.create_table(
        "operation_job_steps",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("job_id", sa.BigInteger(), nullable=False, comment="任务ID"),
        sa.Column("step_no", sa.Integer(), nullable=False, comment="步骤序号"),
        sa.Column("name", sa.String(64), nullable=False, comment="步骤名称"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", comment="步骤状态"),
        sa.Column("message", sa.String(1000), nullable=False, server_default="", comment="脱敏消息"),
        sa.Column("started_at", sa.DateTime(), nullable=True, comment="开始UTC"),
        sa.Column("finished_at", sa.DateTime(), nullable=True, comment="完成UTC"),
        sa.UniqueConstraint("job_id", "step_no", name="uq_operation_job_steps_job_step"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="运维任务步骤表",
    )
    op.create_index("ix_operation_job_steps_job", "operation_job_steps", ["job_id"])

    op.create_table(
        "backup_records",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("instance_id", sa.BigInteger(), nullable=False, comment="实例ID"),
        sa.Column("node_id", sa.BigInteger(), nullable=True, comment="节点ID"),
        sa.Column("job_id", sa.BigInteger(), nullable=True, comment="运维任务ID"),
        sa.Column("status", sa.String(16), nullable=False, server_default="running", comment="备份状态"),
        sa.Column("storage_key", sa.String(500), nullable=False, server_default="", comment="备份存储键"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0", comment="备份大小字节"),
        sa.Column("started_at", sa.DateTime(), nullable=False, comment="开始UTC"),
        sa.Column("finished_at", sa.DateTime(), nullable=True, comment="完成UTC"),
        sa.Column("retention_until", sa.DateTime(), nullable=True, comment="保留到期UTC"),
        sa.Column("error", sa.String(500), nullable=False, server_default="", comment="失败原因"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4", comment="备份记录表",
    )
    op.create_index("ix_backup_records_instance_started", "backup_records", ["instance_id", "started_at"])


def downgrade() -> None:
    op.drop_table("backup_records")
    op.drop_table("operation_job_steps")
    op.drop_table("operation_jobs")
    op.drop_table("release_versions")
