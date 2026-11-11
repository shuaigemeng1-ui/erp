"""中心库基线：身份、租户、开通、平台套餐与平台审计（center_0001）。

基线迁移，不依赖任何历史版本：

* 全部表禁止外键（plan.txt §2「无外键规范」），关联只存普通 ID 列并建索引；
* 每张表与每个字段都有中文注释；
* 列类型 / 可空性 / 默认值 / 注释与 ``app/modules/{identity,platform}/models_center.py`` 保持一致；
* 迁移由独立发布任务执行，API 启动时不自动迁移。

Revision ID: center_0001
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.base import table_options

revision = "center_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ 用户
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=255), nullable=False, comment="登录邮箱（全局唯一，统一小写存储）"),
        sa.Column("password_hash", sa.String(length=255), nullable=True, comment="密码哈希（bcrypt；受邀未激活时为空）"),
        sa.Column("display_name", sa.String(length=100), nullable=False, comment="显示名称"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="账号状态：active=正常，disabled=停用",
        ),
        sa.Column("locale", sa.String(length=16), nullable=False, comment="语言区域，如 zh-CN"),
        sa.Column("last_login_at", sa.DateTime(), nullable=True, comment="最近登录时间（UTC）"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        **table_options("用户表（全局身份）"),
    )

    # ------------------------------------------------------------------ 平台角色
    op.create_table(
        "platform_roles",
        sa.Column("code", sa.String(length=64), nullable=False, comment="平台角色编码（platform_admin / platform_ops）"),
        sa.Column("name", sa.String(length=64), nullable=False, comment="平台角色名称"),
        sa.Column("description", sa.String(length=255), nullable=False, comment="角色说明"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_roles"),
        sa.UniqueConstraint("code", name="uq_platform_roles_code"),
        **table_options("平台角色表"),
    )

    op.create_table(
        "platform_user_roles",
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="用户ID（关联 users.id，不建外键）"),
        sa.Column("role_id", sa.BigInteger(), nullable=False, comment="平台角色ID（关联 platform_roles.id，不建外键）"),
        sa.Column("granted_by", sa.BigInteger(), nullable=True, comment="授权人用户ID（初始化命令为空）"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_user_roles"),
        sa.UniqueConstraint("user_id", "role_id", name="uq_platform_user_roles_user_id_role_id"),
        **table_options("平台用户角色关联表"),
    )
    op.create_index("ix_platform_user_roles_role_id", "platform_user_roles", ["role_id"], unique=False)

    # ------------------------------------------------------------------ 套餐与订阅
    op.create_table(
        "plans",
        sa.Column("code", sa.String(length=64), nullable=False, comment="套餐编码（全局唯一）"),
        sa.Column("name", sa.String(length=64), nullable=False, comment="套餐名称"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="套餐状态：active=可分配，disabled=停用"),
        sa.Column("description", sa.String(length=255), nullable=False, comment="套餐说明"),
        sa.Column("is_default", sa.Boolean(), nullable=False, comment="是否默认套餐（新租户未指定套餐时使用）"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plans"),
        sa.UniqueConstraint("code", name="uq_plans_code"),
        **table_options("套餐表"),
    )

    op.create_table(
        "plan_features",
        sa.Column("plan_id", sa.BigInteger(), nullable=False, comment="套餐ID（逻辑关联 plans.id，不建外键）"),
        sa.Column(
            "feature_code",
            sa.String(length=64),
            nullable=False,
            comment="额度编码（如 max_members、max_stores、max_skus）",
        ),
        sa.Column("limit_value", sa.Integer(), nullable=True, comment="额度上限（空表示不限制）"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plan_features"),
        sa.UniqueConstraint("plan_id", "feature_code", name="uq_plan_features_plan_id_feature_code"),
        **table_options("套餐额度表"),
    )
    op.create_index("ix_plan_features_plan_id", "plan_features", ["plan_id"], unique=False)

    # ------------------------------------------------------------------ 租户与成员
    op.create_table(
        "tenants",
        sa.Column("code", sa.String(length=64), nullable=False, comment="租户编码（全局唯一，用于数据库命名与展示）"),
        sa.Column("name", sa.String(length=128), nullable=False, comment="租户名称"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="租户状态：provisioning=开通中，active=可用，suspended=暂停，provision_failed=开通失败",
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            comment="租户时区（数据库存 UTC，按时区展示）",
        ),
        sa.Column("plan_id", sa.BigInteger(), nullable=True, comment="套餐ID（关联 plans.id，逻辑关联不建外键）"),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=True, comment="负责人用户ID（关联 users.id，逻辑关联不建外键）"),
        sa.Column("suspended_at", sa.DateTime(), nullable=True, comment="暂停时间（UTC）"),
        sa.Column("suspended_reason", sa.String(length=255), nullable=True, comment="暂停原因"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
        sa.UniqueConstraint("code", name="uq_tenants_code"),
        **table_options("租户表（经营主体）"),
    )
    op.create_index("ix_tenants_status", "tenants", ["status"], unique=False)

    op.create_table(
        "tenant_memberships",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID（关联 tenants.id，不建外键）"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="用户ID（关联 users.id，不建外键）"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="成员状态：active=正常，invited=已邀请未激活，disabled=已停用",
        ),
        sa.Column("is_owner", sa.Boolean(), nullable=False, comment="是否租户负责人"),
        sa.Column("joined_at", sa.DateTime(), nullable=True, comment="加入时间（UTC，接受邀请时写入）"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_memberships"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_id_user_id"),
        **table_options("租户成员关系表"),
    )
    op.create_index("ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"], unique=False)

    op.create_table(
        "tenant_invitations",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID（关联 tenants.id，不建外键）"),
        sa.Column("email", sa.String(length=255), nullable=False, comment="受邀邮箱（统一小写）"),
        sa.Column("token_hash", sa.String(length=64), nullable=False, comment="邀请令牌 SHA-256 摘要（不保存原文）"),
        sa.Column(
            "role_ids",
            sa.JSON(),
            nullable=True,
            comment="邀请时预分配的角色与数据范围（JSON：[{role_id, scopes}]）",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="邀请状态：pending=待接受，accepted=已接受，revoked=已撤销，expired=已过期",
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False, comment="过期时间（UTC）"),
        sa.Column("invited_by", sa.BigInteger(), nullable=True, comment="邀请人用户ID"),
        sa.Column("accepted_at", sa.DateTime(), nullable=True, comment="接受时间（UTC）"),
        sa.Column("accepted_user_id", sa.BigInteger(), nullable=True, comment="接受邀请后对应的用户ID"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_invitations"),
        sa.UniqueConstraint("token_hash", name="uq_tenant_invitations_token_hash"),
        **table_options("租户邀请表"),
    )
    op.create_index(
        "ix_tenant_invitations_tenant_id_status", "tenant_invitations", ["tenant_id", "status"], unique=False
    )

    # ------------------------------------------------------------------ 数据库路由与开通任务
    op.create_table(
        "tenant_databases",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID（一个租户一个业务库）"),
        sa.Column("host", sa.String(length=255), nullable=False, comment="数据库主机"),
        sa.Column("port", sa.Integer(), nullable=False, comment="数据库端口"),
        sa.Column("database_name", sa.String(length=64), nullable=False, comment="业务库名"),
        sa.Column(
            "username",
            sa.String(length=64),
            nullable=False,
            comment="受限业务账号（只授予本库业务读写权限）",
        ),
        sa.Column(
            "password_cipher",
            sa.String(length=512),
            nullable=False,
            comment="受限账号密码密文（AES 加密）",
        ),
        sa.Column(
            "credentials_version",
            sa.Integer(),
            nullable=False,
            comment="凭据版本（凭据轮换后递增，用于丢弃旧连接池缓存）",
        ),
        sa.Column(
            "schema_version",
            sa.String(length=64),
            nullable=False,
            comment="已执行的迁移版本（alembic revision）",
        ),
        sa.Column("status", sa.String(length=32), nullable=False, comment="连接状态：ready=可用，unavailable=不可用"),
        sa.Column("last_migrated_at", sa.DateTime(), nullable=True, comment="最近迁移时间（UTC）"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_databases"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_databases_tenant_id"),
        **table_options("租户数据库路由表"),
    )

    op.create_table(
        "provisioning_jobs",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID（关联 tenants.id，不建外键）"),
        sa.Column(
            "step",
            sa.String(length=64),
            nullable=False,
            comment="开通步骤编码：register/create_database/run_migrations/init_settings/"
            "init_roles/init_stock_place/init_subscription/invite_owner/activate",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="步骤状态：pending=待执行，running=执行中，succeeded=成功，failed=失败",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, comment="已尝试次数"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="失败原因（成功时为空）"),
        sa.Column("started_at", sa.DateTime(), nullable=True, comment="开始时间（UTC）"),
        sa.Column("finished_at", sa.DateTime(), nullable=True, comment="结束时间（UTC）"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_provisioning_jobs"),
        **table_options("租户开通任务表"),
    )
    op.create_index("ix_provisioning_jobs_tenant_id_step", "provisioning_jobs", ["tenant_id", "step"], unique=False)

    op.create_table(
        "subscriptions",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID（关联 tenants.id，不建外键）"),
        sa.Column("plan_id", sa.BigInteger(), nullable=False, comment="套餐ID（关联 plans.id，不建外键）"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="订阅状态：active=生效，expired=到期，cancelled=取消"),
        sa.Column("starts_at", sa.DateTime(), nullable=False, comment="生效时间（UTC）"),
        sa.Column("ends_at", sa.DateTime(), nullable=True, comment="到期时间（UTC，空表示长期有效）"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_subscriptions"),
        **table_options("租户订阅表"),
    )
    op.create_index("ix_subscriptions_tenant_id_status", "subscriptions", ["tenant_id", "status"], unique=False)

    # ------------------------------------------------------------------ 平台审计
    op.create_table(
        "platform_audit_logs",
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True, comment="操作人用户ID（系统任务为空）"),
        sa.Column(
            "action",
            sa.String(length=128),
            nullable=False,
            comment="操作编码（如 tenant.create、tenant.suspend、plan.update）",
        ),
        sa.Column(
            "resource_type",
            sa.String(length=64),
            nullable=False,
            comment="对象类型（tenant / plan / channel_app）",
        ),
        sa.Column("resource_id", sa.BigInteger(), nullable=True, comment="对象ID"),
        sa.Column("tenant_id", sa.BigInteger(), nullable=True, comment="涉及租户ID（平台级操作可为空）"),
        sa.Column("summary", sa.String(length=255), nullable=False, comment="操作摘要（不含密码与密钥）"),
        sa.Column("payload", sa.JSON(), nullable=True, comment="补充信息（JSON，禁止写入明文密码与密钥）"),
        sa.Column("ip", sa.String(length=64), nullable=False, comment="操作来源IP"),
        sa.Column("request_id", sa.String(length=64), nullable=False, comment="请求标识，便于日志关联"),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False, comment="主键ID"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="更新时间（UTC）",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_platform_audit_logs"),
        **table_options("平台管理审计表"),
    )
    op.create_index("ix_platform_audit_logs_tenant_id", "platform_audit_logs", ["tenant_id"], unique=False)
    op.create_index("ix_platform_audit_logs_action", "platform_audit_logs", ["action"], unique=False)
    op.create_index("ix_platform_audit_logs_created_at", "platform_audit_logs", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_platform_audit_logs_created_at", table_name="platform_audit_logs")
    op.drop_index("ix_platform_audit_logs_action", table_name="platform_audit_logs")
    op.drop_index("ix_platform_audit_logs_tenant_id", table_name="platform_audit_logs")
    op.drop_table("platform_audit_logs")

    op.drop_index("ix_subscriptions_tenant_id_status", table_name="subscriptions")
    op.drop_table("subscriptions")

    op.drop_index("ix_provisioning_jobs_tenant_id_step", table_name="provisioning_jobs")
    op.drop_table("provisioning_jobs")

    op.drop_table("tenant_databases")

    op.drop_index("ix_tenant_invitations_tenant_id_status", table_name="tenant_invitations")
    op.drop_table("tenant_invitations")

    op.drop_index("ix_tenant_memberships_user_id", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")

    op.drop_index("ix_tenants_status", table_name="tenants")
    op.drop_table("tenants")

    op.drop_index("ix_plan_features_plan_id", table_name="plan_features")
    op.drop_table("plan_features")

    op.drop_table("plans")

    op.drop_index("ix_platform_user_roles_role_id", table_name="platform_user_roles")
    op.drop_table("platform_user_roles")

    op.drop_table("platform_roles")

    op.drop_table("users")
