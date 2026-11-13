"""中心库：发布版本清单与升级校验字段、Agent 执行批次/租约字段。

批次1「发布版本清单与升级状态可信」：

* ``release_versions``：完整 Git commit、构建配置摘要、最低 Agent 协议版本、
  center/tenant 数据库目标版本、回退兼容声明、构建状态；
* ``tenant_instances``：目标版本、实际版本、Agent/执行器版本、数据库版本、升级状态；
* ``server_nodes``：Agent 协议版本与能力、执行器版本、数据库版本、任务心跳时间；
* ``operation_jobs``：执行批次 attempt_id、租约令牌、任务心跳、事件序号水位、
  Agent 版本/协议版本、实际版本、数据库版本、升级状态与升级校验结果。

全部新列可空或带默认值，旧记录读取兼容；无外键，中文注释。

Revision ID: center_0016
Revises: center_0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0016"
down_revision = "center_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ 发布版本
    op.add_column(
        "release_versions",
        sa.Column(
            "commit_sha",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="完整Git提交号（40或64位十六进制，旧记录为空）",
        ),
    )
    op.add_column(
        "release_versions",
        sa.Column(
            "build_config_summary",
            sa.String(500),
            nullable=False,
            server_default="",
            comment="构建配置摘要（构建参数/依赖锁摘要）",
        ),
    )
    op.add_column(
        "release_versions",
        sa.Column(
            "min_agent_protocol",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="最低Agent协议版本（整数）",
        ),
    )
    op.add_column(
        "release_versions",
        sa.Column(
            "center_db_version",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="运行模式center的数据库目标版本（Alembic revision）",
        ),
    )
    op.add_column(
        "release_versions",
        sa.Column(
            "tenant_db_version",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="运行模式tenant的数据库目标版本（Alembic revision）",
        ),
    )
    op.add_column(
        "release_versions",
        sa.Column(
            "rollback_compatible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
            comment="是否允许旧应用兼容新库（回退兼容声明）",
        ),
    )
    op.add_column(
        "release_versions",
        sa.Column(
            "build_status",
            sa.String(16),
            nullable=False,
            server_default="built",
            comment="构建状态：built/verified/failed",
        ),
    )

    # ------------------------------------------------------------------ 租户实例
    op.add_column(
        "tenant_instances",
        sa.Column(
            "actual_version",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="实际运行版本（Agent上报）",
        ),
    )
    op.add_column(
        "tenant_instances",
        sa.Column(
            "target_version",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="升级目标版本（空表示无进行中的目标）",
        ),
    )
    op.add_column(
        "tenant_instances",
        sa.Column("agent_version", sa.String(32), nullable=False, server_default="", comment="Agent版本"),
    )
    op.add_column(
        "tenant_instances",
        sa.Column(
            "executor_version",
            sa.String(32),
            nullable=False,
            server_default="",
            comment="运维执行器版本",
        ),
    )
    op.add_column(
        "tenant_instances",
        sa.Column(
            "db_version",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="当前数据库版本（Alembic revision）",
        ),
    )
    op.add_column(
        "tenant_instances",
        sa.Column(
            "upgrade_status",
            sa.String(24),
            nullable=False,
            server_default="unknown",
            comment="升级状态：pending/building/maintenance/migrating/verifying/succeeded/failed/unknown",
        ),
    )

    # ------------------------------------------------------------------ 服务器节点
    op.add_column(
        "server_nodes",
        sa.Column(
            "executor_version",
            sa.String(32),
            nullable=False,
            server_default="",
            comment="运维执行器版本",
        ),
    )
    op.add_column(
        "server_nodes",
        sa.Column(
            "protocol_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Agent协议版本（整数，用于与发布版本最低协议比对）",
        ),
    )
    op.add_column(
        "server_nodes",
        sa.Column("capabilities_json", sa.JSON(), nullable=True, comment="Agent能力列表（心跳上报）"),
    )
    op.add_column(
        "server_nodes",
        sa.Column(
            "db_version",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="节点上报的数据库版本",
        ),
    )
    op.add_column(
        "server_nodes",
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True, comment="最后任务执行心跳UTC"),
    )

    # ------------------------------------------------------------------ 运维任务
    op.add_column(
        "operation_jobs",
        sa.Column(
            "actual_version",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="执行后实际版本",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "agent_version",
            sa.String(32),
            nullable=False,
            server_default="",
            comment="执行该任务的Agent版本",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "agent_protocol_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="执行该任务的Agent协议版本（整数）",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "db_version",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="执行后实际数据库版本",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "upgrade_status",
            sa.String(24),
            nullable=False,
            server_default="",
            comment="升级状态：pending/building/maintenance/migrating/verifying/succeeded/failed/unknown",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "attempt_id",
            sa.String(36),
            nullable=False,
            server_default="",
            comment="当前执行批次ID（每次领取重新生成）",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "lease_token",
            sa.String(64),
            nullable=False,
            server_default="",
            comment="执行租约令牌（事件需回传，防旧租约写入）",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True, comment="Agent执行心跳UTC"),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "event_seq",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="已接收事件序号水位（去重与断线重放）",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "checks_json",
            sa.JSON(),
            nullable=True,
            comment="升级校验结果（数据库版本/服务就绪/业务只读）",
        ),
    )
    op.add_column(
        "operation_jobs",
        sa.Column(
            "checks_status",
            sa.String(16),
            nullable=False,
            server_default="",
            comment="校验结论：passed/failed/partial/missing/空",
        ),
    )
    op.create_index("ix_operation_jobs_attempt_id", "operation_jobs", ["attempt_id"], unique=False)

    # ------------------------------------------------------------------ 补齐历史字段中文注释
    op.alter_column(
        "alert_events",
        "severity",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="warning",
        comment="告警级别：warning=警告，critical=严重",
    )
    op.alter_column(
        "alert_events",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="open",
        comment="告警状态：open=待处理，acknowledged=已确认，resolved=已恢复",
    )
    op.alter_column(
        "release_versions",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="active",
        comment="发布状态：draft=草稿，active=可用，retired=已退役",
    )
    op.alter_column(
        "operation_jobs",
        "job_type",
        existing_type=sa.String(32),
        existing_nullable=False,
        comment="任务类型：deploy=部署，upgrade=升级，restart=重启，backup=备份，logs=日志，config=配置",
    )
    op.alter_column(
        "operation_jobs",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="pending",
        comment="任务状态：pending=待领取，leased=已领取，running=执行中，succeeded=成功，failed=失败，unknown=结果待核对",
    )
    op.alter_column(
        "operation_job_steps",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="pending",
        comment="步骤状态：pending=待执行，running=执行中，succeeded=成功，failed=失败，skipped=跳过",
    )
    op.alter_column(
        "backup_records",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="running",
        comment="备份状态：running=执行中，succeeded=成功，failed=失败",
    )


def downgrade() -> None:
    # 恢复历史字段英文注释
    op.alter_column(
        "backup_records",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="running",
        comment="running/succeeded/failed",
    )
    op.alter_column(
        "operation_job_steps",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="pending",
        comment="pending/running/succeeded/failed/skipped",
    )
    op.alter_column(
        "operation_jobs",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="pending",
        comment="pending/leased/running/succeeded/failed/unknown",
    )
    op.alter_column(
        "operation_jobs",
        "job_type",
        existing_type=sa.String(32),
        existing_nullable=False,
        comment="deploy/upgrade/restart/backup/logs/config",
    )
    op.alter_column(
        "release_versions",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="active",
        comment="draft/active/retired",
    )
    op.alter_column(
        "alert_events",
        "status",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="open",
        comment="open/acknowledged/resolved",
    )
    op.alter_column(
        "alert_events",
        "severity",
        existing_type=sa.String(16),
        existing_nullable=False,
        server_default="warning",
        comment="warning/critical",
    )

    op.drop_index("ix_operation_jobs_attempt_id", table_name="operation_jobs")
    for column in (
        "checks_status",
        "checks_json",
        "event_seq",
        "last_heartbeat_at",
        "lease_token",
        "attempt_id",
        "upgrade_status",
        "db_version",
        "agent_protocol_version",
        "agent_version",
        "actual_version",
    ):
        op.drop_column("operation_jobs", column)

    for column in ("last_heartbeat_at", "db_version", "capabilities_json", "protocol_version", "executor_version"):
        op.drop_column("server_nodes", column)

    for column in (
        "upgrade_status",
        "db_version",
        "executor_version",
        "agent_version",
        "target_version",
        "actual_version",
    ):
        op.drop_column("tenant_instances", column)

    for column in (
        "build_status",
        "rollback_compatible",
        "tenant_db_version",
        "center_db_version",
        "min_agent_protocol",
        "build_config_summary",
        "commit_sha",
    ):
        op.drop_column("release_versions", column)
