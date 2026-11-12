"""FastAPI 应用入口（``app.main:app``）。

* 不在启动时执行数据库迁移（迁移由独立发布任务执行）。
* 中间件：请求 ID、会话 / CSRF / 来源校验、结构化日志。
* 统一异常响应：业务码 + 说明 + ``request_id``。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from app.api.router import api_router
from app.core.auth import (
    UNSAFE_METHODS,
    is_csrf_exempt,
    load_session_payload,
    verify_csrf,
    verify_request_origin,
)
from app.core.config import settings
from app.core.context import (
    new_request_id,
    reset_current_user_id,
    reset_request_id,
    set_current_user_id,
    set_request_id,
)
from app.core.errors import AppError, ErrorCode
from app.core.http import error_body
from app.core.logging import configure_logging
from app.core.redis import touch_session
from app.db.base import assert_no_foreign_keys
from app.db.registry import center_metadata, tenant_metadata
from app.db.session import dispose_all, ping_center

logger = logging.getLogger("ventasync.http")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """请求标识 + 会话装载 + CSRF/来源校验。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or new_request_id()
        request_id_token = set_request_id(request_id)
        user_token = None
        request.state.request_id = request_id
        request.state.session_payload = None
        request.state.session_id = ""
        started = time.perf_counter()
        try:
            try:
                payload = load_session_payload(request)
                request.state.session_payload = payload
                session_id = request.cookies.get(settings.session_cookie_name, "")
                request.state.session_id = session_id
                if payload:
                    user_token = set_current_user_id(int(payload["user_id"]))
                    touch_session(session_id, payload)
                verify_request_origin(request)
                mode = settings.deployment_mode
                path = request.url.path
                if mode == "tenant" and path.startswith(f"{settings.api_prefix}/platform"):
                    return JSONResponse(
                        status_code=404,
                        content=error_body("NOT_FOUND", "当前实例未开放平台管理接口"),
                    )
                if mode == "gateway" and not (
                    path in {"/healthz", "/livez"}
                    or path.startswith(f"{settings.api_prefix}/gateway")
                ):
                    return JSONResponse(
                        status_code=404,
                        content=error_body("NOT_FOUND", "当前网关未开放该接口"),
                    )
                if request.method in UNSAFE_METHODS and not is_csrf_exempt(request.url.path):
                    verify_csrf(request, payload)
            except AppError as exc:
                response = JSONResponse(
                    status_code=exc.status_code,
                    content=error_body(exc.code.value, exc.message, exc.details),
                    headers=exc.headers or None,
                )
                response.headers["x-request-id"] = request_id
                return response

            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            if settings.debug:
                response.headers["x-process-time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
            return response
        finally:
            if user_token is not None:
                reset_current_user_id(user_token)
            reset_request_id(request_id_token)


def runtime_health() -> dict:
    return {"ok": True, "mode": settings.deployment_mode, "app": settings.app_name}


def create_app() -> FastAPI:
    configure_logging()
    # 代码级自检：模型不得声明外键（迁移与数据库另有验收脚本）
    assert_no_foreign_keys(center_metadata, tenant_metadata)

    application = FastAPI(
        title=f"{settings.app_name} API",
        version="1.0.0",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    application.add_middleware(RequestContextMiddleware)
    # 只允许通过正式域名访问；未配置 TRUSTED_HOSTS 时不限制（本地开发/测试）
    allowed_hosts = settings.allowed_hosts
    if allowed_hosts:
        application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["x-request-id"],
        )

    @application.get(f"{settings.api_prefix}/runtime", tags=["runtime"])
    def runtime_info() -> dict:
        return {"ok": True, "mode": settings.deployment_mode, "app": settings.app_name}

    @application.get(f"{settings.api_prefix}/gateway/health", tags=["gateway"])
    def gateway_health() -> dict:
        return {"ok": True, "mode": settings.deployment_mode}

    application.include_router(api_router, prefix=settings.api_prefix)

    _install_exception_handlers(application)
    _install_lifecycle(application)
    return application


def _install_exception_handlers(application: FastAPI) -> None:
    @application.exception_handler(AppError)
    async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code.value, exc.message, exc.details),
            headers=exc.headers or None,
        )

    @application.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {
                "loc": ".".join(str(item) for item in error.get("loc", ())),
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(ErrorCode.VALIDATION_ERROR.value, "参数校验失败", details),
        )

    @application.exception_handler(StarletteHTTPException)
    async def _http_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.VALIDATION_ERROR
        if exc.status_code >= 500:
            code = ErrorCode.INTERNAL_ERROR
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(status_code=exc.status_code, content=error_body(code.value, detail, None))

    @application.exception_handler(Exception)
    async def _unknown_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content=error_body(ErrorCode.INTERNAL_ERROR.value, "服务器内部错误", None),
        )


def _install_lifecycle(application: FastAPI) -> None:
    @application.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover - 进程退出钩子
        dispose_all()

    @application.get("/healthz", tags=["health"], include_in_schema=False)
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": settings.app_name}

    @application.get("/readyz", tags=["health"], include_in_schema=False)
    def readyz() -> dict[str, Any]:
        """就绪检查：中心库与缓存可用。"""
        ping_center()
        from app.core.redis import cache_redis

        cache_redis().ping()
        return {"status": "ready"}


app = create_app()
