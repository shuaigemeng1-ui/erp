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
    app_version: str = ""  # 当前构建版本（Git 短提交号）
    release_bundle_dir: str = "/srv/releases"  # 离线镜像包目录
    app_commit_sha: str = ""
    app_build_config_summary: str = ""
    app_build_status: str = "built"
    app_min_agent_protocol: int = 0
    app_center_db_version: str = ""
    app_tenant_db_version: str = ""
    app_rollback_compatible: bool = False
    api_prefix: str = "/api/v1"
    # 部署形态：center=统一管理中心，tenant=租户独立实例，gateway=独立集成网关
    deployment_mode: Literal["center", "tenant", "gateway"] = "center"
    # 固定实例绑定：tenant 模式下本实例只服务这一租户；center 模式为 0（不固定）
    # 请求路径/查询参数/请求体中的 tenant_id 必须与之一致，否则 403
    tenant_id: int = 0
    # 本实例在中心库登记的实例ID（tenant 模式用于任务绑定与自检；0 表示未配置）
    instance_id: int = 0
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
    # 迁移期旧凭据：租户侧 admin token；仅在 GATEWAY_LEGACY_ADMIN_ENABLED=true 时回退使用
    gateway_admin_token: str = ""
    gateway_legacy_admin_enabled: bool = True
    # 新链实例身份：每实例独立令牌（只存于该实例环境变量，不共享）
    gateway_instance_id: str = ""
    gateway_instance_token: str = ""
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

    # ------------------------------------------------------------------ 请求性能指标（HTTP 中间件）
    # 指标写缓存 Redis 分钟桶（24h TTL），不落业务库；Redis 异常时 fail-open 不影响请求。
    request_metrics_enabled: bool = True
    request_metrics_window_seconds: int = 15 * 60
    request_metrics_retention_hours: int = 24
    request_metrics_slow_ms: int = 1000
    # 排除不需要统计的业务前缀（逗号分隔）；健康检查与 Agent 上报始终排除
    request_metrics_exclude_prefixes: str = "/api/v1/agent/,/api/v1/platform/metrics,/api/v1/monitoring"
    # 本地内存降级窗口（Redis 不可用时页面仍可展示最近数据）
    request_metrics_memory_minutes: int = 15

    # ------------------------------------------------------------------ 运维告警阈值（默认持续阈值）
    # CPU / 内存按持续时间与采样连续性判定；磁盘按水位；Agent 失联按秒数。
    alert_cpu_percent: float = 85.0
    alert_cpu_sustain_seconds: int = 10 * 60
    alert_memory_percent: float = 90.0
    alert_memory_sustain_seconds: int = 5 * 60
    alert_disk_warning_percent: float = 85.0
    alert_disk_critical_percent: float = 95.0
    alert_agent_offline_seconds: int = 90
    # 公网探测连续失败次数（root 与 api 分别计数，不互相掩盖）
    alert_public_failure_count: int = 3
    # 重复告警抑制间隔（同一告警未恢复时，再次发邮件的间隔）
    alert_remind_seconds: int = 30 * 60
    # 恢复邮件
    alert_recovery_email_enabled: bool = True
    # 请求性能告警判定阈值（仅用于页面提示，不创建节点告警）
    alert_request_p95_ms: int = 1500
    alert_request_5xx_rate: float = 0.05
    alert_request_min_requests: int = 50

    # ------------------------------------------------------------------ 监控数据保留
    metric_retention_days: int = 7
    metric_aggregate_lookback_days: int = 7
    metric_aggregate_5m_retention_days: int = 30
    metric_aggregate_1h_retention_days: int = 400

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
