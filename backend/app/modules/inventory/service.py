"""库存服务契约（供 sales 模块调用）。

跨模块只通过本文件暴露的函数访问库存数据，避免 sales 直接改库存表。

实现负责人：backend-catalog。
"""

from __future__ import annotations


def ensure_stock_place(db, tenant_id: int, stock_place_id: int) -> None:  # type: ignore[no-untyped-def]
    """校验库存地点存在且属于当前租户；不存在抛 ``AppError``。"""
    raise NotImplementedError("inventory.service.ensure_stock_place 尚未实现")
