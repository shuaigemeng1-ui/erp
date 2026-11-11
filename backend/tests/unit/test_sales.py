"""销售 / 任务模块单元测试（纯逻辑，不连接 MySQL / Redis，不调用真实平台接口）。

覆盖：
* 库存影响差额计算与「重复拉单 / 回调 / 重试不重复扣减」的幂等规则；
* 平台订单状态映射、金额 Decimal、明细合并与异常标记；
* 订单 / 原始数据 / 库存影响 / 回调事件的防重唯一键（模型层校验）；
* Outbox 路由解析、重试退避策略与任务结果解释；
* 定时调度下次执行时间；
* 库存推送数量下限为 0 与串行锁名；
* 表与字段中文注释、无外键（本模块自己负责的表）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import AppError, ErrorCode
from app.db.base import assert_comments
from app.modules.sales.order_mapping import (
    DEDUCTING_STATUSES,
    NormalizedOrderStatus,
    compute_stock_delta,
    deducts_stock,
    mapping_status_for,
    merge_abnormal_reasons,
    normalize_platform_status,
    parse_epoch,
    parse_order_payload,
    parse_shopee_order,
    push_quantity,
    target_stock_quantity,
    to_decimal,
)
from app.modules.tasks.outbox import OUTBOX_ROUTES, register_outbox_route, resolve_target
from app.modules.tasks.retry import (
    MAX_OUTBOX_ATTEMPTS,
    is_retryable_error,
    retry_delay_seconds,
    should_retry,
)
from app.modules.tasks.scheduler import compute_next_run_at
from app.modules.tasks.tasks import handler_kwargs, interpret_result

SALES_TABLES = {
    "orders",
    "order_items",
    "order_status_history",
    "platform_order_raw",
    "order_stock_effects",
}
TASK_TABLES = {"sync_schedules", "sync_runs", "webhook_events", "outbox_events"}


# --------------------------------------------------------------------------- 库存差额
class TestStockDelta:
    """库存影响按「目标 - 已应用」计算差额，差额为 0 时不得触碰库存。"""

    @pytest.mark.parametrize(
        ("target", "applied", "expected"),
        [
            (3, 0, 3),  # 首次扣减
            (3, 3, 0),  # 重复拉单：已扣过，不再扣
            (0, 3, -3),  # 取消订单：恢复已扣的 3 件
            (0, 0, 0),  # 未付款订单：不扣也不恢复
            (2, 5, -3),  # 部分退减（数量调小）
            (5, 2, 3),  # 数量调大：只补差额
        ],
    )
    def test_delta(self, target: int, applied: int, expected: int) -> None:
        assert compute_stock_delta(target, applied) == expected

    def test_repeat_is_idempotent(self) -> None:
        """同一目标重复计算：第二次差额必为 0。"""
        applied = 0
        for _ in range(3):
            delta = compute_stock_delta(4, applied)
            applied += delta
        assert applied == 4
        assert compute_stock_delta(4, applied) == 0

    @pytest.mark.parametrize("status", ["paid", "shipped", "completed"])
    def test_deducting_statuses(self, status: str) -> None:
        assert deducts_stock(status) is True
        assert target_stock_quantity(3, status) == 3

    @pytest.mark.parametrize("status", ["pending", "cancelled", "unknown"])
    def test_non_deducting_statuses(self, status: str) -> None:
        assert deducts_stock(status) is False
        assert target_stock_quantity(3, status) == 0

    def test_negative_quantity_clamped(self) -> None:
        assert target_stock_quantity(-5, "paid") == 0

    def test_deducting_statuses_constant(self) -> None:
        assert DEDUCTING_STATUSES == {"paid", "shipped", "completed"}


# --------------------------------------------------------------------------- 状态映射
class TestStatusMapping:
    @pytest.mark.parametrize(
        ("platform_status", "expected"),
        [
            ("UNPAID", NormalizedOrderStatus.PENDING),
            ("READY_TO_SHIP", NormalizedOrderStatus.PAID),
            ("PROCESSED", NormalizedOrderStatus.PAID),
            ("IN_CANCEL", NormalizedOrderStatus.PAID),
            ("SHIPPED", NormalizedOrderStatus.SHIPPED),
            ("TO_CONFIRM_RECEIVE", NormalizedOrderStatus.SHIPPED),
            ("TO_RETURN", NormalizedOrderStatus.SHIPPED),
            ("COMPLETED", NormalizedOrderStatus.COMPLETED),
            ("CANCELLED", NormalizedOrderStatus.CANCELLED),
            ("SOMETHING_NEW", NormalizedOrderStatus.UNKNOWN),
            ("", NormalizedOrderStatus.UNKNOWN),
            (None, NormalizedOrderStatus.UNKNOWN),
        ],
    )
    def test_shopee_status(self, platform_status: str | None, expected: str) -> None:
        assert normalize_platform_status("shopee", platform_status) == expected

    def test_case_insensitive_and_unknown_channel(self) -> None:
        assert normalize_platform_status("Shopee", " ready_to_ship ") == NormalizedOrderStatus.PAID
        assert normalize_platform_status("lazada", "SHIPPED") == NormalizedOrderStatus.UNKNOWN

    def test_mapping_status(self) -> None:
        assert mapping_status_for(12) == "mapped"
        assert mapping_status_for(None) == "missing"


# --------------------------------------------------------------------------- 金额与时间
class TestMoneyAndTime:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("12.3400", Decimal("12.3400")),
            (12.5, Decimal("12.5")),
            (None, Decimal("0")),
            ("", Decimal("0")),
            ("abc", Decimal("0")),
        ],
    )
    def test_to_decimal(self, raw: object, expected: Decimal) -> None:
        assert to_decimal(raw) == expected

    def test_to_decimal_never_float_drift(self) -> None:
        assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")

    def test_parse_epoch(self) -> None:
        value = parse_epoch(1700000000)
        assert value == datetime(2023, 11, 14, 22, 13, 20)
        assert value.tzinfo is None  # 入库统一 UTC naive
        assert parse_epoch("1700000000") == value
        assert parse_epoch(None) is None
        assert parse_epoch("not-a-time") is None
        assert parse_epoch(0) is None


# --------------------------------------------------------------------------- 订单解析
def _shopee_payload(**overrides: object) -> dict:
    payload = {
        "order_sn": "231111ABCDEFG",
        "order_status": "READY_TO_SHIP",
        "create_time": 1700000000,
        "currency": "PHP",
        "total_amount": "199.5000",
        "buyer_username": "buyer_01",
        "item_list": [
            {
                "item_id": 1001,
                "model_id": 2002,
                "model_sku": "SKU-RED-M",
                "item_name": "测试商品",
                "model_quantity_purchased": 2,
                "model_original_price": "100.00",
                "model_discounted_price": "90.00",
            },
            {
                "item_id": 1001,
                "model_id": 2002,
                "model_sku": "SKU-RED-M",
                "item_name": "测试商品",
                "model_quantity_purchased": 1,
                "model_original_price": "100.00",
                "model_discounted_price": "90.00",
            },
        ],
    }
    payload.update(overrides)
    return payload


class TestOrderParsing:
    def test_parse_order(self) -> None:
        parsed = parse_shopee_order(_shopee_payload())
        assert parsed.external_order_no == "231111ABCDEFG"
        assert parsed.status == NormalizedOrderStatus.PAID
        assert parsed.currency == "PHP"
        assert parsed.total_amount == Decimal("199.5000")
        assert parsed.buyer_name == "buyer_01"
        assert parsed.placed_at == datetime(2023, 11, 14, 22, 13, 20)
        assert parsed.abnormal_reason == ""

    def test_duplicate_lines_are_merged(self) -> None:
        """同一商品同一规格出现多行时合并数量，保证明细防重键唯一。"""
        parsed = parse_shopee_order(_shopee_payload())
        assert len(parsed.lines) == 1
        line = parsed.lines[0]
        assert line.quantity == 3
        assert line.unit_price == Decimal("90.00")
        assert line.discount_amount == Decimal("30.00")  # (100-90) * 3

    def test_unknown_status_marks_abnormal(self) -> None:
        parsed = parse_shopee_order(_shopee_payload(order_status="BRAND_NEW"))
        assert parsed.status == NormalizedOrderStatus.UNKNOWN
        assert "未知平台订单状态" in parsed.abnormal_reason
        assert deducts_stock(parsed.status) is False

    def test_missing_order_sn_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_shopee_order(_shopee_payload(order_sn=""))

    def test_parse_order_payload_unsupported_channel(self) -> None:
        with pytest.raises(ValueError):
            parse_order_payload("lazada", {})

    def test_parse_order_payload_dispatch(self) -> None:
        assert parse_order_payload("shopee", _shopee_payload()).external_order_no == "231111ABCDEFG"


class TestAbnormalReasons:
    def test_merge_dedup_and_order(self) -> None:
        merged = merge_abnormal_reasons("未映射 SKU；未知状态", ["未知状态", "负库存"])
        assert merged == "未映射 SKU；未知状态；负库存"

    def test_merge_truncates(self) -> None:
        merged = merge_abnormal_reasons("", ["问题" * 200], limit=20)
        assert len(merged) == 20

    def test_empty(self) -> None:
        assert merge_abnormal_reasons("", []) == ""


# --------------------------------------------------------------------------- 推送数量与锁
class TestPush:
    @pytest.mark.parametrize(
        ("available", "expected"), [(-5, 0), (0, 0), (7, 7), (100, 100)]
    )
    def test_push_quantity_floor_zero(self, available: int, expected: int) -> None:
        """推送平台的可售库存最低为 0：负库存只在本系统显示异常。"""
        assert push_quantity(available) == expected

    def test_lock_names_are_scoped(self) -> None:
        from app.modules.sales.service import stock_push_lock_name

        assert stock_push_lock_name(1, 2) == "stock_push:1:2"
        assert stock_push_lock_name(1, 2, 3) == "stock_push:1:2:3"
        assert stock_push_lock_name(1, 2, 3) != stock_push_lock_name(1, 2, 4)


# --------------------------------------------------------------------------- 防重键
class TestUniqueness:
    def _unique_columns(self, table_name: str) -> set[tuple[str, ...]]:
        from app.db.base import TenantBase

        table = TenantBase.metadata.tables[table_name]
        return {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }

    def test_order_unique_key(self) -> None:
        assert ("tenant_id", "store_id", "external_order_no") in self._unique_columns("orders")

    def test_platform_raw_unique_key(self) -> None:
        assert ("tenant_id", "store_id", "external_order_no") in self._unique_columns(
            "platform_order_raw"
        )

    def test_order_item_unique_key(self) -> None:
        assert (
            "tenant_id",
            "order_id",
            "external_item_id",
            "external_sku_id",
        ) in self._unique_columns("order_items")

    def test_stock_effect_unique_key(self) -> None:
        """同一明细同一库存地点只有一条效果记录，是差额幂等的持久化保证。"""
        assert ("tenant_id", "order_item_id", "stock_place_id") in self._unique_columns(
            "order_stock_effects"
        )

    def test_webhook_event_unique_key(self) -> None:
        assert ("tenant_id", "channel", "external_event_id") in self._unique_columns(
            "webhook_events"
        )

    def test_schedule_unique_key(self) -> None:
        assert ("tenant_id", "job_type", "store_id") in self._unique_columns("sync_schedules")

    def test_no_foreign_keys_in_my_tables(self) -> None:
        from app.db.base import TenantBase

        offenders: list[str] = []
        for name in SALES_TABLES | TASK_TABLES:
            table = TenantBase.metadata.tables.get(name)
            assert table is not None, f"缺少表 {name}"
            offenders.extend(f"{name}.{fk.parent.name}" for fk in table.foreign_keys)
        assert offenders == []


# --------------------------------------------------------------------------- 注释与元数据
class TestModelMetadata:
    def test_chinese_comments(self) -> None:
        """本模块负责的表与字段必须带中文注释（plan.txt 硬性要求）。"""
        from app.db.base import TenantBase

        problems = assert_comments(TenantBase.metadata)
        prefix = tuple(
            f"{name}." for name in SALES_TABLES | TASK_TABLES
        ) + tuple(f"表 {name} " for name in SALES_TABLES | TASK_TABLES)
        own_problems = [item for item in problems if item.startswith(prefix)]
        assert own_problems == []

    def test_expected_columns_present(self) -> None:
        from app.db.base import TenantBase

        expected = {
            "orders": {
                "tenant_id",
                "store_id",
                "channel",
                "external_order_no",
                "status",
                "platform_status",
                "currency",
                "total_amount",
                "buyer_name",
                "placed_at",
                "raw_id",
                "abnormal_reason",
            },
            "order_items": {
                "tenant_id",
                "order_id",
                "external_item_id",
                "external_sku_id",
                "sku_id",
                "quantity",
                "unit_price",
                "discount_amount",
                "item_status",
                "mapping_status",
            },
            "order_status_history": {
                "tenant_id",
                "order_id",
                "from_status",
                "to_status",
                "platform_status",
                "source",
                "changed_at",
            },
            "platform_order_raw": {
                "tenant_id",
                "channel",
                "store_id",
                "external_order_no",
                "payload",
                "fetched_at",
            },
            "order_stock_effects": {
                "tenant_id",
                "order_id",
                "order_item_id",
                "sku_id",
                "stock_place_id",
                "applied_quantity",
                "target_quantity",
                "status",
                "last_task_id",
            },
            "sync_schedules": {
                "tenant_id",
                "job_type",
                "store_id",
                "interval_minutes",
                "enabled",
                "last_run_at",
                "next_run_at",
            },
            "sync_runs": {
                "tenant_id",
                "job_type",
                "store_id",
                "status",
                "triggered_by",
                "requested_by",
                "started_at",
                "finished_at",
                "total_processed",
                "error_message",
                "cursor",
            },
            "webhook_events": {
                "tenant_id",
                "channel",
                "store_id",
                "event_type",
                "external_event_id",
                "signature_ok",
                "status",
                "payload",
                "received_at",
                "processed_at",
            },
            "outbox_events": {
                "tenant_id",
                "event_type",
                "aggregate_type",
                "aggregate_id",
                "payload",
                "status",
                "attempts",
                "available_at",
                "dispatched_at",
                "last_error",
            },
        }

        for table_name, columns in expected.items():
            table = TenantBase.metadata.tables[table_name]
            missing = columns - {column.name for column in table.columns}
            assert missing == set(), f"{table_name} 缺少字段: {sorted(missing)}"


# --------------------------------------------------------------------------- Outbox 路由
class TestOutboxRouting:
    def test_sync_run_route_uses_job_queue(self) -> None:
        task_name, queue, kwargs = resolve_target(
            "sync_run.created",
            {"sync_run_id": 7, "tenant_id": 3, "job_type": "order_sync", "store_id": 9},
        )
        assert task_name == "app.modules.tasks.tasks.execute_sync_run"
        assert queue == "sales"  # 拉单任务走独立 sales 队列
        assert kwargs["sync_run_id"] == 7
        assert kwargs["tenant_id"] == 3
        assert kwargs["job_type"] == "order_sync"
        assert kwargs["store_id"] == 9

    def test_stock_push_single_sku(self) -> None:
        task_name, queue, kwargs = resolve_target(
            "stock.push.requested",
            {"tenant_id": 1, "store_id": 2, "sku_ids": [5]},
        )
        assert task_name == "app.modules.channels.tasks.push_stock_for_sku"
        assert queue == "channel"
        assert kwargs == {"tenant_id": 1, "store_id": 2, "sku_id": 5}

    def test_stock_push_store_wide(self) -> None:
        task_name, _queue, kwargs = resolve_target(
            "stock.push.requested",
            {"tenant_id": 1, "store_id": 2, "sku_ids": [5, 6]},
        )
        assert task_name == "app.modules.channels.tasks.push_stock"
        assert kwargs["sku_ids"] == [5, 6]

    def test_unknown_event_raises(self) -> None:
        with pytest.raises(AppError):
            resolve_target("unknown.event", {})

    def test_register_route(self) -> None:
        register_outbox_route("test.custom.event", "app.modules.tasks.tasks.execute_sync_run", "default")
        assert OUTBOX_ROUTES["test.custom.event"].task_name.endswith("execute_sync_run")
        with pytest.raises(AppError):
            register_outbox_route("", "app.modules.tasks.tasks.execute_sync_run")

    def test_default_routes_registered(self) -> None:
        assert {
            "sync_run.created",
            "order.normalize",
            "stock.push.requested",
        } <= set(OUTBOX_ROUTES)


# --------------------------------------------------------------------------- 重试策略
class TestRetryPolicy:
    def test_backoff(self) -> None:
        assert retry_delay_seconds(1) == 30
        assert retry_delay_seconds(2) == 60
        assert retry_delay_seconds(3) == 120
        assert retry_delay_seconds(10) == 1800  # 封顶 30 分钟

    def test_should_retry(self) -> None:
        assert should_retry(0) is True
        assert should_retry(MAX_OUTBOX_ATTEMPTS - 1) is True
        assert should_retry(MAX_OUTBOX_ATTEMPTS) is False

    def test_permanent_business_error_not_retryable(self) -> None:
        """权限 / 参数 / 未实现依赖类错误直接失败，不做无意义重试。"""
        assert is_retryable_error(AppError(ErrorCode.PERMISSION_DENIED)) is False
        assert is_retryable_error(AppError(ErrorCode.VALIDATION_ERROR)) is False
        assert is_retryable_error(ValueError("bad payload")) is False
        assert is_retryable_error(NotImplementedError("inventory not ready")) is False

    def test_transient_errors_retryable(self) -> None:
        assert is_retryable_error(AppError(ErrorCode.SERVICE_UNAVAILABLE)) is True
        assert is_retryable_error(AppError(ErrorCode.RATE_LIMITED)) is True

        import httpx

        assert is_retryable_error(httpx.ConnectTimeout("timeout")) is True

    def test_http_429_retryable(self) -> None:
        import httpx

        request = httpx.Request("GET", "https://example.invalid")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError("too many", request=request, response=response)
        assert is_retryable_error(error) is True
        response_400 = httpx.Response(400, request=request)
        error_400 = httpx.HTTPStatusError("bad", request=request, response=response_400)
        assert is_retryable_error(error_400) is False


# --------------------------------------------------------------------------- 任务与调度
class TestTaskRules:
    def test_next_run_at(self) -> None:
        now = datetime(2026, 1, 1, 0, 0, 0)
        assert compute_next_run_at(now, 30) == now + timedelta(minutes=30)
        assert compute_next_run_at(now, 0) == now + timedelta(minutes=1)  # 最短 1 分钟
        assert compute_next_run_at(now, 99999) == now + timedelta(minutes=1440)

    def test_handler_kwargs(self) -> None:
        kwargs = handler_kwargs(
            "order_sync",
            tenant_id=1,
            sync_run_id=2,
            store_id=3,
            requested_by=4,
            triggered_by="manual",
        )
        assert kwargs == {
            "tenant_id": 1,
            "sync_run_id": 2,
            "store_id": 3,
            "requested_by": 4,
            "triggered_by": "manual",
        }
        stock_kwargs = handler_kwargs(
            "stock_push",
            tenant_id=1,
            sync_run_id=2,
            store_id=3,
            requested_by=None,
            triggered_by="schedule",
        )
        assert stock_kwargs == {"tenant_id": 1, "store_id": 3, "sync_run_id": 2}

    def test_interpret_result(self) -> None:
        assert interpret_result(
            {"status": "succeeded", "total_processed": 5}
        ) == ("succeeded", 5, "")
        assert interpret_result({"errors": 2, "messages": ["a", "b"]}) == (
            "partial",
            0,
            "a；b",
        )
        assert interpret_result({"status": "weird"})[0] == "succeeded"
        assert interpret_result({"orders_created": 2, "orders_updated": 3})[1] == 5

    def test_summarize_ingest(self) -> None:
        from app.modules.sales.service import summarize_ingest

        status, processed, message = summarize_ingest(
            {
                "total": 3,
                "orders_created": 2,
                "orders_updated": 1,
                "errors": 0,
                "messages": [],
            }
        )
        assert (status, processed, message) == ("succeeded", 3, "")

        status, processed, message = summarize_ingest(
            {"total": 2, "orders_created": 1, "errors": 1, "messages": ["失败一笔"]}
        )
        assert status == "partial"
        assert processed == 1
        assert "失败一笔" in message

        status, _processed, message = summarize_ingest(
            {"total": 1, "orders_created": 0, "errors": 1, "messages": ["全失败"]}
        )
        assert status == "failed"
        assert "全失败" in message


# --------------------------------------------------------------------------- 响应模型
class TestSchemas:
    def test_id_serialized_as_string_and_money_decimal(self) -> None:
        from app.modules.sales.schemas import OrderOut

        payload = OrderOut(
            id=123456789012345,
            store_id=7,
            channel="shopee",
            external_order_no="SN-1",
            status="paid",
            platform_status="READY_TO_SHIP",
            currency="PHP",
            total_amount=Decimal("199.5000"),
            buyer_name="b",
            placed_at=None,
            abnormal_reason="",
        ).model_dump(mode="json")
        assert payload["id"] == "123456789012345"
        assert payload["store_id"] == "7"
        assert isinstance(payload["total_amount"], str)
        assert payload["total_amount"] == "199.5000"

    def test_schedule_update_validation(self) -> None:
        from pydantic import ValidationError

        from app.modules.tasks.schemas import SyncScheduleUpdate

        assert SyncScheduleUpdate().interval_minutes is None
        with pytest.raises(ValidationError):
            SyncScheduleUpdate(interval_minutes=0)


# --------------------------------------------------------------------------- 订单闭环
SQLITE_TABLES = (
    "platform_order_raw",
    "orders",
    "order_items",
    "order_status_history",
    "order_stock_effects",
    "sync_schedules",
    "sync_runs",
    "webhook_events",
    "outbox_events",
)


def _sqlite_metadata():
    """为内存 SQLite 复制本模块 9 张表的元数据（BIGINT 主键改为 INTEGER 以支持自增）。

    只用于测试：不连接 MySQL / Redis，也不依赖其它模块的表与服务。
    """
    from sqlalchemy import Integer, MetaData

    from app.db.base import TenantBase

    copy = MetaData()
    for name in SQLITE_TABLES:
        TenantBase.metadata.tables[name].to_metadata(copy)
    for table in copy.tables.values():
        table.c.id.type = Integer()
    return copy


@pytest.fixture()
def session():
    """内存 SQLite 会话：只建本模块的表，验证订单闭环的真实读写行为。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite+pysqlite:///:memory:")
    _sqlite_metadata().create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def stock_stub(monkeypatch):
    """替换库存 / SKU 映射两个跨模块依赖，记录每次库存变动。"""
    from app.modules.sales import service as sales_service

    balances: dict[tuple[int, int], int] = {}
    calls: list[dict] = []

    def fake_apply_balance_delta(db, **kwargs):
        calls.append(kwargs)
        key = (int(kwargs["sku_id"]), int(kwargs["stock_place_id"]))
        balances[key] = balances.get(key, 10) + int(kwargs["quantity_change"])
        return balances[key]

    monkeypatch.setattr(sales_service, "resolve_default_stock_place", lambda _db, _tenant: 9001)
    monkeypatch.setattr(sales_service, "_apply_balance_delta", fake_apply_balance_delta)
    return {"balances": balances, "calls": calls}


def _mapping_for(*pairs: tuple[str, int]):
    """构造平台商品键 → 本地 SKU 的映射（未列出的键视为未映射）。"""
    table = {key: sku for key, sku in pairs}

    def resolver(_db, *, tenant_id, store_id, keys):
        return {
            key: (table.get(key[1]), "mapped" if table.get(key[1]) else "missing")
            for key in keys
        }

    return resolver


def _order_payload(**overrides: object) -> dict:
    payload = {
        "order_sn": "SN-1",
        "order_status": "READY_TO_SHIP",
        "currency": "PHP",
        "total_amount": "30.00",
        "create_time": 1700000000,
        "buyer_username": "b1",
        "item_list": [
            {
                "item_id": 1,
                "model_sku": "SKU-1",
                "item_name": "商品A",
                "model_quantity_purchased": 3,
                "model_original_price": "10.00",
                "model_discounted_price": "10.00",
            }
        ],
    }
    payload.update(overrides)
    return payload


class TestOrderClosure:
    """订单闭环：原始数据先落库 → 归一化 → 状态历史 → 库存影响 → 推送事件。"""

    def _ingest(self, db, payloads, *, tenant_id=1, store_id=2):
        from app.modules.sales import service as sales_service

        return sales_service.ingest_platform_orders(
            db,
            tenant_id=tenant_id,
            store_id=store_id,
            channel="shopee",
            payloads=payloads,
        )

    def test_repeat_ingest_no_duplicate_order_or_stock(self, session, monkeypatch, stock_stub):
        from sqlalchemy import func, select

        from app.modules.sales import service as sales_service
        from app.modules.sales.models_tenant import (
            Order,
            OrderItem,
            OrderStatusHistory,
            OrderStockEffect,
            PlatformOrderRaw,
        )

        monkeypatch.setattr(
            sales_service, "_resolve_sku_mapping", _mapping_for(("SKU-1", 501))
        )
        first = self._ingest(session, [_order_payload()])
        second = self._ingest(session, [_order_payload()])  # 重复拉单

        assert first["orders_created"] == 1
        assert second["orders_created"] == 0 and second["orders_updated"] == 1
        assert second["stock_changed"] == 0  # 差额为 0，不重复扣减

        assert session.execute(select(func.count()).select_from(Order)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(OrderItem)).scalar_one() == 1
        assert (
            session.execute(select(func.count()).select_from(PlatformOrderRaw)).scalar_one() == 1
        )
        # 只在新建订单时写一条状态历史（重复同步同状态不追加）
        assert (
            session.execute(select(func.count()).select_from(OrderStatusHistory)).scalar_one() == 1
        )
        effect = session.execute(select(OrderStockEffect)).scalar_one()
        assert (effect.applied_quantity, effect.target_quantity, effect.status) == (3, 3, "applied")
        assert stock_stub["balances"][(501, 9001)] == 7  # 10 - 3，只扣一次
        assert len(stock_stub["calls"]) == 1

    def test_cancel_restores_stock_once(self, session, monkeypatch, stock_stub):
        from sqlalchemy import func, select

        from app.modules.sales import service as sales_service
        from app.modules.sales.models_tenant import (
            OrderStatusHistory,
            OrderStockEffect,
        )

        monkeypatch.setattr(
            sales_service, "_resolve_sku_mapping", _mapping_for(("SKU-1", 501))
        )
        self._ingest(session, [_order_payload()])
        cancelled = _order_payload(order_status="CANCELLED")
        self._ingest(session, [cancelled])
        self._ingest(session, [cancelled])  # 重复回调同样不重复恢复

        effect = session.execute(select(OrderStockEffect)).scalar_one()
        assert (effect.applied_quantity, effect.target_quantity) == (0, 0)
        assert stock_stub["balances"][(501, 9001)] == 10  # 恢复原数量
        history = [
            (row.from_status, row.to_status)
            for row in session.execute(select(OrderStatusHistory)).scalars()
        ]
        assert history == [("", "paid"), ("paid", "cancelled")]
        assert (
            session.execute(select(func.count()).select_from(OrderStatusHistory)).scalar_one() == 2
        )

    def test_quantity_change_only_applies_difference(self, session, monkeypatch, stock_stub):
        from sqlalchemy import select

        from app.modules.sales import service as sales_service
        from app.modules.sales.models_tenant import OrderStockEffect

        monkeypatch.setattr(
            sales_service, "_resolve_sku_mapping", _mapping_for(("SKU-1", 501))
        )
        self._ingest(session, [_order_payload()])
        assert stock_stub["balances"][(501, 9001)] == 7

        smaller = _order_payload()
        smaller["item_list"][0]["model_quantity_purchased"] = 1  # 平台改单：3 → 1
        self._ingest(session, [smaller])

        effect = session.execute(select(OrderStockEffect)).scalar_one()
        assert (effect.applied_quantity, effect.target_quantity) == (1, 1)
        assert stock_stub["balances"][(501, 9001)] == 9  # 只恢复 2 件

    def test_unmapped_sku_saved_but_no_stock_effect(self, session, monkeypatch, stock_stub):
        from sqlalchemy import func, select

        from app.modules.sales import service as sales_service
        from app.modules.sales.models_tenant import Order, OrderItem, OrderStockEffect

        monkeypatch.setattr(sales_service, "_resolve_sku_mapping", _mapping_for())
        result = self._ingest(session, [_order_payload()])

        assert result["orders_created"] == 1
        order = session.execute(select(Order)).scalar_one()
        assert "未映射 SKU" in order.abnormal_reason
        item = session.execute(select(OrderItem)).scalar_one()
        assert item.sku_id is None
        assert item.mapping_status == "missing"
        # 不猜测扣减对象：不写库存影响、不调库存服务
        assert (
            session.execute(select(func.count()).select_from(OrderStockEffect)).scalar_one() == 0
        )
        assert stock_stub["calls"] == []

    def test_unknown_status_does_not_touch_stock(self, session, monkeypatch, stock_stub):
        """无法识别的平台状态不猜库存：只保存并标记异常，状态明确后再结算。"""
        from sqlalchemy import func, select

        from app.modules.sales import service as sales_service
        from app.modules.sales.models_tenant import Order, OrderStockEffect

        monkeypatch.setattr(
            sales_service, "_resolve_sku_mapping", _mapping_for(("SKU-1", 501))
        )
        self._ingest(session, [_order_payload(order_status="BRAND_NEW")])

        order = session.execute(select(Order)).scalar_one()
        assert "未知平台订单状态" in order.abnormal_reason
        assert stock_stub["calls"] == []  # 没有库存动作
        assert (
            session.execute(select(func.count()).select_from(OrderStockEffect)).scalar_one() == 0
        )

        # 平台状态明确后再次同步：补扣一次，异常自动清除
        self._ingest(session, [_order_payload()])
        assert stock_stub["balances"][(501, 9001)] == 7
        order = session.execute(select(Order)).scalar_one()
        assert order.abnormal_reason == ""
        assert len(stock_stub["calls"]) == 1

    def test_negative_stock_allowed_but_marked_abnormal(self, session, monkeypatch):
        """外部已成立订单允许负库存（不丢单），但订单必须标记异常。"""
        from sqlalchemy import select

        from app.modules.sales import service as sales_service
        from app.modules.sales.models_tenant import Order

        monkeypatch.setattr(sales_service, "resolve_default_stock_place", lambda _db, _t: 9001)
        monkeypatch.setattr(sales_service, "_resolve_sku_mapping", _mapping_for(("SKU-1", 501)))

        def apply_negative(_db, **kwargs):
            assert kwargs["allow_negative"] is True  # 订单影响允许负库存
            return -2

        monkeypatch.setattr(sales_service, "_apply_balance_delta", apply_negative)
        self._ingest(session, [_order_payload()])

        order = session.execute(select(Order)).scalar_one()
        assert "负库存" in order.abnormal_reason

    def test_stock_push_events_are_merged(self, session, monkeypatch, stock_stub):
        from sqlalchemy import func, select

        from app.modules.sales import service as sales_service
        from app.modules.tasks.models_tenant import OutboxEvent

        monkeypatch.setattr(
            sales_service, "_resolve_sku_mapping", _mapping_for(("SKU-1", 501), ("SKU-2", 502))
        )
        payload_a = _order_payload()
        payload_b = _order_payload(order_sn="SN-2")
        payload_b["item_list"] = [
            {
                "item_id": 2,
                "model_sku": "SKU-2",
                "item_name": "商品B",
                "model_quantity_purchased": 1,
                "model_original_price": "5.00",
                "model_discounted_price": "5.00",
            }
        ]
        self._ingest(session, [payload_a, payload_b])

        events = list(session.execute(select(OutboxEvent)).scalars())
        assert len(events) == 1  # 同一店铺的推送事件合并为一条
        assert events[0].event_type == "stock.push.requested"
        assert sorted(events[0].payload["sku_ids"]) == [501, 502]
        assert (
            session.execute(select(func.count()).select_from(OutboxEvent)).scalar_one() == 1
        )

