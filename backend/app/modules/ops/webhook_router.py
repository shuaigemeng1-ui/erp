"""平台管理员只读：TikTok Webhook 可观测性指标。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import CenterDb, PlatformAdmin
from app.core.http import success
from app.modules.ops import webhook_metrics

router = APIRouter(prefix="/monitoring", tags=["platform-monitoring"])


@router.get("/webhooks", summary="TikTok Webhook 接收/待刷新/已写入与网关指标")
def webhook_metrics_view(
    center: CenterDb,
    _: PlatformAdmin,
    window_seconds: int = Query(
        default=3600,
        ge=60,
        le=86400,
        description="租户事件/订单统计窗口（秒）",
    ),
    tenant_id: str = Query(default="", max_length=32, description="按租户下钻"),
    store_id: str = Query(default="", max_length=32, description="按店铺下钻"),
) -> dict:
    return success(
        webhook_metrics.webhook_metrics(
            center,
            window_seconds=window_seconds,
            tenant_id=int(tenant_id) if tenant_id.isdigit() else None,
            store_id=int(store_id) if store_id.isdigit() else None,
        )
    )


__all__ = ["router"]
