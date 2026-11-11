"""Celery 应用与队列配置。

* API、Worker、调度器分进程运行（见 deploy/docker-compose.yml）。
* Worker 任务必须通过 ``tenant_context_for_job`` 重建租户上下文，不携带明文凭据。
* 队列 Redis 与缓存 Redis 分离；每类任务使用独立队列，便于限流与扩容。
"""

from __future__ import annotations

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "ventasync",
    broker=settings.queue_redis_url,
    backend=settings.celery_result_backend,
    include=[
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
