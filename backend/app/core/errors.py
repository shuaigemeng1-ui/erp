"""统一错误码与异常。

接口错误响应固定为::

    {
      "error": {"code": "AUTH_INVALID_CREDENTIALS", "message": "账号或密码错误", "details": null},
      "request_id": "0f7c2f2c6f5c4c1e"
    }

未登录 401、无操作权限 403、对象不可见 404、业务冲突 409，均通过本模块抛出。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping


class ErrorCode(StrEnum):
    """稳定错误码，前端与文档直接引用，禁止随意改名。"""

    # 认证 / 会话
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_SESSION_REQUIRED = "AUTH_SESSION_REQUIRED"
    AUTH_SESSION_EXPIRED = "AUTH_SESSION_EXPIRED"
    AUTH_CSRF_INVALID = "AUTH_CSRF_INVALID"
    AUTH_ORIGIN_INVALID = "AUTH_ORIGIN_INVALID"
    AUTH_ACCOUNT_DISABLED = "AUTH_ACCOUNT_DISABLED"
    AUTH_PASSWORD_TOO_WEAK = "AUTH_PASSWORD_TOO_WEAK"
    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
    AUTH_TOKEN_USED = "AUTH_TOKEN_USED"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_RATE_LIMITED = "AUTH_RATE_LIMITED"

    # 租户
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    TENANT_FORBIDDEN = "TENANT_FORBIDDEN"
    TENANT_SUSPENDED = "TENANT_SUSPENDED"
    TENANT_NOT_ACTIVE = "TENANT_NOT_ACTIVE"
    TENANT_DATABASE_UNAVAILABLE = "TENANT_DATABASE_UNAVAILABLE"
    TENANT_PROVISION_FAILED = "TENANT_PROVISION_FAILED"
    TENANT_CONTEXT_MISSING = "TENANT_CONTEXT_MISSING"

    # 权限
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PLATFORM_PERMISSION_REQUIRED = "PLATFORM_PERMISSION_REQUIRED"
    SCOPE_DENIED = "SCOPE_DENIED"
    ROLE_ASSIGN_FORBIDDEN = "ROLE_ASSIGN_FORBIDDEN"
    MEMBER_DISABLED = "MEMBER_DISABLED"

    # 通用
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    FEATURE_DISABLED = "FEATURE_DISABLED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # 渠道
    STORE_ALREADY_BOUND = "STORE_ALREADY_BOUND"
    STORE_NOT_CONNECTED = "STORE_NOT_CONNECTED"
    STORE_REAUTH_REQUIRED = "STORE_REAUTH_REQUIRED"
    CHANNEL_APP_NOT_CONFIGURED = "CHANNEL_APP_NOT_CONFIGURED"
    CHANNEL_SIGNATURE_INVALID = "CHANNEL_SIGNATURE_INVALID"
    CHANNEL_CALLBACK_UNKNOWN = "CHANNEL_CALLBACK_UNKNOWN"

    # 商品 / 库存 / 订单
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    SKU_NOT_FOUND = "SKU_NOT_FOUND"
    SKU_MAPPING_NOT_UNIQUE = "SKU_MAPPING_NOT_UNIQUE"
    SKU_MAPPING_MISSING = "SKU_MAPPING_MISSING"
    INVENTORY_NEGATIVE_FORBIDDEN = "INVENTORY_NEGATIVE_FORBIDDEN"
    ORDER_ALREADY_EXISTS = "ORDER_ALREADY_EXISTS"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"

    # 任务
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    JOB_IN_PROGRESS = "JOB_IN_PROGRESS"


# 错误码默认 HTTP 状态与默认中文说明
DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_SESSION_REQUIRED: 401,
    ErrorCode.AUTH_SESSION_EXPIRED: 401,
    ErrorCode.AUTH_CSRF_INVALID: 403,
    ErrorCode.AUTH_ORIGIN_INVALID: 403,
    ErrorCode.AUTH_ACCOUNT_DISABLED: 403,
    ErrorCode.AUTH_PASSWORD_TOO_WEAK: 422,
    ErrorCode.AUTH_TOKEN_INVALID: 422,
    ErrorCode.AUTH_TOKEN_USED: 422,
    ErrorCode.AUTH_TOKEN_EXPIRED: 422,
    ErrorCode.AUTH_RATE_LIMITED: 429,
    ErrorCode.TENANT_NOT_FOUND: 404,
    ErrorCode.TENANT_FORBIDDEN: 403,
    ErrorCode.TENANT_SUSPENDED: 403,
    ErrorCode.TENANT_NOT_ACTIVE: 409,
    ErrorCode.TENANT_DATABASE_UNAVAILABLE: 503,
    ErrorCode.TENANT_PROVISION_FAILED: 409,
    ErrorCode.TENANT_CONTEXT_MISSING: 500,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.PLATFORM_PERMISSION_REQUIRED: 403,
    ErrorCode.SCOPE_DENIED: 403,
    ErrorCode.ROLE_ASSIGN_FORBIDDEN: 403,
    ErrorCode.MEMBER_DISABLED: 403,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.FEATURE_DISABLED: 403,
    ErrorCode.QUOTA_EXCEEDED: 409,
    ErrorCode.UNSUPPORTED_ACTION: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.STORE_ALREADY_BOUND: 409,
    ErrorCode.STORE_NOT_CONNECTED: 409,
    ErrorCode.STORE_REAUTH_REQUIRED: 409,
    ErrorCode.CHANNEL_APP_NOT_CONFIGURED: 409,
    ErrorCode.CHANNEL_SIGNATURE_INVALID: 401,
    ErrorCode.CHANNEL_CALLBACK_UNKNOWN: 404,
    ErrorCode.PRODUCT_NOT_FOUND: 404,
    ErrorCode.SKU_NOT_FOUND: 404,
    ErrorCode.SKU_MAPPING_NOT_UNIQUE: 409,
    ErrorCode.SKU_MAPPING_MISSING: 409,
    ErrorCode.INVENTORY_NEGATIVE_FORBIDDEN: 409,
    ErrorCode.ORDER_ALREADY_EXISTS: 409,
    ErrorCode.ORDER_NOT_FOUND: 404,
    ErrorCode.TASK_NOT_FOUND: 404,
    ErrorCode.JOB_IN_PROGRESS: 409,
}


class AppError(Exception):
    """业务异常基类。Service 层只抛本异常，Router 层不做业务判断。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        details: Any = None,
        status_code: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.message = message or DEFAULT_MESSAGES.get(code, code.value)
        self.details = details
        self.status_code = status_code or DEFAULT_STATUS.get(code, 400)
        self.headers = dict(headers or {})
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


class NotFoundError(AppError):
    def __init__(self, message: str = "对象不存在或不可见", *, details: Any = None) -> None:
        super().__init__(ErrorCode.NOT_FOUND, message, details=details)


class ConflictError(AppError):
    def __init__(self, message: str = "业务冲突", *, details: Any = None) -> None:
        super().__init__(ErrorCode.CONFLICT, message, details=details)


class PermissionDeniedError(AppError):
    def __init__(self, message: str = "无操作权限", *, details: Any = None) -> None:
        super().__init__(ErrorCode.PERMISSION_DENIED, message, details=details)


class ScopeDeniedError(AppError):
    def __init__(self, message: str = "无权操作该数据范围", *, details: Any = None) -> None:
        super().__init__(ErrorCode.SCOPE_DENIED, message, details=details)


DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: "账号或密码错误",
    ErrorCode.AUTH_SESSION_REQUIRED: "请先登录",
    ErrorCode.AUTH_SESSION_EXPIRED: "登录已过期，请重新登录",
    ErrorCode.AUTH_CSRF_INVALID: "请求校验失败，请刷新页面后重试",
    ErrorCode.AUTH_ORIGIN_INVALID: "请求来源不被允许",
    ErrorCode.AUTH_ACCOUNT_DISABLED: "账号已停用",
    ErrorCode.AUTH_PASSWORD_TOO_WEAK: "密码强度不足",
    ErrorCode.AUTH_TOKEN_INVALID: "激活链接无效",
    ErrorCode.AUTH_TOKEN_USED: "激活链接已使用",
    ErrorCode.AUTH_TOKEN_EXPIRED: "激活链接已过期",
    ErrorCode.AUTH_RATE_LIMITED: "操作过于频繁，请稍后再试",
    ErrorCode.TENANT_NOT_FOUND: "租户不存在",
    ErrorCode.TENANT_FORBIDDEN: "无权访问该租户",
    ErrorCode.TENANT_SUSPENDED: "租户已暂停",
    ErrorCode.TENANT_NOT_ACTIVE: "租户尚未就绪",
    ErrorCode.TENANT_DATABASE_UNAVAILABLE: "租户数据库暂不可用",
    ErrorCode.TENANT_PROVISION_FAILED: "租户开通失败，请重试",
    ErrorCode.TENANT_CONTEXT_MISSING: "缺少租户上下文",
    ErrorCode.PERMISSION_DENIED: "无操作权限",
    ErrorCode.PLATFORM_PERMISSION_REQUIRED: "需要平台管理员权限",
    ErrorCode.SCOPE_DENIED: "无权操作该数据范围",
    ErrorCode.ROLE_ASSIGN_FORBIDDEN: "无权分配该角色或数据范围",
    ErrorCode.MEMBER_DISABLED: "成员已停用",
    ErrorCode.VALIDATION_ERROR: "参数校验失败",
    ErrorCode.NOT_FOUND: "对象不存在或不可见",
    ErrorCode.CONFLICT: "业务冲突",
    ErrorCode.IDEMPOTENCY_CONFLICT: "重复请求处理中，请稍后查询结果",
    ErrorCode.RATE_LIMITED: "请求过于频繁",
    ErrorCode.FEATURE_DISABLED: "功能未开启",
    ErrorCode.QUOTA_EXCEEDED: "已超出套餐额度",
    ErrorCode.UNSUPPORTED_ACTION: "当前状态不支持该操作",
    ErrorCode.INTERNAL_ERROR: "服务器内部错误",
    ErrorCode.SERVICE_UNAVAILABLE: "服务暂不可用",
    ErrorCode.STORE_ALREADY_BOUND: "该平台店铺已归属其他租户",
    ErrorCode.STORE_NOT_CONNECTED: "店铺尚未完成授权",
    ErrorCode.STORE_REAUTH_REQUIRED: "店铺凭据失效，请重新授权",
    ErrorCode.CHANNEL_APP_NOT_CONFIGURED: "平台应用未配置",
    ErrorCode.CHANNEL_SIGNATURE_INVALID: "回调签名校验失败",
    ErrorCode.CHANNEL_CALLBACK_UNKNOWN: "回调对象不存在",
    ErrorCode.PRODUCT_NOT_FOUND: "商品不存在",
    ErrorCode.SKU_NOT_FOUND: "SKU 不存在",
    ErrorCode.SKU_MAPPING_NOT_UNIQUE: "SKU 自动匹配结果不唯一，请人工处理",
    ErrorCode.SKU_MAPPING_MISSING: "SKU 尚未映射",
    ErrorCode.INVENTORY_NEGATIVE_FORBIDDEN: "人工调整不允许产生负库存",
    ErrorCode.ORDER_ALREADY_EXISTS: "订单已存在",
    ErrorCode.ORDER_NOT_FOUND: "订单不存在",
    ErrorCode.TASK_NOT_FOUND: "任务不存在",
    ErrorCode.JOB_IN_PROGRESS: "任务执行中",
}
