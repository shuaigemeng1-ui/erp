"""平台邮件服务配置：阿里云 DirectMail、腾讯云 SES。

凭据只以密文入库；查询接口只返回是否已配置，不返回明文。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import CenterBase, IdMixin, TimestampMixin, table_options


class EmailProfile(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "email_profiles"
    __table_args__ = (
        UniqueConstraint("name", name="uq_email_profiles_name"),
        table_options("平台邮件服务账号配置表"),
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="邮件账号名称")
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="邮件厂商：aliyun_directmail/tencent_ses"
    )
    region: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=text("''"), comment="服务区域"
    )
    from_address: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="发信地址"
    )
    from_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="", server_default=text("''"), comment="发信人名称"
    )
    access_key_id_cipher: Mapped[str] = mapped_column(
        Text, nullable=False, comment="AccessKey ID 密文"
    )
    secret_access_key_cipher: Mapped[str] = mapped_column(
        Text, nullable=False, comment="AccessKey Secret / SecretKey 密文"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1"), comment="是否启用"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0"), comment="是否默认发信账号"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1"), comment="配置版本号"
    )
