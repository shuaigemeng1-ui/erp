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
    actual_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="实际运行版本（Agent上报）"
    )
    target_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="升级目标版本（空表示无进行中的目标）"
    )
    agent_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default="", comment="Agent版本"
    )
    executor_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default="", comment="运维执行器版本"
    )
    db_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="当前数据库版本（Alembic revision）"
    )
    upgrade_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="unknown",
        server_default="unknown",
        comment="升级状态：pending/building/maintenance/migrating/verifying/succeeded/failed/unknown",
    )
    deployment_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="shared",
        server_default="shared",
        comment="部署模式：shared=中心共享，independent=独立部署（已迁出中心）",
    )
    migration_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="registered",
        server_default="registered",
        comment="迁移状态：registered=已登记，migrating=迁移中，migrated=已迁出，rollback=回滚",
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
    executor_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default="", comment="运维执行器版本"
    )
    protocol_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="Agent协议版本（整数，用于与发布版本最低协议比对）",
    )
    capabilities_json: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, comment="Agent能力列表（心跳上报）"
    )
    db_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="节点上报的数据库版本"
    )
    app_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="应用版本"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending", comment="状态：pending/online/offline/unknown"
    )
    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="注册完成时间UTC")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最后心跳UTC")
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最后任务执行心跳UTC"
    )
    last_metrics_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最后指标UTC")
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, comment="节点访问令牌摘要")
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"), comment="令牌版本"
    )
    enrollment_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="一次性接入令牌过期时间UTC"
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, comment="附加元数据")
    ssh_host: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default="", comment="SSH主机（空则使用探测IP）"
    )
    ssh_port: Mapped[int] = mapped_column(
        Integer, nullable=False, default=22, server_default=text("22"), comment="SSH端口"
    )
    ssh_username_cipher: Mapped[str] = mapped_column(
        String(1024), nullable=False, default="", server_default="", comment="SSH用户名密文"
    )
    ssh_password_cipher: Mapped[str] = mapped_column(
        String(2048), nullable=False, default="", server_default="", comment="SSH密码密文"
    )
    ssh_host_fingerprint: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
        server_default="",
        comment="SSH主机公钥指纹（SHA256:xxxx；用于连接前校验主机身份）",
    )
    ssh_host_fingerprint_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="主机指纹首次确认时间UTC"
    )
    ssh_fingerprint_algorithm: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="SHA256",
        server_default="SHA256",
        comment="主机指纹算法（当前固定SHA256，预留扩展）",
    )


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
    cert_days_left: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="HTTPS证书剩余天数")
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
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning", comment="告警级别：warning=警告，critical=严重")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", comment="告警状态：open=待处理，acknowledged=已确认，resolved=已恢复")
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
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active", comment="发布状态：draft=草稿，active=可用，retired=已退役")
    commit_sha: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        server_default="",
        comment="完整Git提交号（40或64位十六进制，旧记录为空）",
    )
    build_config_summary: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        default="",
        server_default="",
        comment="构建配置摘要（构建参数/依赖锁摘要）",
    )
    min_agent_protocol: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="最低Agent协议版本（整数）",
    )
    center_db_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        server_default="",
        comment="运行模式center的数据库目标版本（Alembic revision）",
    )
    tenant_db_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        server_default="",
        comment="运行模式tenant的数据库目标版本（Alembic revision）",
    )
    rollback_compatible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("0"),
        comment="是否允许旧应用兼容新库（回退兼容声明）",
    )
    build_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="built",
        server_default="built",
        comment="构建状态：built/verified/failed",
    )
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
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="任务类型：deploy=部署，upgrade=升级，restart=重启，backup=备份，logs=日志，config=配置")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending", comment="任务状态：pending=待领取，leased=已领取，running=执行中，succeeded=成功，failed=失败，unknown=结果待核对")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, comment="幂等键")
    target_version: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="", comment="目标版本")
    actual_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="执行后实际版本"
    )
    agent_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default="", comment="执行该任务的Agent版本"
    )
    agent_protocol_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="执行该任务的Agent协议版本（整数）",
    )
    db_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="执行后实际数据库版本"
    )
    upgrade_status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="",
        server_default="",
        comment="升级状态：pending/building/maintenance/migrating/verifying/succeeded/failed/unknown",
    )
    attempt_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default="", server_default="", comment="当前执行批次ID（每次领取重新生成）"
    )
    lease_token: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", comment="执行租约令牌（事件需回传，防旧租约写入）"
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Agent执行心跳UTC"
    )
    event_seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
        comment="已接收事件序号水位（去重与断线重放）",
    )
    checks_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="升级校验结果（数据库版本/服务就绪/业务只读）"
    )
    checks_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="",
        server_default="",
        comment="校验结论：passed/failed/partial/missing/空",
    )
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
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", server_default="pending", comment="步骤状态：pending=待执行，running=执行中，succeeded=成功，failed=失败，skipped=跳过")
    message: Mapped[str] = mapped_column(String(1000), nullable=False, default="", server_default="", comment="脱敏消息")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="开始UTC")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成UTC")


class BackupRecord(IdMixin, CenterBase):
    __tablename__ = "backup_records"
    __table_args__ = (
        Index("ix_backup_records_instance_started", "instance_id", "started_at"),
        Index("ix_backup_records_trigger_status", "trigger", "status"),
        table_options("备份记录表"),
    )

    instance_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="实例ID")
    node_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="节点ID")
    job_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="运维任务ID")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running", server_default="running", comment="备份状态：running=执行中，succeeded=成功，failed=失败")
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="", comment="备份存储键（本地备份目录或对象存储键）")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0", comment="备份大小字节")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="开始UTC")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成UTC")
    retention_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="保留到期UTC")
    error: Mapped[str] = mapped_column(String(500), nullable=False, default="", server_default="", comment="失败原因")
    # ---------------------------------------------------------------- 批次3：备份加密/异地/恢复演练
    trigger: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="scheduled",
        server_default="scheduled",
        comment="触发来源：manual=手工，scheduled=每日定时，pre_upgrade=升级前额外备份",
    )
    local_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="本地生成状态：pending=待生成，generated=已生成，verified=已校验，failed=失败",
    )
    local_path: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default="", comment="本地备份目录（Git 工作目录之外）"
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="本地完整性校验通过时间（UTC）"
    )
    checksum_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="校验状态：pending=待校验，verified=校验通过，failed=校验失败",
    )
    checksum: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default="", comment="加密包/归档校验值（sha256:<hex>）"
    )
    offsite_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="异地上传状态：pending=待上传，uploaded=已上传，failed=失败，skipped=未配置异地",
    )
    offsite_key: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default="", comment="异地私有存储对象键"
    )
    offsite_bucket: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default="", comment="异地私有存储桶（不复用商品图片公开目录）"
    )
    encryption: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default="", comment="异地包加密算法（如 aes-256-cbc/pbkdf2）"
    )
    restore_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="not_tested",
        server_default="not_tested",
        comment="恢复验证状态：not_tested=未演练，verified=隔离恢复验证通过，failed=验证失败",
    )
    restore_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="隔离环境恢复验证通过时间（UTC）"
    )
    restore_notes: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default="", comment="恢复演练结论/证据摘要"
    )
    failure_reason: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default="", comment="失败原因（本地/校验/异地/恢复任一阶段）"
    )
    stages: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True, comment="阶段状态流水（生成/校验/上传/恢复，JSON 数组）"
    )
    manifest_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="备份清单（数据库/配置/本地文件/校验结果 JSON）"
    )


class MaintenanceWindow(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "maintenance_windows"
    __table_args__ = (
        Index("ix_maintenance_windows_instance_time", "instance_id", "starts_at", "ends_at"),
        table_options("实例维护窗口表"),
    )

    instance_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="实例ID")
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="开始时间UTC")
    ends_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="结束时间UTC")
    reason: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="", comment="维护原因")
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="创建人用户ID")


class NodeMetricAggregate(IdMixin, CenterBase):
    __tablename__ = "node_metric_aggregates"
    __table_args__ = (
        UniqueConstraint("node_id", "resolution", "bucket_start", name="uq_node_metric_agg_bucket"),
        Index("ix_node_metric_agg_node_res_start", "node_id", "resolution", "bucket_start"),
        table_options("节点指标聚合表"),
    )

    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="节点ID")
    resolution: Mapped[str] = mapped_column(String(8), nullable=False, comment="聚合粒度：5m/1h")
    bucket_start: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="桶开始UTC")
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="样本数")
    cpu_avg: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=0, comment="CPU平均%")
    cpu_max: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=0, comment="CPU最大%")
    memory_avg: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=0, comment="内存平均%")
    memory_max: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=0, comment="内存最大%")
    load_avg: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0, comment="负载平均")
    disk_max: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=0, comment="磁盘最高%")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="创建UTC")
