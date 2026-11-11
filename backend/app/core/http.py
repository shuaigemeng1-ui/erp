"""HTTP 响应封装与分页。

成功响应：``{"data": ..., "request_id": "..."}``
分页响应：``{"data": {"items": [...], "total": n, "page": p, "page_size": s}, "request_id": "..."}``
异步任务：返回 ``202`` + ``{"data": {"task_id": "..."}, "request_id": "..."}``
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from fastapi import Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.context import get_request_id

T = TypeVar("T")


class PageParams:
    """查询分页参数依赖。"""

    def __init__(
        self,
        page: int = Query(default=1, ge=1, description="页码，从 1 开始"),
        page_size: int = Query(default=20, ge=1, le=200, description="每页条数"),
    ) -> None:
        from app.core.config import settings

        self.page = page
        self.page_size = min(page_size, settings.max_page_size)


class PageMeta(BaseModel):
    total: int = Field(description="总条数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页条数")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


def success(data: Any = None) -> dict[str, Any]:
    return {"data": data, "request_id": get_request_id()}


def paged(items: list[Any], total: int, page: int, page_size: int) -> dict[str, Any]:
    return success({"items": items, "total": total, "page": page, "page_size": page_size})


def accepted(data: Any) -> JSONResponse:
    return JSONResponse(status_code=202, content=success(data))


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details},
        "request_id": get_request_id(),
    }


def paginate_meta(total: int, page: int, page_size: int) -> dict[str, int]:
    return {"total": total, "page": page, "page_size": page_size}
