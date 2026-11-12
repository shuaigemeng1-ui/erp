"""中心库：统一运维管理（租户实例、服务器节点、指标、探测、告警）。

Revision ID: center_0010
Revises: center_0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "center_0010"
down_revision = "center_0009"
branch_labels = None
depends_on = None


def _base() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"), comment="更新时间（UTC）"),
    ]


def upgrade() -> None:
    op.create_table(
        "tenant_instances",
        *_base(),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("name", sa.String(100), nullable=False, comment="实例名称"),
        sa.Column("domain", sa.String(255), nullable=False, comment="业务域名（不含协议）"),
        sa.Column("environment", sa.String(16), nullable=False, server_default="production", comment="环境"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending", comment="状态"),
        sa.Column("app_version", sa.String(64), nullable=False, server_default="", comment="当前部署版本"),
        sa.Column("notes", sa.String(500), nullable=False, server_default="", comment="备注"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_instances_tenant_id"),
        sa.UniqueConstraint("domain", name="uq_tenant_instances_domain"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="租户独立实例表",
    )

    op.create_table(
        "server_nodes",
        *_base(),
        sa.Column("instance_id", sa.BigInteger(), nullable=False, comment="实例ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("node_uuid", sa.String(36), nullable=False, comment="节点固定UUID"),
        sa.Column("hostname", sa.String(255), nullable=False, server_default="", comment="主机名"),
        sa.Column("os_name", sa.String(100), nullable=False, server_default="", comment="操作系统"),
        sa.Column("arch", sa.String(32), nullable=False, server_default="", comment="CPU架构"),
        sa.Column("cpu_cores", sa.Integer(), nullable=False, server_default="0", comment="CPU核数"),
        sa.Column("memory_total_mb", sa.BigInteger(), nullable=False, server_default="0", comment="总内存MB"),
        sa.Column("disk_total_gb", sa.BigInteger(), nullable=False, server_default="0", comment="总磁盘GB"),
        sa.Column("internal_ips", sa.JSON(), nullable=True, comment="内网IP列表"),
        sa.Column("configured_public_ip", sa.String(64), nullable=False, server_default="", comment="配置公网IP"),
        sa.Column("observed_public_ip", sa.String(64), nullable=False, server_default="", comment="观测出口IP"),
        sa.Column("agent_version", sa.String(32), nullable=False, server_default="", comment="Agent版本"),
        sa.Column("app_version", sa.String(64), nullable=False, server_default="", comment="应用版本"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending", comment="节点状态"),
        sa.Column("enrolled_at", sa.DateTime(), nullable=True, comment="注册完成UTC"),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True, comment="最后心跳UTC"),
        sa.Column("last_metrics_at", sa.DateTime(), nullable=True, comment="最后指标UTC"),
        sa.Column("token_hash", sa.String(128), nullable=False, comment="节点访问令牌摘要"),
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="1", comment="令牌版本"),
        sa.Column("enrollment_expires_at", sa.DateTime(), nullable=True, comment="接入令牌过期UTC"),
        sa.Column("metadata_json", sa.JSON(), nullable=True, comment="附加元数据"),
        sa.UniqueConstraint("node_uuid", name="uq_server_nodes_node_uuid"),
        sa.UniqueConstraint("token_hash", name="uq_server_nodes_token_hash"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="租户服务器节点表",
    )
    op.create_index("ix_server_nodes_instance_id", "server_nodes", ["instance_id"])
    op.create_index("ix_server_nodes_status_last_seen", "server_nodes", ["status", "last_seen_at"])

    op.create_table(
        "node_metric_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("node_id", sa.BigInteger(), nullable=False, comment="节点ID"),
        sa.Column("sampled_at", sa.DateTime(), nullable=False, comment="采集时间UTC"),
        sa.Column("received_at", sa.DateTime(), nullable=False, comment="接收时间UTC"),
        sa.Column("cpu_percent", sa.Numeric(6, 2), nullable=False, server_default="0", comment="CPU使用率%"),
        sa.Column("load1", sa.Numeric(8, 2), nullable=False, server_default="0", comment="1分钟负载"),
        sa.Column("load5", sa.Numeric(8, 2), nullable=False, server_default="0", comment="5分钟负载"),
        sa.Column("load15", sa.Numeric(8, 2), nullable=False, server_default="0", comment="15分钟负载"),
        sa.Column("memory_total_mb", sa.BigInteger(), nullable=False, server_default="0", comment="总内存MB"),
        sa.Column("memory_used_mb", sa.BigInteger(), nullable=False, server_default="0", comment="已用内存MB"),
        sa.Column("memory_available_mb", sa.BigInteger(), nullable=False, server_default="0", comment="可用内存MB"),
        sa.Column("memory_percent", sa.Numeric(6, 2), nullable=False, server_default="0", comment="内存使用率%"),
        sa.Column("swap_total_mb", sa.BigInteger(), nullable=False, server_default="0", comment="Swap总量MB"),
        sa.Column("swap_used_mb", sa.BigInteger(), nullable=False, server_default="0", comment="Swap已用MB"),
        sa.Column("disks", sa.JSON(), nullable=True, comment="磁盘使用明细"),
        sa.Column("network", sa.JSON(), nullable=True, comment="网络速率"),
        sa.Column("services", sa.JSON(), nullable=True, comment="服务/容器状态"),
        sa.Column("public_ip", sa.String(64), nullable=False, server_default="", comment="Agent观测公网IP"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="节点指标原始样本表（默认保留7天）",
    )
    op.create_index("ix_node_metric_samples_node_sampled", "node_metric_samples", ["node_id", "sampled_at"])
    op.create_index("ix_node_metric_samples_received", "node_metric_samples", ["received_at"])

    op.create_table(
        "node_checks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column("node_id", sa.BigInteger(), nullable=False, comment="节点ID"),
        sa.Column("checked_at", sa.DateTime(), nullable=False, comment="探测时间UTC"),
        sa.Column("target", sa.String(32), nullable=False, comment="探测目标"),
        sa.Column("url", sa.String(500), nullable=False, comment="探测URL"),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.text("0"), comment="是否可用"),
        sa.Column("status_code", sa.Integer(), nullable=True, comment="HTTP状态码"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0", comment="耗时毫秒"),
        sa.Column("error", sa.String(255), nullable=False, server_default="", comment="错误摘要"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="公网访问探测结果表",
    )
    op.create_index("ix_node_checks_node_checked", "node_checks", ["node_id", "checked_at"])

    op.create_table(
        "alert_events",
        *_base(),
        sa.Column("node_id", sa.BigInteger(), nullable=False, comment="节点ID"),
        sa.Column("instance_id", sa.BigInteger(), nullable=False, comment="实例ID"),
        sa.Column("alert_type", sa.String(64), nullable=False, comment="告警类型"),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warning", comment="warning/critical"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open", comment="open/acknowledged/resolved"),
        sa.Column("message", sa.String(500), nullable=False, server_default="", comment="告警说明"),
        sa.Column("metric_value", sa.String(64), nullable=False, server_default="", comment="触发值"),
        sa.Column("threshold", sa.String(64), nullable=False, server_default="", comment="阈值"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False, comment="首次触发UTC"),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, comment="最近触发UTC"),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True, comment="确认时间UTC"),
        sa.Column("resolved_at", sa.DateTime(), nullable=True, comment="恢复时间UTC"),
        sa.Column("notify_count", sa.Integer(), nullable=False, server_default="0", comment="通知次数"),
        sa.Column("last_notified_at", sa.DateTime(), nullable=True, comment="最后通知UTC"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        comment="运维告警事件表",
    )
    op.create_index("ix_alert_events_node_status", "alert_events", ["node_id", "status"])
    op.create_index("ix_alert_events_last_seen", "alert_events", ["last_seen_at"])


def downgrade() -> None:
    op.drop_table("alert_events")
    op.drop_table("node_checks")
    op.drop_table("node_metric_samples")
    op.drop_table("server_nodes")
    op.drop_table("tenant_instances")
