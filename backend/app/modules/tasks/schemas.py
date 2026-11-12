"""任务模块的 Pydantic 响应 / 请求模型。

约定（见 docs/api-contract.md「通用约定」）：

* 大整数 ID 一律序列化为字符串，避免前端 JS 精度丢失；
* 金额使用 ``Decimal``，时间统一 UTC；
* 列表响应由 ``app.core.http.paged`` 包裹，单个对象由 ``success`` 包裹。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# 大整数 ID 的 JSON 序列化：始终输出字符串
IdStr = Annotated[int, PlainSerializer(lambda value: str(value), return_type=str, when_used="json")]


class SyncRunOut(BaseModel):
    """任务记录（列表项）。"""

    model_config = ConfigDict(from_attributes=True)

    id: IdStr = Field(description="任务ID（202 接口返回的 task_id）")
    job_type: str = Field(description="任务类型")
    store_id: IdStr | None = Field(default=None, description="店铺ID")
    status: str = Field(description="任务状态")
    triggered_by: str = Field(description="触发来源")
    requested_by: IdStr | None = Field(default=None, description="发起人用户ID")
    started_at: datetime | None = Field(default=None, description="开始时间（UTC）")
    finished_at: datetime | None = Field(default=None, description="结束时间（UTC）")
    total_processed: int = Field(default=0, description="已处理数量")
    error_message: str = Field(default="", description="失败原因")
    created_at: datetime | None = Field(default=None, description="创建时间（UTC）")


class SyncRunDetailOut(SyncRunOut):
    """任务详情（含分页游标，便于排查断点续传）。"""

    cursor: str = Field(default="", description="分页游标")


class SyncScheduleOut(BaseModel):
    """定时同步配置。"""

    model_config = ConfigDict(from_attributes=True)

    id: IdStr = Field(description="配置ID")
    job_type: str = Field(description="任务类型")
    store_id: IdStr | None = Field(default=None, description="店铺ID（为空表示全部店铺）")
    interval_minutes: int = Field(description="执行间隔（分钟）")
    enabled: bool = Field(description="是否启用")
    last_run_at: datetime | None = Field(default=None, description="上次执行时间（UTC）")
    next_run_at: datetime | None = Field(default=None, description="下次执行时间（UTC）")


class SyncScheduleUpdate(BaseModel):
    """更新定时配置（只允许改启停与间隔）。"""

    enabled: bool | None = Field(default=None, description="是否启用")
    interval_minutes: int | None = Field(
        default=None, ge=1, le=1440, description="执行间隔（分钟）"
    )


class SyncSettingsUpdate(BaseModel):
    """租户同步总开关，只接受明确的布尔配置。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    order_sync_enabled: bool | None = Field(default=None, description="是否允许主动同步订单")
    stock_push_enabled: bool | None = Field(default=None, description="是否允许向平台推送库存")


class TaskStatusOut(BaseModel):
    """异步任务状态（tasks/{task_id}）。"""

    task_id: str = Field(description="任务ID")
    type: str = Field(description="任务类型（sync_run 为任务类型，celery 为 celery）")
    status: str = Field(description="任务状态")
    result: Any | None = Field(default=None, description="任务结果")
    error: str | None = Field(default=None, description="失败原因")


class AsyncTaskOut(BaseModel):
    """202 异步操作返回体。"""

    task_id: str = Field(description="任务ID（同步任务记录ID或 Celery 任务ID）")
