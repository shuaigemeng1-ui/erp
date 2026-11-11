"""任务重试策略（纯函数，便于单元测试）。

规则（见 plan.txt §4「库存与任务可靠性」）：

* 暂时性错误（网络超时、限流、连接中断、数据库暂时不可用）做有限次退避重试；
* 凭据失效、参数错误、权限不足、业务冲突等直接进入可处理失败状态，不做无意义重试；
* Outbox 投递与 Celery 任务各自有独立的重试上限，达到上限后落 ``failed`` 并保留
  ``last_error``，人工在任务页重试即可恢复（业务逻辑本身幂等）。
"""

from __future__ import annotations

from app.core.errors import AppError, ErrorCode

# Outbox 事件投递：最多尝试次数与退避参数
MAX_OUTBOX_ATTEMPTS = 8
# Celery 任务自身：最多尝试次数
MAX_TASK_ATTEMPTS = 3
# 指数退避：30s、60s、120s…… 最长 30 分钟
BASE_RETRY_DELAY_SECONDS = 30
MAX_RETRY_DELAY_SECONDS = 1800

# 可重试的业务错误码：服务暂不可用 / 限流 / 租户库暂不可用 / 内部错误
RETRYABLE_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.SERVICE_UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.TENANT_DATABASE_UNAVAILABLE,
        ErrorCode.INTERNAL_ERROR,
    }
)


def retry_delay_seconds(
    attempts: int,
    *,
    base: int = BASE_RETRY_DELAY_SECONDS,
    cap: int = MAX_RETRY_DELAY_SECONDS,
) -> int:
    """按已尝试次数计算退避秒数：base * 2^(attempts-1)，封顶 cap。"""
    attempt = max(1, int(attempts))
    delay = base * (2 ** (attempt - 1))
    return int(min(delay, cap))


def should_retry(attempts: int, max_attempts: int = MAX_OUTBOX_ATTEMPTS) -> bool:
    """已尝试次数是否仍可重试。``attempts`` 指已失败的次数（含本次）。"""
    return int(attempts) < int(max_attempts)


def is_retryable_error(exc: BaseException) -> bool:
    """判断异常是否属于「暂时性错误」。

    * 业务错误（``AppError``）：只有服务不可用 / 限流类可重试；
    * 网络层：HTTPX 超时与传输错误可重试，HTTP 429 / 5xx 可重试；
    * 数据库：SQLAlchemy ``OperationalError``（连接断开、锁等待超时）可重试；
    * 其它未知异常：交由上层有限重试（次数上限保证不会无限重试）。
    """
    if isinstance(exc, AppError):
        return exc.code in RETRYABLE_ERROR_CODES

    try:  # 延迟 import：避免任务模块在无 HTTP 依赖环境下导入失败
        import httpx

        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code if exc.response is not None else 0
            return status == 429 or status >= 500
    except ImportError:  # pragma: no cover - 依赖缺失时跳过
        pass

    try:
        from sqlalchemy.exc import OperationalError

        if isinstance(exc, OperationalError):
            return True
    except ImportError:  # pragma: no cover
        pass

    # 参数错误 / 未实现依赖：重试没有意义，直接失败并写清原因
    if isinstance(exc, (ValueError, TypeError, KeyError, NotImplementedError)):
        return False
    return True
