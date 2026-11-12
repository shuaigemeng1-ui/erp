"""平台对象存储配置；所有关联均为逻辑关联，不创建外键。"""
from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import CenterBase, IdMixin, TimestampMixin, table_options


class StorageProfile(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "storage_profiles"
    __table_args__ = (
        UniqueConstraint("name", name="uq_storage_profiles_name"),
        table_options("平台对象存储账号配置表"),
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="存储账号名称")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, comment="云厂商编码：OSS、COS、OBS、S3或兼容服务")
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False, comment="对象存储服务HTTPS地址")
    bucket: Mapped[str] = mapped_column(String(255), nullable=False, comment="存储桶名称")
    region: Mapped[str] = mapped_column(String(100), nullable=False, comment="存储区域编码")
    access_key_id_cipher: Mapped[str] = mapped_column(Text, nullable=False, comment="访问密钥ID密文")
    secret_access_key_cipher: Mapped[str] = mapped_column(Text, nullable=False, comment="访问密钥Secret密文")
    public_base_url: Mapped[str] = mapped_column(String(1000), nullable=False, comment="文件长期访问域名或CDN基础地址")
    key_prefix: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="", comment="对象存储键前缀")
    addressing_style: Mapped[str] = mapped_column(String(16), nullable=False, default="auto", server_default="auto", comment="存储桶寻址方式：自动、虚拟主机或路径")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("1"), comment="是否启用此存储账号")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"), comment="存储配置编辑版本号")


class SystemSettings(IdMixin, TimestampMixin, CenterBase):
    __tablename__ = "platform_system_settings"
    __table_args__ = (table_options("平台系统配置表（固定主键一）"),)
    default_storage_profile_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="当前默认对象存储账号ID（逻辑关联，无外键）")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"), comment="系统配置版本号")
