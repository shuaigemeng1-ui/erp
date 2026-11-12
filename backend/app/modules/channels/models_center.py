"""中心库模型：平台渠道应用、外部店铺归属登记。

约定（见 plan.txt §2「无外键规范」与 docs/data-model.md）：

* 平台统一配置 Shopee 应用凭据，租户只做店铺授权，不接触 Partner Key。
* 凭据只保存 AES-GCM 密文（``enc:v1:`` 前缀，见 ``app.core.security``），禁止明文。
* 同一环境的同一外部店铺只能归属一个有效租户：由唯一约束
  ``(channel, environment, external_account_id)`` 保证；店铺释放后复用同一行重新登记。
* 禁止外键；表名与字段均带中文注释。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import CenterBase, IdMixin, TimestampMixin, table_options


class PlatformChannelApp(IdMixin, TimestampMixin, CenterBase):
    """平台渠道应用配置（生产 / 测试各一套凭据）。"""

    __tablename__ = "platform_channel_apps"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "environment",
            name="uq_platform_channel_apps_channel_environment",
        ),
        table_options("平台渠道应用配置表"),
    )

    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="渠道编码，如 shopee"
    )
    environment: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="应用环境：production/test"
    )
    partner_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="平台合作方ID（Shopee Partner ID）"
    )
    app_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="应用 ID（TikTok Shop Partner Center 的 App ID）"
    )
    app_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="应用 Key（TikTok Shop App Key；Shopee 用 partner_id）"
    )
    app_secret_cipher: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="应用 Secret 密文（TikTok Shop App Secret）"
    )
    partner_key_cipher: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="合作方密钥密文（用于请求签名，禁止明文）"
    )
    partner_secret_cipher: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="推送校验密钥密文，为空时回退使用合作方密钥"
    )
    redirect_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="OAuth 回调地址（平台统一配置）"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="active",
        server_default=text("'active'"),
        comment="状态：active/disabled",
    )


class ChannelAccountRegistry(IdMixin, TimestampMixin, CenterBase):
    """外部店铺归属登记：同一环境同一店铺只允许一个有效租户。"""

    __tablename__ = "channel_account_registry"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "environment",
            "external_account_id",
            name="uq_channel_account_registry_env_external",
        ),
        Index(
            "ix_channel_account_registry_channel_external_account_id",
            "channel",
            "external_account_id",
        ),
        Index("ix_channel_account_registry_tenant_id", "tenant_id"),
        table_options("渠道外部账号归属登记表"),
    )

    channel: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="渠道编码，如 shopee"
    )
    environment: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="环境：production/test"
    )
    external_account_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="平台外部账号ID（Shopee 店铺ID）"
    )
    tenant_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="归属租户ID"
    )
    store_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="归属租户库内的店铺ID"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="bound",
        server_default=text("'bound'"),
        comment="状态：bound/released",
    )
    bound_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="绑定时间（UTC）"
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="释放时间（UTC）"
    )
