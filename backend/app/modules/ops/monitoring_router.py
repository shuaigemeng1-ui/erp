"""监控总览路由（task-29 / task-30 共用契约）。

* ``GET /platform/monitoring/overview``：Lead 批次3定稿接口，聚合
  components / performance / public_probes / business_requests；
* ``GET /platform/monitoring/status``：兼容早期契约的监控分项状态。

路由由 ``ops/platform_router.py`` 以一行 ``include_router`` 接入，避免多人
同时改大文件；性能数据由 ``app.modules.ops.performance``（task-30）提供，
模块缺失或 Redis 不可用时返回结构化空数据，不阻塞监控页。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CenterDb, PlatformAdmin
from app.core.http import success
from app.modules.ops import monitoring

router = APIRouter(prefix="/monitoring", tags=["platform-monitoring"])


@router.get("/overview", summary="监控总览（分项状态/性能/公网探测）")
def monitoring_overview(center: CenterDb, _: PlatformAdmin) -> dict:
    return success(monitoring.monitoring_overview(center))


@router.get("/status", summary="监控分项状态（兼容契约）")
def monitoring_status(center: CenterDb, _: PlatformAdmin) -> dict:
    components = monitoring.components_status(center)
    return success(
        {
            "overall": monitoring.overall_status(components),
            "checked_at": monitoring._iso(monitoring._now()),
            "components": components,
        }
    )


__all__ = ["router"]
