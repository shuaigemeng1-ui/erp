"""Celery 应用与队列配置。

* API、Worker、调度器分进程运行（见 deploy/docker-compose.yml）。
* Worker 任务必须通过 ``tenant_context_for_job`` 重建租户上下文，不携带明文凭据。
* Worker 固定实例 / 租户绑定来自进程环境（``ERP_WORKER_INSTANCE_ID`` /
  ``ERP_WORKER_TENANT_ID``），由 ``app.modules.tasks.instance`` 统一解析；
  启动时校验并打印，配置非法直接拒绝启动，避免静默串租户。
* 队列 Redis 与缓存 Redis 分离；每类任务使用独立队列，便于限流与扩容。
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.signals import worker_process_init, worker_ready

from app.core.config import settings

logger = logging.getLogger("ventasync.worker")

celery_app = Celery(
    "ventasync",
    broker=settings.queue_redis_url,
    backend=settings.celery_result_backend,
    include=[
        "app.modules.identity.tasks",
        "app.modules.tasks.outbox",
        "app.modules.tasks.scheduler",
        "app.modules.tasks.tasks",
        "app.modules.channels.tasks",
        "app.modules.sales.tasks",
    ],
)

celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_default_queue="default",
    task_routes={
        "app.modules.tasks.outbox.*": {"queue": "outbox"},
        "app.modules.tasks.scheduler.*": {"queue": "scheduler"},
        "app.modules.channels.tasks.*": {"queue": "channel"},
        "app.modules.sales.tasks.*": {"queue": "sales"},
        "app.modules.tasks.tasks.*": {"queue": "default"},
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=200,
    task_track_started=True,
    task_time_limit=15 * 60,
    task_soft_time_limit=12 * 60,
    broker_connection_retry_on_startup=True,
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=settings.celery_task_always_eager,
)


def worker_instance_binding() -> dict[str, object]:
    """读取并校验本 Worker 的固定实例 / 租户绑定（配置非法时抛 RuntimeError）。"""
    from app.modules.tasks import instance

    try:
        binding = instance.load_worker_binding()
    except instance.BindingConfigError as exc:  # pragma: no cover - 部署配置错误
        raise RuntimeError(f"后台任务实例绑定配置错误：{exc}") from exc
    return binding.describe()


@worker_process_init.connect
def _log_worker_binding(**_kwargs: object) -> None:
    """Worker 子进程启动时打印绑定，让部署日志可直接核对实例归属。"""
    try:
        binding = worker_instance_binding()
    except RuntimeError:
        logger.exception("Worker 实例绑定配置非法，拒绝启动")
        raise
    logger.info(
        "Worker 启动：deployment_mode=%s 固定绑定=%s",
        settings.deployment_mode,
        binding,
    )


@worker_ready.connect
def _start_worker_heartbeat(**_kwargs: object) -> None:
    """Worker 就绪后写组件心跳（供 /platform/monitoring/overview 分项状态）。"""
    try:
        from app.workers.monitor import start_heartbeat_thread

        start_heartbeat_thread("worker")
    except Exception:  # noqa: BLE001 - 心跳失败不能阻止 Worker 处理任务
        logger.exception("启动 Worker 心跳失败（监控分项将显示 unknown）")
