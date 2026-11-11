"""中心库模型：套餐、套餐额度、租户订阅、平台审计。

平台管理员配置套餐与额度（不实现在线购买）；额度降低不删除已有数据，仅限制超额新增。
本文件只包含平台自身的四张表；``platform_channel_apps`` / ``channel_account_registry``
由 channels 模块（backend-channels）负责，勿在此重复定义，否则元数据表名冲突。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import CenterBase, IdMixin, TimestampMixin, utcnow, table_options


class Plan(IdMixin, TimestampMixin, CenterBase):
    """套餐：首版由平台管理员维护，不实现线上购买。"""

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("code", name="uq_plans_code"),
        table_options("套餐表"),
    )

    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="套餐编码（全局唯一）",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="套餐名称",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        comment="套餐状态：active=可分配，disabled=停用",
    )
    description: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        comment="套餐说明",
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否默认套餐（新租户未指定套餐时使用）",
    )


class PlanFeature(IdMixin, TimestampMixin, CenterBase):
    """套餐额度：一条记录代表某套餐对某功能编码的额度限制。"""

    __tablename__ = "plan_features"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "feature_code",
            name="uq_plan_features_plan_id_feature_code",
        ),
        Index("ix_plan_features_plan_id", "plan_id"),
        table_options("套餐额度表"),
    )

    plan_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="套餐ID（逻辑关联 plans.id，不建外键）",
    )
    feature_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="额度编码（如 max_members、max_stores、max_skus）",
    )
    limit_value: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="额度上限（空表示不限制）",
    )


class Subscription(IdMixin, TimestampMixin, CenterBase):
    """租户订阅：记录租户当前套餐与有效期；续期 / 调整保留历史行。"""

    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_tenant_id_status", "tenant_id", "status"),
        table_options("租户订阅表"),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户ID（关联 tenants.id，不建外键）",
    )
    plan_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="套餐ID（关联 plans.id，不建外键）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        comment="订阅状态：active=生效，expired=到期，cancelled=取消",
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        comment="生效时间（UTC）",
    )
    ends_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="到期时间（UTC，空表示长期有效）",
    )


class PlatformAuditLog(IdMixin, TimestampMixin, CenterBase):
    """平台管理审计：租户开通、暂停、套餐与渠道应用变更。"""

    __tablename__ = "platform_audit_logs"
    __table_args__ = (
        Index("ix_platform_audit_logs_tenant_id", "tenant_id"),
        Index("ix_platform_audit_logs_action", "action"),
        Index("ix_platform_audit_logs_created_at", "created_at"),
        table_options("平台管理审计表"),
    )

    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="操作人用户ID（系统任务为空）",
    )
    action: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="操作编码（如 tenant.create、tenant.suspend、plan.update）",
    )
    resource_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="对象类型（tenant / plan / channel_app）",
    )
    resource_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="对象ID",
    )
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="涉及租户ID（平台级操作可为空）",
    )
    summary: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        comment="操作摘要（不含密码与密钥）",
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="补充信息（JSON，禁止写入明文密码与密钥）",
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
