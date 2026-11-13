"""监控指标聚合修复：历史补报补算、小时桶加权平均、过期清理。

背景（批次3 task-30）：

* 原实现只聚合「最近 2 小时」的原始样本，Agent 断网恢复后补报的历史样本
  （``sampled_at`` 早于窗口）永远不会进入聚合，7/30 天趋势出现空洞；
* 小时桶由 5 分钟桶平均再平均（未按样本数加权），样本数不同的桶权重被抹平，
  长时间断采后会失真；
* 原始指标按 ``received_at`` 清理，历史补报的旧时间样本长期滞留，且聚合表没有
  过期策略，会无限增长。

修复策略：

1. 5 分钟桶：按分钟桶 SQL 分组（``floor(unix_timestamp/300)``）得到实际样本数，
   与已聚合行比较；**只重算缺失或样本数变化的桶**（天然覆盖补报 / 迟到样本），
   重算时补全磁盘最大值；
2. 小时桶：读取保留期内全部 5 分钟桶，按 ``sample_count`` 加权平均、取各最大值，
   保证长窗口趋势与原始样本一致；
3. 清理：原始样本 / 探测按 ``sampled_at`` / ``checked_at`` 过期（同时兜底
   ``received_at``），聚合桶按 5m 30 天 / 1h 400 天保留，均由调度进程每日执行。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import delete, func, or_, select

from app.core.config import settings
from app.modules.ops.models_center import (
    NodeCheck,
    NodeMetricAggregate,
    NodeMetricSample,
    ServerNode,
)

logger = logging.getLogger("ventasync.ops.metrics_aggregation")

BUCKET_SECONDS_5M = 300
AGGREGATE_REFRESH_SECONDS = 240
_last_aggregate_at: datetime | None = None
_last_cleanup_at: datetime | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bucket_epoch_expr(column):
    """MySQL：按 300 秒分桶的 Unix 秒表达式（返回 float，Python 侧转 int）。"""
    return func.floor(func.unix_timestamp(column) / BUCKET_SECONDS_5M)


# --------------------------------------------------------------------------- 纯函数
def bucket_start_5m(moment: datetime) -> datetime:
    """把时间归一到 5 分钟桶起点（UTC naive）。"""
    value = moment
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(
        minute=(value.minute // 5) * 5, second=0, microsecond=0
    )


def hourly_bucket_start(moment: datetime) -> datetime:
    value = moment
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(minute=0, second=0, microsecond=0)


def _max_disk_percent(disks: Any) -> float:
    if not isinstance(disks, Sequence):
        return 0.0
    result = 0.0
    for disk in disks:
        if not isinstance(disk, Mapping):
            continue
        try:
            result = max(result, float(disk.get("percent") or 0))
        except (TypeError, ValueError):
            continue
    return result


def bucket_needs_recompute(
    existing_sample_count: int | None, actual_sample_count: int | None, *, force: bool = False
) -> bool:
    """5 分钟桶是否需要重算：缺失、样本数增加（历史补报/迟到）或强制刷新。"""
    if actual_sample_count is None or int(actual_sample_count or 0) <= 0:
        return False
    if force or existing_sample_count is None:
        return True
    try:
        existing = int(existing_sample_count or 0)
        actual = int(actual_sample_count or 0)
    except (TypeError, ValueError):
        return True
    return actual > existing


def aggregate_sample_rows(rows: Iterable[Any]) -> dict[str, Any]:
    """由原始样本计算一个 5 分钟桶的聚合值（纯函数，补报/迟到样本都适用）。"""
    items = list(rows)
    if not items:
        return {
            "sample_count": 0,
            "cpu_avg": 0.0,
            "cpu_max": 0.0,
            "memory_avg": 0.0,
            "memory_max": 0.0,
            "load_avg": 0.0,
            "disk_max": 0.0,
        }
    count = len(items)
    cpu_values = [float(getattr(item, "cpu_percent", 0) or 0) for item in items]
    memory_values = [float(getattr(item, "memory_percent", 0) or 0) for item in items]
    load_values = [float(getattr(item, "load1", 0) or 0) for item in items]
    disk_values = [_max_disk_percent(getattr(item, "disks", None)) for item in items]
    return {
        "sample_count": count,
        "cpu_avg": round(sum(cpu_values) / count, 2),
        "cpu_max": round(max(cpu_values), 2),
        "memory_avg": round(sum(memory_values) / count, 2),
        "memory_max": round(max(memory_values), 2),
        "load_avg": round(sum(load_values) / count, 2),
        "disk_max": round(max(disk_values), 2),
    }


def weighted_average(values: Iterable[float], weights: Iterable[float]) -> float:
    """按权重求平均；权重全为 0 / 非正时退化为简单平均。"""
    pairs = list(zip(values, weights))
    if not pairs:
        return 0.0
    total_weight = sum(max(0.0, float(weight)) for _, weight in pairs)
    if total_weight <= 0:
        return sum(float(value) for value, _ in pairs) / len(pairs)
    return sum(float(value) * max(0.0, float(weight)) for value, weight in pairs) / total_weight


def combine_5m_rows(rows: Iterable[Any]) -> dict[str, Any]:
    """由若干 5 分钟聚合行计算小时桶：平均按 sample_count 加权，最大值取最大。"""
    items = list(rows)
    if not items:
        return {
            "sample_count": 0,
            "cpu_avg": 0.0,
            "cpu_max": 0.0,
            "memory_avg": 0.0,
            "memory_max": 0.0,
            "load_avg": 0.0,
            "disk_max": 0.0,
        }
    counts = [max(0, int(getattr(item, "sample_count", 0) or 0)) for item in items]
    sample_count = sum(counts)
    cpu_avg = weighted_average([float(getattr(item, "cpu_avg", 0) or 0) for item in items], counts)
    memory_avg = weighted_average(
        [float(getattr(item, "memory_avg", 0) or 0) for item in items], counts
    )
    load_avg = weighted_average([float(getattr(item, "load_avg", 0) or 0) for item in items], counts)
    return {
        "sample_count": sample_count,
        "cpu_avg": round(cpu_avg, 2),
        "cpu_max": round(max(float(getattr(item, "cpu_max", 0) or 0) for item in items), 2),
        "memory_avg": round(memory_avg, 2),
        "memory_max": round(max(float(getattr(item, "memory_max", 0) or 0) for item in items), 2),
        "load_avg": round(load_avg, 2),
        "disk_max": round(max(float(getattr(item, "disk_max", 0) or 0) for item in items), 2),
    }


# --------------------------------------------------------------------------- 聚合
def _existing_5m_rows(center, node_id: int, since: datetime) -> dict[datetime, NodeMetricAggregate]:
    rows = center.execute(
        select(NodeMetricAggregate).where(
            NodeMetricAggregate.node_id == node_id,
            NodeMetricAggregate.resolution == "5m",
            NodeMetricAggregate.bucket_start >= since,
        )
    ).scalars().all()
    return {row.bucket_start: row for row in rows}


def _samples_for_bucket(center, node_id: int, bucket_start: datetime) -> list[NodeMetricSample]:
    return list(
        center.execute(
            select(NodeMetricSample).where(
                NodeMetricSample.node_id == node_id,
                NodeMetricSample.sampled_at >= bucket_start,
                NodeMetricSample.sampled_at < bucket_start + timedelta(seconds=BUCKET_SECONDS_5M),
            )
        ).scalars().all()
    )


def _upsert_aggregate(
    center,
    node_id: int,
    resolution: str,
    bucket_start: datetime,
    values: Mapping[str, Any],
    now: datetime,
    existing: NodeMetricAggregate | None = None,
) -> bool:
    row = existing
    if row is None:
        row = center.execute(
            select(NodeMetricAggregate).where(
                NodeMetricAggregate.node_id == node_id,
                NodeMetricAggregate.resolution == resolution,
                NodeMetricAggregate.bucket_start == bucket_start,
            )
        ).scalar_one_or_none()
    if row is None:
        center.add(
            NodeMetricAggregate(
                node_id=node_id,
                resolution=resolution,
                bucket_start=bucket_start,
                created_at=now,
                **dict(values),
            )
        )
        return True
    changed = False
    for key, value in values.items():
        current_value = getattr(row, key, None)
        try:
            different = float(current_value) != float(value)  # Numeric 返回 Decimal
        except (TypeError, ValueError):
            different = current_value != value
        if different:
            setattr(row, key, value)
            changed = True
    if changed:
        row.created_at = row.created_at or now
    return changed


def aggregate_metrics(center, *, force: bool = False) -> int:
    """重算缺失/样本数变化的 5m 桶，再按加权平均重算 1h 桶。返回写入桶数。"""
    global _last_aggregate_at
    now = _now()
    if not force and _last_aggregate_at is not None and (
        (now - _last_aggregate_at).total_seconds() < AGGREGATE_REFRESH_SECONDS
    ):
        return 0
    _last_aggregate_at = now
    lookback_days = max(1, int(settings.metric_aggregate_lookback_days))
    since = now - timedelta(days=lookback_days)
    node_ids = list(center.execute(select(ServerNode.id)).scalars().all())
    written = 0
    for node_id in node_ids:
        existing_5m = _existing_5m_rows(center, node_id, since)
        rows = center.execute(
            select(
                _bucket_epoch_expr(NodeMetricSample.sampled_at).label("bucket"),
                func.count().label("sample_count"),
            )
            .where(
                NodeMetricSample.node_id == node_id,
                NodeMetricSample.sampled_at >= since,
            )
            .group_by(_bucket_epoch_expr(NodeMetricSample.sampled_at))
        ).all()
        for bucket_epoch, sample_count in rows:
            try:
                bucket_start = datetime.fromtimestamp(
                    int(float(bucket_epoch)), tz=timezone.utc
                ).replace(tzinfo=None)
            except (TypeError, ValueError, OSError):
                continue
            current = existing_5m.get(bucket_start)
            # 补报 / 迟到样本：样本数比已聚合多即重算（force 时全量重算）
            if not bucket_needs_recompute(
                int(current.sample_count or 0) if current is not None else None,
                int(sample_count or 0),
                force=force,
            ):
                continue
            samples = _samples_for_bucket(center, node_id, bucket_start)
            if not samples:
                continue
            values = aggregate_sample_rows(samples)
            if _upsert_aggregate(
                center, node_id, "5m", bucket_start, values, now, existing=current
            ):
                written += 1
        center.flush()

    # 小时桶：由保留期内全部 5 分钟桶按样本数加权合并（修复未加权平均）。
    for node_id in node_ids:
        rows5 = center.execute(
            select(NodeMetricAggregate).where(
                NodeMetricAggregate.node_id == node_id,
                NodeMetricAggregate.resolution == "5m",
                NodeMetricAggregate.bucket_start >= since,
            )
        ).scalars().all()
        grouped: dict[datetime, list[NodeMetricAggregate]] = {}
        for row in rows5:
            grouped.setdefault(hourly_bucket_start(row.bucket_start), []).append(row)
        for bucket_start, items in grouped.items():
            values = combine_5m_rows(items)
            if _upsert_aggregate(center, node_id, "1h", bucket_start, values, now):
                written += 1
    center.commit()
    return written


# --------------------------------------------------------------------------- 清理
def cleanup_monitoring_data(center) -> dict[str, int]:
    """按保留期清理原始样本、探测与聚合桶（每日最多一次）。"""
    global _last_cleanup_at
    now = _now()
    if _last_cleanup_at is not None and (now - _last_cleanup_at).total_seconds() < 86400:
        return {"metrics": 0, "checks": 0, "aggregates": 0}
    raw_cutoff = now - timedelta(days=max(1, int(settings.metric_retention_days)))
    metrics = center.execute(
        delete(NodeMetricSample).where(
            or_(
                NodeMetricSample.sampled_at < raw_cutoff,
                NodeMetricSample.received_at < raw_cutoff,
            )
        )
    ).rowcount
    checks = center.execute(
        delete(NodeCheck).where(NodeCheck.checked_at < raw_cutoff)
    ).rowcount
    agg_cutoff_5m = now - timedelta(days=max(1, int(settings.metric_aggregate_5m_retention_days)))
    agg_cutoff_1h = now - timedelta(days=max(1, int(settings.metric_aggregate_1h_retention_days)))
    aggregates = center.execute(
        delete(NodeMetricAggregate).where(
            or_(
                (NodeMetricAggregate.resolution == "5m")
                & (NodeMetricAggregate.bucket_start < agg_cutoff_5m),
                (NodeMetricAggregate.resolution == "1h")
                & (NodeMetricAggregate.bucket_start < agg_cutoff_1h),
            )
        )
    ).rowcount
    center.commit()
    _last_cleanup_at = now
    cleaned = {
        "metrics": int(metrics or 0),
        "checks": int(checks or 0),
        "aggregates": int(aggregates or 0),
    }
    if any(cleaned.values()):
        logger.info(
            "监控数据清理：样本=%s 探测=%s 聚合=%s",
            cleaned["metrics"],
            cleaned["checks"],
            cleaned["aggregates"],
        )
    return cleaned


__all__ = [
    "aggregate_metrics",
    "aggregate_sample_rows",
    "bucket_needs_recompute",
    "bucket_start_5m",
    "cleanup_monitoring_data",
    "combine_5m_rows",
    "hourly_bucket_start",
    "weighted_average",
]
