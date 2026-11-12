"""固定时间窗口、分页断点和成功水位；调用方按店铺持有分布式锁。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.sales.models_tenant import OrderSyncCheckpoint


def load_checkpoint(db: Session, tenant_id: int, store_id: int, channel: str,
                    environment: str) -> OrderSyncCheckpoint | None:
    return db.execute(select(OrderSyncCheckpoint).where(
        OrderSyncCheckpoint.tenant_id == tenant_id,
        OrderSyncCheckpoint.store_id == store_id,
        OrderSyncCheckpoint.channel == channel,
        OrderSyncCheckpoint.environment == environment,
    ).with_for_update()).scalar_one_or_none()


def open_window(db: Session, *, tenant_id: int, store_id: int, channel: str,
                environment: str, now: datetime) -> OrderSyncCheckpoint:
    checkpoint = load_checkpoint(db, tenant_id, store_id, channel, environment)
    if checkpoint is None:
        checkpoint = OrderSyncCheckpoint(tenant_id=tenant_id, store_id=store_id,
                                         channel=channel, environment=environment, cursor="")
        db.add(checkpoint)
    if checkpoint.window_start is None or checkpoint.window_end is None:
        start = (checkpoint.watermark - timedelta(hours=2)
                 if checkpoint.watermark else now - timedelta(days=14))
        checkpoint.window_start = start
        # 限制单个窗口长度；长期停机后的积压通过后续窗口补齐，不丢弃旧水位。
        checkpoint.window_end = min(now, start + timedelta(days=14))
        checkpoint.cursor = ""
    db.flush()
    return checkpoint


def save_page(db: Session, checkpoint: OrderSyncCheckpoint, *, next_cursor: str,
              complete: bool) -> None:
    if complete:
        checkpoint.watermark = checkpoint.window_end
        checkpoint.window_start = None
        checkpoint.window_end = None
        checkpoint.cursor = ""
    else:
        checkpoint.cursor = next_cursor
    db.flush()
