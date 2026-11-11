"""租户库基线：角色、权限、绑定范围、租户设置、功能开关、审计与支撑表（tenant_0001）。

基线迁移，不依赖任何历史版本：

* 全部表禁止外键（plan.txt §2「无外键规范」），``member_id`` / ``role_id`` 等只存普通 ID 列；
* 每张表都带 ``tenant_id``（便于审计、队列、缓存定位）；跨租户隔离由数据库本身承担；
* 每张表与每个字段都有中文注释；
* 列类型 / 可空性 / 默认值 / 注释与 ``app/modules/{identity,access,audit}/models_tenant.py`` 保持一致；
* 由开通任务或独立发布任务按租户执行：``alembic -c alembic/tenant.ini -x db_url=... upgrade head``。

Revision ID: tenant_0001
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.base import table_options

revision = "tenant_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ 角色与权限
    op.create_table(
        "roles",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column(
            "code",
            sa.String(length=64),
            nullable=False,
            comment="角色编码（租户内唯一；预置角色编码保持不变以便升级对照）",
        ),
        sa.Column("name", sa.String(length=64), nullable=False, comment="角色名称"),
        sa.Column("description", sa.String(length=255), nullable=False, comment="角色说明"),
        sa.Column(
            "is_system",
            sa.Boolean(),
            nullable=False,
            comment="是否系统预置角色（预置角色不可删除，可复制后修改）",
        ),
        sa.Column(
            "copied_from_role_id",
            sa.BigInteger(),
            nullable=True,
            comment="复制来源角色ID（逻辑关联 roles.id，不建外键）",
        ),
        sa.Column("status", sa.String(length=32), nullable=False, comment="角色状态：active=启用，disabled=停用"),
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
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("tenant_id", "code", name="uq_roles_tenant_id_code"),
        **table_options("租户角色表"),
    )
    op.create_index("ix_roles_tenant_id", "roles", ["tenant_id"], unique=False)
    op.create_index("ix_roles_tenant_id_status", "roles", ["tenant_id", "status"], unique=False)

    op.create_table(
        "role_permissions",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("role_id", sa.BigInteger(), nullable=False, comment="角色ID（逻辑关联 roles.id，不建外键）"),
        sa.Column(
            "permission_code",
            sa.String(length=64),
            nullable=False,
            comment="权限编码（见 app/core/permissions.py）",
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_role_permissions"),
        sa.UniqueConstraint(
            "tenant_id",
            "role_id",
            "permission_code",
            name="uq_role_permissions_tenant_role_permission",
        ),
        **table_options("角色权限关联表"),
    )
    op.create_index("ix_role_permissions_tenant_id", "role_permissions", ["tenant_id"], unique=False)
    op.create_index("ix_role_permissions_role_id", "role_permissions", ["role_id"], unique=False)

    op.create_table(
        "member_roles",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("role_id", sa.BigInteger(), nullable=False, comment="角色ID（逻辑关联 roles.id，不建外键）"),
        sa.Column(
            "member_id",
            sa.BigInteger(),
            nullable=False,
            comment="成员ID（中心库 tenant_memberships.id，不建外键）",
        ),
        sa.Column("assigned_by", sa.BigInteger(), nullable=True, comment="分配人用户ID（自动化 / 邀请激活时为空）"),
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
        sa.PrimaryKeyConstraint("id", name="pk_member_roles"),
        sa.UniqueConstraint("tenant_id", "member_id", "role_id", name="uq_member_roles_tenant_member_role"),
        **table_options("成员角色绑定表"),
    )
    op.create_index("ix_member_roles_tenant_id", "member_roles", ["tenant_id"], unique=False)
    op.create_index("ix_member_roles_member_id", "member_roles", ["member_id"], unique=False)
    op.create_index("ix_member_roles_role_id", "member_roles", ["role_id"], unique=False)

    # ------------------------------------------------------------------ 数据范围与租户设置
    op.create_table(
        "role_binding_scopes",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column(
            "member_role_id",
            sa.BigInteger(),
            nullable=False,
            comment="成员角色绑定ID（逻辑关联 member_roles.id，不建外键）",
        ),
        sa.Column(
            "scope_type",
            sa.String(length=32),
            nullable=False,
            comment="范围类型：all=租户全部，stores=指定店铺，stock_places=指定库存地点",
        ),
        sa.Column(
            "scope_id",
            sa.BigInteger(),
            nullable=False,
            comment="范围对象ID（店铺ID或库存地点ID；scope_type=all 时为 0）",
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_role_binding_scopes"),
        sa.UniqueConstraint(
            "tenant_id",
            "member_role_id",
            "scope_type",
            "scope_id",
            name="uq_role_binding_scopes_binding_scope",
        ),
        **table_options("角色绑定数据范围表"),
    )
    op.create_index("ix_role_binding_scopes_tenant_id", "role_binding_scopes", ["tenant_id"], unique=False)
    op.create_index(
        "ix_role_binding_scopes_member_role_id",
        "role_binding_scopes",
        ["member_role_id"],
        unique=False,
    )

    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column(
            "permission_version",
            sa.Integer(),
            nullable=False,
            comment="权限版本号（成员/角色/数据范围变更后递增，用于缓存失效）",
        ),
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            comment="租户时区（数据库存 UTC，按时区展示）",
        ),
        sa.Column("company_name", sa.String(length=128), nullable=False, comment="公司名称（展示用）"),
        sa.Column("order_sync_enabled", sa.Boolean(), nullable=False, comment="是否允许主动拉取订单"),
        sa.Column(
            "stock_push_enabled",
            sa.Boolean(),
            nullable=False,
            comment="是否允许向平台推送可售库存（真实推送默认关闭）",
        ),
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
        sa.PrimaryKeyConstraint("id", name="pk_tenant_settings"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_settings_tenant_id"),
        **table_options("租户设置表"),
    )
    op.create_index("ix_tenant_settings_tenant_id", "tenant_settings", ["tenant_id"], unique=False)

    op.create_table(
        "feature_settings",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column(
            "feature_code",
            sa.String(length=64),
            nullable=False,
            comment="功能编码（如 order_sync、stock_push、channel_listing）",
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="是否开启"),
        sa.Column("config", sa.JSON(), nullable=True, comment="功能配置（JSON，按功能编码解释）"),
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
        sa.PrimaryKeyConstraint("id", name="pk_feature_settings"),
        sa.UniqueConstraint(
            "tenant_id",
            "feature_code",
            name="uq_feature_settings_tenant_id_feature_code",
        ),
        **table_options("租户功能开关表"),
    )
    op.create_index("ix_feature_settings_tenant_id", "feature_settings", ["tenant_id"], unique=False)

    # ------------------------------------------------------------------ 审计与支撑
    op.create_table(
        "audit_logs",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column("actor_id", sa.BigInteger(), nullable=True, comment="操作人用户ID（自动化任务为空）"),
        sa.Column(
            "action",
            sa.String(length=128),
            nullable=False,
            comment="操作编码（如 member.invite、role.update、inventory.adjust）",
        ),
        sa.Column(
            "resource_type",
            sa.String(length=64),
            nullable=False,
            comment="对象类型（member / role / store / product / order / task）",
        ),
        sa.Column("resource_id", sa.BigInteger(), nullable=True, comment="对象ID"),
        sa.Column("summary", sa.String(length=255), nullable=False, comment="操作摘要（不含敏感信息）"),
        sa.Column("payload", sa.JSON(), nullable=True, comment="补充信息（JSON，禁止写入明文密码与令牌）"),
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
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
        **table_options("租户审计日志表"),
    )
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"], unique=False)
    op.create_index("ix_audit_logs_tenant_created", "audit_logs", ["tenant_id", "created_at"], unique=False)
    op.create_index("ix_audit_logs_tenant_action", "audit_logs", ["tenant_id", "action"], unique=False)

    op.create_table(
        "files",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column(
            "object_key",
            sa.String(length=512),
            nullable=False,
            comment="对象存储键（含 tenant_id 前缀，禁止跨租户复用）",
        ),
        sa.Column("filename", sa.String(length=255), nullable=False, comment="原始文件名"),
        sa.Column("content_type", sa.String(length=128), nullable=False, comment="内容类型（如 image/png）"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, comment="文件字节数"),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=True, comment="上传人用户ID"),
        sa.Column("status", sa.String(length=32), nullable=False, comment="文件状态：active=可用，deleted=已删除"),
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
        sa.PrimaryKeyConstraint("id", name="pk_files"),
        sa.UniqueConstraint("tenant_id", "object_key", name="uq_files_tenant_id_object_key"),
        **table_options("文件记录表"),
    )
    op.create_index("ix_files_tenant_id", "files", ["tenant_id"], unique=False)
    op.create_index("ix_files_tenant_id_created_at", "files", ["tenant_id", "created_at"], unique=False)

    op.create_table(
        "idempotency_records",
        sa.Column("tenant_id", sa.BigInteger(), nullable=False, comment="租户ID"),
        sa.Column(
            "idempotency_key",
            sa.String(length=128),
            nullable=False,
            comment="幂等键（客户端请求头 X-Idempotency-Key）",
        ),
        sa.Column("endpoint", sa.String(length=255), nullable=False, comment="请求路径与方法"),
        sa.Column("request_hash", sa.String(length=64), nullable=False, comment="请求体摘要（同一幂等键的请求体必须一致）"),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            comment="处理状态：processing=处理中，completed=已完成",
        ),
        sa.Column("response_code", sa.Integer(), nullable=True, comment="首次响应的 HTTP 状态码"),
        sa.Column("response_body", sa.JSON(), nullable=True, comment="首次响应体（JSON）"),
        sa.Column("expires_at", sa.DateTime(), nullable=True, comment="记录过期时间（UTC）"),
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
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_idempotency_records_tenant_id_key",
        ),
        **table_options("请求防重记录表"),
    )
    op.create_index("ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"], unique=False)
    op.create_index("ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_idempotency_records_expires_at", table_name="idempotency_records")
    op.drop_index("ix_idempotency_records_tenant_id", table_name="idempotency_records")
    op.drop_table("idempotency_records")

    op.drop_index("ix_files_tenant_id_created_at", table_name="files")
    op.drop_index("ix_files_tenant_id", table_name="files")
    op.drop_table("files")

    op.drop_index("ix_audit_logs_tenant_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_feature_settings_tenant_id", table_name="feature_settings")
    op.drop_table("feature_settings")

    op.drop_index("ix_tenant_settings_tenant_id", table_name="tenant_settings")
    op.drop_table("tenant_settings")

    op.drop_index("ix_role_binding_scopes_member_role_id", table_name="role_binding_scopes")
    op.drop_index("ix_role_binding_scopes_tenant_id", table_name="role_binding_scopes")
    op.drop_table("role_binding_scopes")

    op.drop_index("ix_member_roles_role_id", table_name="member_roles")
    op.drop_index("ix_member_roles_member_id", table_name="member_roles")
    op.drop_index("ix_member_roles_tenant_id", table_name="member_roles")
    op.drop_table("member_roles")

    op.drop_index("ix_role_permissions_role_id", table_name="role_permissions")
    op.drop_index("ix_role_permissions_tenant_id", table_name="role_permissions")
    op.drop_table("role_permissions")

    op.drop_index("ix_roles_tenant_id_status", table_name="roles")
    op.drop_index("ix_roles_tenant_id", table_name="roles")
    op.drop_table("roles")
