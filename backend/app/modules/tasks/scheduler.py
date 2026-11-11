"""调度进程：扫描各租户 ``sync_schedules``，按 cron/间隔投递同步任务。

独立进程运行（不放在 API 副本内）：``python -m app.workers.scheduler``。
本文件由 backend-sales 实现调度扫描与租户上下文重建。

实现负责人：backend-sales（tasks 模块）。
"""

from __future__ import annotations
