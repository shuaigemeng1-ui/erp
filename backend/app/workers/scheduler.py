"""独立调度进程入口：``python -m app.workers.scheduler``。

不放在 API 副本内运行；调度逻辑由 ``app.modules.tasks.scheduler`` 实现
（扫描各租户 ``sync_schedules``，投递 Celery 任务并重建租户上下文）。
"""

from __future__ import annotations

import logging

from app.core.logging import configure_logging


def main() -> None:
    configure_logging()
    logger = logging.getLogger("ventasync.scheduler")
    try:
        from app.modules.tasks.scheduler import main as scheduler_main
    except (ImportError, AttributeError) as exc:
        logger.error("调度逻辑未实现（app.modules.tasks.scheduler.main 缺失）: %s", exc)
        raise SystemExit(3) from exc
    logger.info("调度进程启动")
    scheduler_main()


if __name__ == "__main__":
    main()
