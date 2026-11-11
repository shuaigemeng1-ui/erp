"""租户库模型：角色、角色权限、成员角色绑定。

* 角色与权限集合存租户库；``member_roles.member_id`` 引用中心库的成员 ID（逻辑关联，不建外键）。
* 写入绑定前必须在中心库校验该成员确属当前租户（由 Service 完成）。
* 预置角色（owner/admin/...）由开通流程写入，``is_system=True`` 的角色不可删除。
* 数据范围表见 ``app/modules/access/models_tenant.py``（``role_binding_scopes``）。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import IdMixin, TenantBase, TenantIdMixin, TimestampMixin, table_options


class Role(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """租户角色：一个角色绑定多个权限编码。"""

    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_roles_tenant_id_code"),
        Index("ix_roles_tenant_id_status", "tenant_id", "status"),
        table_options("租户角色表"),
    )

    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="角色编码（租户内唯一；预置角色编码保持不变以便升级对照）",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="角色名称",
    )
    description: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        comment="角色说明",
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否系统预置角色（预置角色不可删除，可复制后修改）",
    )
    copied_from_role_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="复制来源角色ID（逻辑关联 roles.id，不建外键）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        comment="角色状态：active=启用，disabled=停用",
    )


class RolePermission(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """角色与权限编码的关联：一条记录代表角色拥有一个功能权限。"""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "role_id",
            "permission_code",
            name="uq_role_permissions_tenant_role_permission",
        ),
        Index("ix_role_permissions_role_id", "role_id"),
        table_options("角色权限关联表"),
    )

    role_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="角色ID（逻辑关联 roles.id，不建外键）",
    )
    permission_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="权限编码（见 app/core/permissions.py）",
    )


class MemberRole(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """成员角色绑定：一条记录代表「某成员拥有某角色」，数据范围挂在绑定的范围表上。

    ``id`` 即 ``AccessSnapshot`` 中的 ``role_binding_id``；同一绑定上的权限与数据范围不可拆分。
    """

    __tablename__ = "member_roles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "member_id",
            "role_id",
            name="uq_member_roles_tenant_member_role",
        ),
        Index("ix_member_roles_member_id", "member_id"),
        Index("ix_member_roles_role_id", "role_id"),
        table_options("成员角色绑定表"),
    )

    role_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="角色ID（逻辑关联 roles.id，不建外键）",
    )
    member_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="成员ID（中心库 tenant_memberships.id，不建外键）",
    )
    assigned_by: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="分配人用户ID（自动化 / 邀请激活时为空）",
    )
