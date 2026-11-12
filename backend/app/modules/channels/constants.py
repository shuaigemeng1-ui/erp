"""渠道模块公共常量与纯函数（无数据库 / 无网络依赖）。

* 渠道与环境编码是稳定契约：授权会话、任务消息、审计与前端筛选都引用这里。
* 店铺 / 凭据 / 映射状态取值与 docs/data-model.md 一致，状态用 VARCHAR 存，
  不在数据库层使用 ENUM。
"""

from __future__ import annotations

CHANNEL_SHOPEE = "shopee"
CHANNEL_TIKTOK = "tiktok"
SUPPORTED_CHANNELS: frozenset[str] = frozenset({CHANNEL_SHOPEE, CHANNEL_TIKTOK})

ENV_PRODUCTION = "production"
ENV_TEST = "test"
ENVIRONMENTS: frozenset[str] = frozenset({ENV_PRODUCTION, ENV_TEST})

# 店铺状态
STORE_STATUS_CONNECTED = "connected"
STORE_STATUS_REAUTH_REQUIRED = "reauth_required"
STORE_STATUS_DISCONNECTED = "disconnected"
STORE_STATUSES: frozenset[str] = frozenset(
    {STORE_STATUS_CONNECTED, STORE_STATUS_REAUTH_REQUIRED, STORE_STATUS_DISCONNECTED}
)

# 凭据状态
CREDENTIAL_STATUS_ACTIVE = "active"
CREDENTIAL_STATUS_EXPIRED = "expired"
CREDENTIAL_STATUS_INVALID = "invalid"
CREDENTIAL_STATUS_REVOKED = "revoked"
CREDENTIAL_STATUSES: frozenset[str] = frozenset(
    {
        CREDENTIAL_STATUS_ACTIVE,
        CREDENTIAL_STATUS_EXPIRED,
        CREDENTIAL_STATUS_INVALID,
        CREDENTIAL_STATUS_REVOKED,
    }
)

# 平台商品状态
LISTING_STATUS_ACTIVE = "active"
LISTING_STATUS_INACTIVE = "inactive"

# 映射状态
LINK_STATUS_MAPPED = "mapped"
LINK_STATUS_PENDING = "pending"
LINK_STATUS_AMBIGUOUS = "ambiguous"
LINK_STATUS_MISSING = "missing"
LINK_STATUSES: frozenset[str] = frozenset(
    {LINK_STATUS_MAPPED, LINK_STATUS_PENDING, LINK_STATUS_AMBIGUOUS, LINK_STATUS_MISSING}
)
# 列表筛选用：尚无映射记录
LISTING_FILTER_UNMAPPED = "unmapped"

# 映射来源
MAPPING_SOURCE_AUTO = "auto"
MAPPING_SOURCE_MANUAL = "manual"

# 平台商品无规格行使用空串，保证唯一约束不因 NULL 失效
NO_EXTERNAL_SKU = ""

# 访问令牌提前刷新窗口（秒）
TOKEN_REFRESH_SKEW_SECONDS = 300
# Shopify / Shopee refresh token 默认有效期（天）
REFRESH_TOKEN_TTL_DAYS = 30


def resolve_environment(app_env: str) -> str:
    """根据部署环境决定默认接入的 Shopee 应用环境。

    生产部署默认 production；其余（development/test）默认使用平台测试应用，
    避免开发环境误连真实店铺。
    """
    return ENV_PRODUCTION if (app_env or "").strip().lower() == "production" else ENV_TEST


def normalize_environment(value: str | None) -> str | None:
    """规范化环境编码；非法值返回 ``None``。"""
    candidate = (value or "").strip().lower()
    return candidate if candidate in ENVIRONMENTS else None


def normalize_external_id(value: object) -> str:
    """规范化平台外部 ID（去空白、转字符串），``None`` 视为空串。"""
    if value is None:
        return ""
    return str(value).strip()


def normalize_sku_code(value: object) -> str:
    """规范化 SKU 编码：去首尾空白，保留大小写（平台 SKU 编码区分大小写）。"""
    return normalize_external_id(value)
