"""中心库模型：用户、租户、成员关系、邀请、数据库路由、开通任务、平台角色。

硬性规范（见 plan.txt §2「无外键规范」与 docs/data-model.md）：

* 禁止声明 ``ForeignKey`` / ``ForeignKeyConstraint``，跨表关联只存普通 ID 列；
* 每张表与每个字段都有中文注释；
* 业务唯一性用唯一约束表达（邮箱、租户编码、成员关系、平台角色绑定）；
* 时间统一 UTC；状态字段用 ``VARCHAR(32)`` + 代码常量。

租户库中的角色、权限与数据范围不在这里定义（见 ``models_tenant``）。
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
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import CenterBase, IdMixin, TimestampMixin, table_options
from app.modules.identity.constants import (
    InvitationStatus,
    MembershipStatus,
    ProvisioningStatus,
    TENANT_DATABASE_READY,
    TenantStatus,
    UserStatus,
)


class User(IdMixin, TimestampMixin, CenterBase):
    """全局用户：一个用户可以加入多个租户，在不同租户拥有不同角色。"""

    __tablename__ = "users"
    __table_args__ = (table_options("用户表（全局身份）"),)

    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        comment="登录邮箱（全局唯一，统一小写存储）",
    )
    password_hash: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="密码哈希（bcrypt；受邀未激活时为空）",
    )
    display_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="",
        comment="显示名称",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=UserStatus.ACTIVE.value,
        comment="账号状态：active=正常，disabled=停用",
    )
    locale: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="zh-CN",
        comment="语言区域，如 zh-CN",
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="最近登录时间（UTC）",
    )


class Tenant(IdMixin, TimestampMixin, CenterBase):
    """租户（经营主体）：每个租户拥有独立业务库、成员与店铺。"""

    __tablename__ = "tenants"
    __table_args__ = (
        Index("ix_tenants_status", "status"),
        table_options("租户表（经营主体）"),
    )

    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="租户编码（全局唯一，用于数据库命名与展示）",
    )
    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="租户名称",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TenantStatus.PROVISIONING.value,
        comment="租户状态：provisioning=开通中，active=可用，suspended=暂停，provision_failed=开通失败",
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="Asia/Shanghai",
        comment="租户时区（数据库存 UTC，按时区展示）",
    )
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="套餐ID（关联 plans.id，逻辑关联不建外键）",
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="负责人用户ID（关联 users.id，逻辑关联不建外键）",
    )
    next_member_no: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
        comment="下一个租户内成员编号（从1开始，已分配编号不重复使用）",
    )
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="暂停时间（UTC）",
    )
    suspended_reason: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="暂停原因",
    )


class TenantMembership(IdMixin, TimestampMixin, CenterBase):
    """租户成员关系：同一用户在不同租户拥有独立成员身份。"""

    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_id_user_id"),
        UniqueConstraint("tenant_id", "member_no", name="uq_tenant_memberships_tenant_member_no"),
        Index("ix_tenant_memberships_user_id", "user_id"),
        table_options("租户成员关系表"),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户ID（关联 tenants.id，不建外键）",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="用户ID（关联 users.id，不建外键）",
    )
    member_no: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户内成员编号（每个租户从1递增，内部关联仍使用全局主键ID）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=MembershipStatus.INVITED.value,
        comment="成员状态：active=正常，invited=已邀请未激活，disabled=已停用",
    )
    is_owner: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否租户负责人",
    )
    joined_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="加入时间（UTC，接受邀请时写入）",
    )


class TenantInvitation(IdMixin, TimestampMixin, CenterBase):
    """租户邀请：令牌只存 SHA-256 摘要，有时效且只能使用一次。"""

    __tablename__ = "tenant_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_tenant_invitations_token_hash"),
        Index("ix_tenant_invitations_tenant_id_status", "tenant_id", "status"),
        table_options("租户邀请表"),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户ID（关联 tenants.id，不建外键）",
    )
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="受邀邮箱（统一小写）",
    )
    token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="邀请令牌 SHA-256 摘要（不保存原文）",
    )
    role_ids: Mapped[list[Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="邀请时预分配的角色与数据范围（JSON：[{role_id, scopes}]）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=InvitationStatus.PENDING.value,
        comment="邀请状态：pending=待接受，accepted=已接受，revoked=已撤销，expired=已过期",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="过期时间（UTC）",
    )
    invited_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="邀请人用户ID",
    )
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="接受时间（UTC）",
    )
    accepted_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="接受邀请后对应的用户ID",
    )


class TenantDatabase(IdMixin, TimestampMixin, CenterBase):
    """租户数据库路由：业务库地址与受限账号，凭据加密保存。"""

    __tablename__ = "tenant_databases"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_databases_tenant_id"),
        table_options("租户数据库路由表"),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户ID（一个租户一个业务库）",
    )
    host: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="数据库主机",
    )
    port: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=3306,
        comment="数据库端口",
    )
    database_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="业务库名",
    )
    username: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="受限业务账号（只授予本库业务读写权限）",
    )
    password_cipher: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="受限账号密码密文（AES 加密）",
    )
    credentials_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="凭据版本（凭据轮换后递增，用于丢弃旧连接池缓存）",
    )
    schema_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="",
        comment="已执行的迁移版本（alembic revision）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TENANT_DATABASE_READY,
        comment="连接状态：ready=可用，unavailable=不可用",
    )
    last_migrated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="最近迁移时间（UTC）",
    )


class ProvisioningJob(IdMixin, TimestampMixin, CenterBase):
    """开通任务：每个开通步骤一行，可重试、失败不激活租户。"""

    __tablename__ = "provisioning_jobs"
    __table_args__ = (
        Index("ix_provisioning_jobs_tenant_id_step", "tenant_id", "step"),
        table_options("租户开通任务表"),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户ID（关联 tenants.id，不建外键）",
    )
    step: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="开通步骤编码：register/create_database/run_migrations/init_settings/"
        "init_roles/init_stock_place/init_subscription/invite_owner/activate",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProvisioningStatus.PENDING.value,
        comment="步骤状态：pending=待执行，running=执行中，succeeded=成功，failed=失败",
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="已尝试次数",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="失败原因（成功时为空）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="开始时间（UTC）",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="结束时间（UTC）",
    )


class PlatformRole(IdMixin, TimestampMixin, CenterBase):
    """平台角色：平台管理员 / 平台运营，仅由初始化命令授权。"""

    __tablename__ = "platform_roles"
    __table_args__ = (
        UniqueConstraint("code", name="uq_platform_roles_code"),
        table_options("平台角色表"),
    )

    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="平台角色编码（platform_admin / platform_ops）",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="平台角色名称",
    )
    description: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        comment="角色说明",
    )


class PlatformUserRole(IdMixin, TimestampMixin, CenterBase):
    """平台用户与平台角色的关联（不开放公共管理员注册）。"""

    __tablename__ = "platform_user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_platform_user_roles_user_id_role_id"),
        Index("ix_platform_user_roles_role_id", "role_id"),
        table_options("平台用户角色关联表"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="用户ID（关联 users.id，不建外键）",
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="平台角色ID（关联 platform_roles.id，不建外键）",
    )
    granted_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="授权人用户ID（初始化命令为空）",
    )
