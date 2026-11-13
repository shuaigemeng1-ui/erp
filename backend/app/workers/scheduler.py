"""独立调度进程入口：``python -m app.workers.scheduler``。

不放在 API 副本内运行；调度逻辑由 ``app.modules.tasks.scheduler`` 实现
（扫描各租户 ``sync_schedules``，投递 Celery 任务并重建租户上下文）。

调度进程同样受固定实例 / 租户绑定约束：环境变量非法时拒绝启动，
只为本实例绑定且未独立部署 / 未迁移的租户生成任务。
"""

from __future__ import annotations

import logging

from app.core.logging import configure_logging


def main() -> None:
    configure_logging()
    logger = logging.getLogger("ventasync.scheduler")
    try:
        from app.modules.tasks.scheduler import main as scheduler_main
        from app.modules.tasks import instance
    except (ImportError, AttributeError) as exc:
        logger.error("调度逻辑未实现（app.modules.tasks.scheduler.main 缺失）: %s", exc)
        raise SystemExit(3) from exc
    try:
        binding = instance.load_worker_binding()
    except instance.BindingConfigError as exc:
        logger.error("调度进程实例绑定配置非法，拒绝启动：%s", exc)
        raise SystemExit(3) from exc
    logger.info("调度进程启动，固定绑定=%s", binding.describe())
    try:
        from app.workers.monitor import start_heartbeat_thread

        start_heartbeat_thread("scheduler")
    except Exception:  # noqa: BLE001 - 心跳失败不阻塞调度进程
        logger.exception("启动调度器心跳失败（监控分项将显示 unknown）")
    scheduler_main()


if __name__ == "__main__":
    main()
