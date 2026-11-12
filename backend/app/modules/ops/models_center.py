"""统一运维中心：租户实例、服务器节点、指标、探测与告警。

所有关联均为逻辑ID，不创建外键；表与字段使用中文注释。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import CenterBase, IdMixin, TimestampMixin, table_options


class TenantInstance(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "tenant_instances"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_instances_tenant_id"),
        UniqueConstraint("domain", name="uq_tenant_instances_domain"),
        table_options("租户独立实例表"),
    )

    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="租户ID")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="实例名称")
    domain: Mapped[str] = mapped_column(String(255), nullable=False, comment="业务域名（不含协议）")
    environment: Mapped[str] = mapped_column(
        String(16), nullable=False, default="production", server_default="production", comment="环境"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending", comment="状态：pending/active/suspended"
    )
    app_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="当前部署版本"
    )
    notes: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default="", comment="备注"
    )


class ServerNode(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "server_nodes"
    __table_args__ = (
        UniqueConstraint("node_uuid", name="uq_server_nodes_node_uuid"),
        UniqueConstraint("token_hash", name="uq_server_nodes_token_hash"),
        Index("ix_server_nodes_instance_id", "instance_id"),
        Index("ix_server_nodes_status_last_seen", "status", "last_seen_at"),
        table_options("租户服务器节点表"),
    )

    instance_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="实例ID")
    tenant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="租户ID（冗余便于查询）")
    node_uuid: Mapped[str] = mapped_column(String(36), nullable=False, comment="节点固定UUID")
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="", comment="主机名")
    os_name: Mapped[str] = mapped_column(String(100), nullable=False, default="", server_default="", comment="操作系统")
    arch: Mapped[str] = mapped_column(String(32), nullable=False, default="", server_default="", comment="CPU架构")
    cpu_cores: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"), comment="CPU核数")
    memory_total_mb: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"), comment="总内存MB"
    )
    disk_total_gb: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"), comment="总磁盘GB"
    )
    internal_ips: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, comment="内网IP列表")
    configured_public_ip: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="配置的公网IP"
    )
    observed_public_ip: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="Agent观测到的出口IP"
    )
    agent_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default="", comment="Agent版本"
    )
    app_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="应用版本"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending", comment="状态：pending/online/offline/unknown"
    )
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="注册完成时间UTC")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最后心跳UTC")
    last_metrics_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最后指标UTC")
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, comment="节点访问令牌摘要")
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"), comment="令牌版本"
    )
    enrollment_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="一次性接入令牌过期时间UTC"
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="附加元数据")


class NodeMetricSample(IdMixin, CenterBase):
    __tablename__ = "node_metric_samples"
    __table_args__ = (
        Index("ix_node_metric_samples_node_sampled", "node_id", "sampled_at"),
        Index("ix_node_metric_samples_received", "received_at"),
        table_options("节点指标原始样本表（默认保留7天）"),
    )

    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="节点ID")
    sampled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="采集时间UTC")
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="接收时间UTC")
    cpu_percent: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=0, comment="CPU使用率%")
    load1: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0, comment="1分钟负载")
    load5: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0, comment="5分钟负载")
    load15: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0, comment="15分钟负载")
    memory_total_mb: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="总内存MB")
    memory_used_mb: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="已用内存MB")
    memory_available_mb: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="可用内存MB")
    memory_percent: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=0, comment="内存使用率%")
    swap_total_mb: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="Swap总量MB")
    swap_used_mb: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, comment="Swap已用MB")
    disks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True, comment="磁盘使用明细")
    network: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="网络速率")
    services: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="服务/容器状态")
    public_ip: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="Agent观测的公网IP"
    )


class NodeCheck(IdMixin, CenterBase):
    __tablename__ = "node_checks"
    __table_args__ = (
        Index("ix_node_checks_node_checked", "node_id", "checked_at"),
        table_options("公网访问探测结果表"),
    )

    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="节点ID")
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="探测时间UTC")
    target: Mapped[str] = mapped_column(String(32), nullable=False, comment="探测目标：root/healthz/api")
    url: Mapped[str] = mapped_column(String(500), nullable=False, comment="探测URL")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否可用")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="HTTP状态码")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="总耗时毫秒")
    error: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="", comment="错误摘要")


class AlertEvent(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "alert_events"
    __table_args__ = (
        Index("ix_alert_events_node_status", "node_id", "status"),
        Index("ix_alert_events_last_seen", "last_seen_at"),
        table_options("运维告警事件表"),
    )

    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="节点ID")
    instance_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="实例ID")
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="告警类型")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning", comment="warning/critical")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", comment="open/acknowledged/resolved")
    message: Mapped[str] = mapped_column(String(500), nullable=False, default="", comment="告警说明")
    metric_value: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="触发值")
    threshold: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="阈值")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="首次触发UTC")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="最近触发UTC")
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="确认时间UTC")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="恢复时间UTC")
    notify_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="通知次数")
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最后通知UTC")


class ReleaseVersion(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "release_versions"
    __table_args__ = (
        UniqueConstraint("version", name="uq_release_versions_version"),
        table_options("发布版本表"),
    )

    version: Mapped[str] = mapped_column(String(64), nullable=False, comment="版本号")
    image_tag: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="", comment="镜像标签")
    image_digest: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="", comment="镜像摘要")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active", comment="draft/active/retired")
    notes: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="", comment="发布说明")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="创建人用户ID")


class OperationJob(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "operation_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operation_jobs_idempotency_key"),
        Index("ix_operation_jobs_instance_status", "instance_id", "status"),
        Index("ix_operation_jobs_node_status", "node_id", "status"),
        table_options("运维任务表"),
    )

    instance_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="实例ID")
    node_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="节点ID")
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="deploy/upgrade/restart/backup/logs/config")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending", comment="pending/leased/running/succeeded/failed/unknown")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, comment="幂等键")
    target_version: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="", comment="目标版本")
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="任务参数")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="执行结果")
    error: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="", comment="失败原因")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="已领取次数")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="执行租约到期UTC")
    requested_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="发起人用户ID")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始执行UTC")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成时间UTC")


class OperationJobStep(IdMixin, CenterBase):
    __tablename__ = "operation_job_steps"
    __table_args__ = (
        UniqueConstraint("job_id", "step_no", name="uq_operation_job_steps_job_step"),
        Index("ix_operation_job_steps_job", "job_id"),
        table_options("运维任务步骤表"),
    )

    job_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="任务ID")
    step_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="步骤序号")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="步骤名称")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending", comment="pending/running/succeeded/failed/skipped")
    message: Mapped[str] = mapped_column(String(1000), nullable=False, default="", server_default="", comment="脱敏消息")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始UTC")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成UTC")


class BackupRecord(IdMixin, CenterBase):
    __tablename__ = "backup_records"
    __table_args__ = (
        Index("ix_backup_records_instance_started", "instance_id", "started_at"),
        table_options("备份记录表"),
    )

    instance_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="实例ID")
    node_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="节点ID")
    job_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="运维任务ID")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", server_default="running", comment="running/succeeded/failed")
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="", comment="备份存储键")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0", comment="备份大小字节")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="开始UTC")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成UTC")
    retention_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="保留到期UTC")
    error: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="", comment="失败原因")
