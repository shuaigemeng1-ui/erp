"""租户库模型：审计日志、文件、请求防重记录。

* 审计日志与业务写操作在同一租户库事务中写入（见 docs/architecture.md）。
* 审计 payload 禁止保存明文密码 / Token / 密钥。
* ``idempotency_records`` 用于写接口防重：唯一约束 (tenant_id, idempotency_key) 是最终保障。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import IdMixin, TenantBase, TenantIdMixin, TimestampMixin, table_options


class AuditLog(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """租户审计日志：成员、角色、范围、店铺、库存调整、任务重试等敏感操作。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_logs_tenant_action", "tenant_id", "action"),
        Index("ix_audit_logs_tenant_product_created", "tenant_id", "product_id", "created_at", "id"),
        table_options("租户审计日志表"),
    )

    actor_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="操作人用户ID（自动化任务为空）",
    )
    product_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="关联SPU商品ID")
    sku_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="关联SKU ID")
    actor_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="操作人姓名历史快照")
    action: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="操作编码（如 member.invite、role.update、inventory.adjust）",
    )
    resource_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="对象类型（member / role / store / product / order / task）",
    )
    resource_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="对象ID",
    )
    summary: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        comment="操作摘要（不含敏感信息）",
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="补充信息（JSON，禁止写入明文密码与令牌）",
    )
    ip: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="操作来源IP",
    )
    request_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="请求标识，便于日志关联",
    )


class FileRecord(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """文件记录：对象存储中的文件元数据；对象键必须包含 tenant_id。"""

    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("tenant_id", "object_key", name="uq_files_tenant_id_object_key"),
        Index("ix_files_tenant_id_created_at", "tenant_id", "created_at"),
        table_options("文件记录表"),
    )

    object_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="对象存储键（含 tenant_id 前缀，禁止跨租户复用）",
    )
    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="原始文件名",
    )
    content_type: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
        comment="内容类型（如 image/png）",
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="文件字节数",
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="上传人用户ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        comment="文件状态：active=可用，deleted=已删除",
    )
    storage_profile_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="上传时使用的平台存储配置ID（无外键）")
    storage_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="local", server_default="local", comment="文件存储厂商（历史文件为本地）")
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True, comment="上传成功后保存的长期访问URL")


class IdempotencyRecord(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """请求防重记录：重复请求返回首次结果，避免重复扣减或重复创建。"""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_idempotency_records_tenant_id_key",
        ),
        Index("ix_idempotency_records_expires_at", "expires_at"),
        table_options("请求防重记录表"),
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="幂等键（客户端请求头 X-Idempotency-Key）",
    )
    endpoint: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        comment="请求路径与方法",
    )
    request_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="请求体摘要（同一幂等键的请求体必须一致）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="processing",
        comment="处理状态：processing=处理中，completed=已完成",
    )
    response_code: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="首次响应的 HTTP 状态码",
    )
    response_body: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="首次响应体（JSON）",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="记录过期时间（UTC）",
    )
