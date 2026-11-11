"""调度进程：扫描各租户 ``sync_schedules``，按间隔投递同步任务。

独立进程运行（不放在 API 副本内）：``python -m app.workers.scheduler``
（Lead 提供的启动脚本会直接调用本模块的 ``main()``）。

职责：

1. 遍历中心库中 active 的租户，逐个打开租户库（连接路由只来自中心库受控映射）；
2. 补齐默认定时配置（订单同步默认 30 分钟）；
3. 扫描到点且启用的配置，创建 ``sync_runs`` 并在同一事务写 Outbox（事务提交后才投递）；
4. 回收长时间卡在 running 的任务（Worker 中断 / 被杀后可人工重试）；
5. 单租户、单轮异常只记录日志，不影响下一轮。

调度进程只做「到点生成任务」，真正的业务执行在 Worker 中完成，并通过
``app.core.context`` 重建 TenantContext。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import utcnow
from app.db.session import center_session, tenant_session
from app.modules.tasks import repository
from app.modules.tasks import service as tasks_service
from app.modules.tasks.tenancy import active_tenant_ids, load_tenant_route

logger = logging.getLogger("ventasync.tasks.scheduler")# 调度进程轮询间隔（秒）。最小 5 秒，避免空转。
DEFAULT_TICK_SECONDS = 30
# 单租户一次最多生成的任务数
DEFAULT_BATCH_SIZE = 100


def compute_next_run_at(now: datetime, interval_minutes: int) -> datetime:
    """下一次执行时间（纯函数，供调度与测试共用）。"""
    return tasks_service.next_run_at(now, interval_minutes)


def run_tenant_schedules(
    db: Session, tenant_id: int, *, now: datetime, limit: int = DEFAULT_BATCH_SIZE
) -> dict[str, int]:
    """处理单个租户的一轮到点调度（调用方负责提交事务）。"""
    summary = {"scanned": 0, "created": 0, "reused": 0}
    # 补齐默认配置：新开通租户即使没有配置也能按默认频率拉单
    tasks_service.ensure_default_schedules(db, tenant_id=tenant_id)
    due = repository.due_schedules(db, tenant_id, now, limit=limit)
    summary["scanned"] = len(due)
    for schedule in due:
        store_id = int(schedule.store_id) if schedule.store_id else None
        pending = repository.find_pending_sync_run(db, tenant_id, str(schedule.job_type), store_id)
        tasks_service.request_sync_run(
            db,
            tenant_id=tenant_id,
            job_type=str(schedule.job_type),
            store_id=store_id,
            triggered_by="schedule",
            requested_by=None,
        )
        if pending is None:
            summary["created"] += 1
        else:
            summary["reused"] += 1
        schedule.last_run_at = now
        schedule.next_run_at = compute_next_run_at(now, int(schedule.interval_minutes or 30))
    return summary


def recover_stale_runs_for_tenant(tenant_id: int, *, stale_minutes: int | None = None) -> int:
    """回收中断任务（延迟 import，避免调度进程导入 Celery 任务模块的重量级依赖）。"""
    from app.modules.tasks.tasks import recover_stale_runs

    return recover_stale_runs(tenant_id, stale_minutes=stale_minutes)


def compensate_webhook_events_for_tenant(tenant_id: int) -> tuple[int, int]:
    """回调事件补偿扫描（渠道模块提供，延迟 import；未就绪时跳过，不影响调度）。

    渠道模块负责把「已验签但未处理成功」的回调事件重新投递订单同步；本函数只做调用与
    计数，不直接改渠道数据。事件防重依赖 ``webhook_events`` 唯一约束，订单防重与库存
    幂等由 sales 侧保证，因此重复补偿不会重复建单或重复扣库存。

    返回 ``(已处理数量, 失败数量)``，供调度日志对账。
    """
    try:
        from app.modules.channels.tasks import compensate_tenant_webhook_events
    except (ImportError, AttributeError):
        return 0, 0
    try:
        result = compensate_tenant_webhook_events(int(tenant_id))
    except Exception:  # noqa: BLE001 - 补偿失败不影响调度轮次
        logger.exception("回调事件补偿失败 tenant=%s", tenant_id)
        return 0, 0
    if isinstance(result, dict):
        processed = int(result.get("processed") or 0) + int(result.get("retried") or 0)
        failed = int(result.get("failed") or 0)
        return processed, failed
    return int(result or 0), 0


def run_once(
    *,
    now: datetime | None = None,
    limit: int = DEFAULT_BATCH_SIZE,
    tenant_ids: list[int] | None = None,
    recover_stale: bool = True,
    compensate_webhooks: bool = True,
) -> dict[str, int]:
    """执行一轮调度扫描，返回汇总计数（异常只记录，不抛出）。"""
    timestamp = now or utcnow()
    summary = {
        "tenants": 0,
        "scanned": 0,
        "created": 0,
        "reused": 0,
        "recovered": 0,
        "webhook_compensated": 0,
        "webhook_failed": 0,
        "failed": 0,
    }
    candidates = tenant_ids if tenant_ids is not None else active_tenant_ids()
    for tenant_id in candidates:
        try:
            with center_session() as center:
                route = load_tenant_route(center, int(tenant_id))
            with tenant_session(route) as db:
                tenant_summary = run_tenant_schedules(
                    db, int(tenant_id), now=timestamp, limit=limit
                )
                db.commit()
        except Exception:  # noqa: BLE001 - 单租户失败不影响其它租户
            summary["failed"] += 1
            logger.exception("调度扫描租户 %s 失败", tenant_id)
            continue
        summary["tenants"] += 1
        for key in ("scanned", "created", "reused"):
            summary[key] += tenant_summary[key]
        if recover_stale:
            try:
                summary["recovered"] += recover_stale_runs_for_tenant(int(tenant_id))
            except Exception:  # noqa: BLE001
                logger.exception("回收中断任务失败 tenant=%s", tenant_id)
        if compensate_webhooks:
            compensated, compensation_failed = compensate_webhook_events_for_tenant(int(tenant_id))
            summary["webhook_compensated"] += compensated
            summary["webhook_failed"] += compensation_failed
    if (
        summary["created"]
        or summary["recovered"]
        or summary["webhook_compensated"]
        or summary["webhook_failed"]
        or summary["failed"]
    ):
        logger.info(
            "调度轮次: 租户=%s 扫描=%s 新建任务=%s 复用=%s 回收中断=%s 回调补偿=%s 回调补偿失败=%s 失败租户=%s",
            summary["tenants"],
            summary["scanned"],
            summary["created"],
            summary["reused"],
            summary["recovered"],
            summary["webhook_compensated"],
            summary["webhook_failed"],
            summary["failed"],
        )
    return summary


def tick_seconds() -> int:
    """调度轮询间隔（可由配置派生，便于部署时调整）。"""
    return max(5, int(getattr(settings, "outbox_dispatch_interval_seconds", DEFAULT_TICK_SECONDS)))


def main(dispatch_outbox: bool = True) -> int:
    """调度进程入口（阻塞运行，直到收到 Ctrl+C / SIGTERM）。

    除「到点生成任务」外，同一进程还负责一轮 Outbox 投递：部署上只有 api / worker /
    scheduler 三类常驻进程，把待投递事件交给调度进程轮询可以少一个常驻进程；
    需要独立扩缩容时也可以单独运行 ``app.modules.tasks.outbox.main()``
    （两边同时运行是安全的：领取用 ``FOR UPDATE SKIP LOCKED``，投递语义为至少一次）。
    """
    from app.core.logging import configure_logging
    from app.modules.tasks import outbox

    configure_logging()
    logger.info(
        "调度进程启动，轮询间隔 %s 秒，内置 Outbox 投递=%s", tick_seconds(), dispatch_outbox
    )
    try:
        while True:
            try:
                run_once()
            except KeyboardInterrupt:  # pragma: no cover - 进程信号
                raise
            except Exception:  # noqa: BLE001
                logger.exception("调度轮次失败")
            if dispatch_outbox:
                try:
                    outbox.dispatch_pending()
                except KeyboardInterrupt:  # pragma: no cover
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("Outbox 投递轮次失败")
            time.sleep(tick_seconds())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("调度进程收到停止信号")
        return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
