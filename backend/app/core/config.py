"""应用配置。

所有配置通过环境变量或 ``.env`` 注入，禁止在代码中硬编码生产凭据。
本模块只做配置解析，不建立任何数据库/Redis 连接，导入本模块不会产生副作用。
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置。字段顺序与 ``.env.example`` 保持一致。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ 基础
    app_name: str = "ERP"
    app_env: str = "development"  # development | test | production
    api_prefix: str = "/api/v1"
    # 部署形态：center=统一管理中心，tenant=租户独立实例，gateway=独立集成网关
    deployment_mode: Literal["center", "tenant", "gateway"] = "center"
    debug: bool = False
    log_level: str = "INFO"
    # 前端站点源，用于 Cookie 会话的请求来源校验
    app_origin: str = "http://127.0.0.1:5173"

    # ------------------------------------------------------------------ 安全
    secret_key: str = Field(
        default="dev-only-secret-key-change-me",
        description="HMAC/摘要盐值，生产环境必须显式配置",
    )
    # 平台密钥加密（店铺 Token、Shopee 应用 Secret）。留空时由 secret_key 派生，仅用于开发。
    encryption_key: str = ""
    session_cookie_name: str = "vs_session"
    session_cookie_secure: bool = True
    session_cookie_same_site: str = "lax"
    csrf_cookie_name: str = "vs_csrf"
    session_ttl_seconds: int = 12 * 60 * 60
    invitation_ttl_seconds: int = 7 * 24 * 60 * 60
    token_encryption_prefix: str = "enc:v1:"

    # ------------------------------------------------------------------ 中心库
    center_database_url: str = (
        "mysql+pymysql://ventasync:ventasync@127.0.0.1:3306/ventasync_center?charset=utf8mb4"
    )
    # 建库/建号/迁移使用独立运维身份，应用运行账号不得拥有该权限
    provisioner_database_url: str = ""

    # ------------------------------------------------------------------ 租户库
    tenant_db_host: str = "127.0.0.1"
    tenant_db_port: int = 3306
    tenant_db_prefix: str = "vs_tenant_"
    tenant_db_user_prefix: str = "vs_t_"
    # 租户库连接池（每个租户一个 Engine）
    tenant_pool_size: int = 5
    tenant_max_overflow: int = 5
    tenant_pool_recycle_seconds: int = 1800
    tenant_engine_cache_size: int = 64

    # ------------------------------------------------------------------ Redis
    # 队列与缓存 Redis 分开配置，清缓存不能清除待执行任务
    cache_redis_url: str = "redis://127.0.0.1:6379/0"
    queue_redis_url: str = "redis://127.0.0.1:6379/1"
    celery_result_backend: str = "redis://127.0.0.1:6379/2"
    celery_task_always_eager: bool = False

    # ------------------------------------------------------------------ HTTP/分页
    default_page_size: int = 20
    max_page_size: int = 200
    cors_allow_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    # 允许访问的 Host（逗号分隔；"*" 表示不限制，生产环境应配置为正式域名）。
    # 用于只允许域名访问，拒绝 IP / 未知 Host 直接访问应用。
    trusted_hosts: str = "*"

    # ------------------------------------------------------------------ 对象存储
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_force_path_style: bool = True
    file_public_base_url: str = ""
    catalog_media_dir: str = "var/catalog-media"

    # ------------------------------------------------------------------ Shopee（平台级应用凭据存中心库，这里只放环境地址）
    shopee_api_base: str = "https://partner.shopeemobile.com"
    shopee_auth_base: str = "https://partner.shopeemobile.com"
    shopee_auth_redirect_url: str = ""
    shopee_http_timeout_seconds: float = 20.0
    shopee_http_max_retries: int = 3

    # ------------------------------------------------------------------ TikTok Shop
    tiktok_auth_base: str = "https://auth.tiktok-shops.com"
    # 独立集成网关（租户实例可选的渠道配置来源）
    gateway_base_url: str = ""
    gateway_admin_token: str = ""
    gateway_channel_config: bool = False
    gateway_timeout_seconds: float = 5.0

    tiktok_api_base: str = "https://open-api.tiktokglobalshop.com"
    tiktok_auth_redirect_url: str = ""
    tiktok_http_timeout_seconds: float = 20.0
    tiktok_http_max_retries: int = 3

    # ------------------------------------------------------------------ 平台初始化
    # 仅由显式初始化命令使用，不开放公共管理员注册
    bootstrap_platform_admin_email: str = ""
    bootstrap_platform_admin_password: str = ""
    bootstrap_platform_admin_name: str = "平台管理员"

    # ------------------------------------------------------------------ 定时任务
    outbox_dispatch_interval_seconds: int = 15
    outbox_batch_size: int = 100
    sync_run_stale_minutes: int = 30

    @field_validator("app_env")
    @classmethod
    def _check_env(cls, value: str) -> str:
        allowed = {"development", "test", "production"}
        if value not in allowed:
            raise ValueError(f"app_env 必须是 {sorted(allowed)} 之一")
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_allow_origins.split(",") if item.strip()]

    @property
    def allowed_hosts(self) -> list[str] | None:
        """``None`` 表示不限制 Host；否则返回允许的 Host 列表。"""
        value = self.trusted_hosts.strip()
        if not value or value == "*":
            return None
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def secret_key_bytes(self) -> bytes:
        return self.secret_key.encode("utf-8")

    @property
    def derived_encryption_key(self) -> bytes:
        """返回 32 字节对称密钥。生产环境必须配置 ``ENCRYPTION_KEY``。"""
        if self.encryption_key:
            raw = self.encryption_key.encode("utf-8")
            try:
                decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
                if len(decoded) == 32:
                    return decoded
            except Exception:  # noqa: BLE001 - 任意非 base64 输入都退化为派生
                pass
            return hashlib.sha256(raw).digest()
        return hashlib.sha256(b"ventasync-encryption|" + self.secret_key_bytes).digest()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
