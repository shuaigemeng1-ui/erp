"""租户库模型：数据范围、租户设置（权限版本）、功能开关。

* ``role_binding_scopes``：一条绑定的数据范围。``scope_type=all`` 时 ``scope_id=0``（唯一约束需要非空值），
  ``stores`` / ``stock_places`` 时每个对象一行。
* ``tenant_settings``：权限版本 ``permission_version`` 存这里；权限 / 绑定 / 成员变更后递增，
  缓存键和后续请求都按当前版本重新检查。
* ``feature_settings``：功能开关，租户暂停、套餐限制不能通过角色绕过。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import IdMixin, TenantBase, TenantIdMixin, TimestampMixin, table_options


class RoleBindingScope(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """角色绑定的数据范围：店铺或库存地点；空范围代表没有授权。"""

    __tablename__ = "role_binding_scopes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "member_role_id",
            "scope_type",
            "scope_id",
            name="uq_role_binding_scopes_binding_scope",
        ),
        Index("ix_role_binding_scopes_member_role_id", "member_role_id"),
        table_options("角色绑定数据范围表"),
    )

    member_role_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="成员角色绑定ID（逻辑关联 member_roles.id，不建外键）",
    )
    scope_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="范围类型：all=租户全部，stores=指定店铺，stock_places=指定库存地点",
    )
    scope_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="范围对象ID（店铺ID或库存地点ID；scope_type=all 时为 0）",
    )


class TenantSettings(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """租户设置：每个租户一行，保存权限版本与同步开关。"""

    __tablename__ = "tenant_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_settings_tenant_id"),
        table_options("租户设置表"),
    )

    permission_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        comment="权限版本号（成员/角色/数据范围变更后递增，用于缓存失效）",
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="Asia/Shanghai",
        comment="租户时区（数据库存 UTC，按时区展示）",
    )
    company_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
        comment="公司名称（展示用）",
    )
    order_sync_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="是否允许主动拉取订单",
    )
    stock_push_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否允许向平台推送可售库存（真实推送默认关闭）",
    )


class FeatureSetting(IdMixin, TenantIdMixin, TimestampMixin, TenantBase):
    """租户功能开关：按功能编码启停，套餐额度不删除已有数据。"""

    __tablename__ = "feature_settings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "feature_code",
            name="uq_feature_settings_tenant_id_feature_code",
        ),
        table_options("租户功能开关表"),
    )

    feature_code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="功能编码（如 order_sync、stock_push、channel_listing）",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="是否开启",
    )
    config: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="功能配置（JSON，按功能编码解释）",
    )
