"""订单归一化与库存影响计算（纯函数，无数据库 / 无网络依赖）。

把「平台状态 → 统一状态 → 是否扣减库存 → 本次库存差额」这一段业务规则集中在这里，
方便单元测试与后续按租户配置化（见 plan.txt §4「库存状态映射沿用现有项目的可配置
扣减机制，初始映射从现有 Shopee 初始化配置提取」）。

关键规则：

* 归一化状态固定 6 个取值：``pending``（未付款）、``paid``（已付款待发货）、
  ``shipped``（已发货）、``completed``（已完成）、``cancelled``（已取消）、
  ``unknown``（无法识别，订单照常保存并标记异常，不猜测库存对象）。
* 扣减库存的状态集合为 ``DEDUCTING_STATUSES``（paid / shipped / completed）；
  退款、退货通知不直接视为实物入库，不在此自动恢复库存。
* 库存影响按「目标数量 - 已应用数量」计算本次差额，差额为 0 时不触碰库存，
  从根上保证重复拉单 / 回调 / 重试不会重复扣减或重复恢复。
* 推送平台的可售库存最低为 0（外部订单造成的负库存只在本系统显示异常）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum

# 允许的归一化订单状态（写库前统一校验，避免脏值进入统计）
NORMALIZED_ORDER_STATUSES: tuple[str, ...] = (
    "pending",
    "paid",
    "shipped",
    "completed",
    "cancelled",
    "unknown",
)


class NormalizedOrderStatus(StrEnum):
    """统一订单状态。平台原始状态保留在 ``orders.platform_status``。"""

    PENDING = "pending"      # 未付款
    PAID = "paid"            # 已付款 / 待发货
    SHIPPED = "shipped"      # 已发货 / 待收货
    COMPLETED = "completed"  # 已完成
    CANCELLED = "cancelled"  # 已取消
    UNKNOWN = "unknown"      # 无法识别（标记异常，不扣库存）


# 会扣减库存的状态：与现有项目「订单状态可配置 subtract_stock」等价，首版固定在此。
DEDUCTING_STATUSES: frozenset[str] = frozenset(
    {
        NormalizedOrderStatus.PAID,
        NormalizedOrderStatus.SHIPPED,
        NormalizedOrderStatus.COMPLETED,
    }
)

# 首版只接入 Shopee；新增渠道时在这里补一张映射表，不动下游逻辑。
SHOPEE_STATUS_MAP: dict[str, NormalizedOrderStatus] = {
    # 未付款：不扣库存
    "UNPAID": NormalizedOrderStatus.PENDING,
    # 已付款 / 待发货类状态：扣库存
    "READY_TO_SHIP": NormalizedOrderStatus.PAID,
    "PROCESSED": NormalizedOrderStatus.PAID,
    "INVOICE_PENDING": NormalizedOrderStatus.PAID,
    "IN_CANCEL": NormalizedOrderStatus.PAID,
    "RETRY_SHIP": NormalizedOrderStatus.PAID,
    # 已发货 / 待收货 / 退货中：已扣库存，不自动恢复
    "SHIPPED": NormalizedOrderStatus.SHIPPED,
    "TO_CONFIRM_RECEIVE": NormalizedOrderStatus.SHIPPED,
    "TO_RETURN": NormalizedOrderStatus.SHIPPED,
    # 已完成
    "COMPLETED": NormalizedOrderStatus.COMPLETED,
    # 已取消：不占用库存
    "CANCELLED": NormalizedOrderStatus.CANCELLED,
}

# TikTok Shop 订单状态（旧应用 ch_tiktok_order_status_map 的归一化映射）
TIKTOK_STATUS_MAP: dict[str, NormalizedOrderStatus] = {
    "UNPAID": NormalizedOrderStatus.PENDING,
    "ON_HOLD": NormalizedOrderStatus.PENDING,
    "AWAITING_SHIPMENT": NormalizedOrderStatus.PAID,
    "AWAITING_COLLECTION": NormalizedOrderStatus.PAID,
    "PARTIALLY_SHIPPING": NormalizedOrderStatus.SHIPPED,
    "IN_TRANSIT": NormalizedOrderStatus.SHIPPED,
    "DELIVERED": NormalizedOrderStatus.SHIPPED,
    "COMPLETED": NormalizedOrderStatus.COMPLETED,
    "CANCELLED": NormalizedOrderStatus.CANCELLED,
}

# 渠道 -> 平台状态映射表。未登记渠道一律返回 unknown。
CHANNEL_STATUS_MAPS: dict[str, dict[str, NormalizedOrderStatus]] = {
    "shopee": SHOPEE_STATUS_MAP,
    "tiktok": TIKTOK_STATUS_MAP,
}

# 明细映射状态
MAPPING_MAPPED = "mapped"
MAPPING_MISSING = "missing"
MAPPING_AMBIGUOUS = "ambiguous"


def normalize_platform_status(channel: str, platform_status: str | None) -> NormalizedOrderStatus:
    """平台状态 → 统一状态。未知状态返回 ``unknown``（调用方负责标记异常）。"""
    mapping = CHANNEL_STATUS_MAPS.get((channel or "").strip().lower(), {})
    key = (platform_status or "").strip().upper()
    return mapping.get(key, NormalizedOrderStatus.UNKNOWN)


def deducts_stock(status: str) -> bool:
    """该归一化状态是否需要占用（扣减）库存。"""
    return status in DEDUCTING_STATUSES


def target_stock_quantity(quantity: int, status: str) -> int:
    """目标库存影响量：扣减状态为购买数量，其它状态为 0（已扣的要恢复）。"""
    quantity = max(0, int(quantity))
    return quantity if deducts_stock(status) else 0


def compute_stock_delta(target_quantity: int, applied_quantity: int) -> int:
    """本次需要写入库存的差额 = 目标 - 已应用。

    返回正数表示「还需要扣减」，负数表示「需要恢复」，0 表示无需触碰库存。
    重复拉单 / 回调 / 重试时 ``applied_quantity`` 已是旧目标，差额自然为 0。
    """
    return int(target_quantity) - int(applied_quantity)


def push_quantity(available_quantity: int) -> int:
    """推送平台的可售库存：最低为 0，负库存只在本系统显示异常。"""
    return max(0, int(available_quantity))


def mapping_status_for(sku_id: int | None) -> str:
    """明细 SKU 映射状态：空 = 未映射（订单仍保存，但不猜扣减对象）。"""
    return MAPPING_MAPPED if sku_id else MAPPING_MISSING


def merge_abnormal_reasons(
    current: str, reasons: list[str] | tuple[str, ...], limit: int = 512
) -> str:
    """合并订单异常原因（去重、保持顺序、超长截断）。

    订单异常只做标记，不阻塞保存：无法映射 SKU / 未知状态 / 负库存都记录在这里，
    由授权人员在页面确认后再人工处理库存。
    """
    parts: list[str] = []
    for item in (current or "").split("；"):
        value = item.strip()
        if value and value not in parts:
            parts.append(value)
    for item in reasons:
        value = (item or "").strip()
        if value and value not in parts:
            parts.append(value)
    text_value = "；".join(parts)
    return text_value[:limit] if limit and len(text_value) > limit else text_value


def to_decimal(value: object, default: Decimal = Decimal("0")) -> Decimal:
    """把平台返回的金额安全转为 Decimal；非法值返回默认值（金额禁止用 float 运算）。"""
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def parse_epoch(value: object) -> datetime | None:
    """解析平台时间戳（秒）为 UTC naive datetime；非法值返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ParsedOrderLine:
    """归一化后的订单明细行（平台差异已在解析层抹平）。"""

    external_item_id: str
    external_sku_id: str
    item_name: str
    quantity: int
    unit_price: Decimal
    discount_amount: Decimal = Decimal("0")
    item_status: str = ""
    seller_sku: str = ""
    platform_sku_id: str = ""


@dataclass(frozen=True, slots=True)
class ParsedOrder:
    """归一化后的订单头 + 明细。"""

    external_order_no: str
    platform_status: str
    status: NormalizedOrderStatus
    currency: str
    total_amount: Decimal
    buyer_name: str
    placed_at: datetime | None
    lines: tuple[ParsedOrderLine, ...] = field(default_factory=tuple)
    abnormal_reason: str = ""

    @property
    def deducts(self) -> bool:
        return deducts_stock(self.status)


def _text(value: object, limit: int = 0) -> str:
    result = "" if value is None else str(value).strip()
    return result[:limit] if limit and len(result) > limit else result


def _merge_lines(lines: list[ParsedOrderLine]) -> tuple[ParsedOrderLine, ...]:
    """同一平台商品 + 规格出现多行时合并数量，保证明细防重键唯一。"""
    merged: dict[tuple[str, str], ParsedOrderLine] = {}
    for line in lines:
        key = (line.external_item_id, line.platform_sku_id or line.external_sku_id)
        existing = merged.get(key)
        if existing is None:
            merged[key] = line
            continue
        merged[key] = ParsedOrderLine(
            external_item_id=line.external_item_id,
            external_sku_id=line.external_sku_id,
            item_name=existing.item_name or line.item_name,
            quantity=existing.quantity + line.quantity,
            unit_price=line.unit_price if line.unit_price else existing.unit_price,
            discount_amount=existing.discount_amount + line.discount_amount,
            item_status=line.item_status or existing.item_status,
            seller_sku=existing.seller_sku or line.seller_sku,
            platform_sku_id=existing.platform_sku_id if existing.platform_sku_id == line.platform_sku_id else "",
        )
    return tuple(merged.values())


def _parse_shopee_lines(payload: dict) -> list[ParsedOrderLine]:
    raw_items = payload.get("item_list") or []
    if not isinstance(raw_items, list):
        return []
    lines: list[ParsedOrderLine] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        external_item_id = _text(item.get("item_id"), 64)
        external_sku_id = _text(item.get("model_sku") or item.get("item_sku"), 128)
        quantity = item.get("model_quantity_purchased", item.get("quantity", 1))
        try:
            quantity_value = max(1, int(quantity))
        except (TypeError, ValueError):
            quantity_value = 1
        # 平台给的是原价与折后价，折扣按「(原价 - 折后价) × 数量」计算，全程 Decimal
        original_price = to_decimal(item.get("model_original_price"))
        discounted_price = to_decimal(item.get("model_discounted_price"))
        unit_price = discounted_price if discounted_price > 0 else original_price
        unit_discount = max(Decimal("0"), original_price - unit_price)
        lines.append(
            ParsedOrderLine(
                external_item_id=external_item_id,
                external_sku_id=external_sku_id,
                item_name=_text(item.get("item_name"), 255),
                quantity=quantity_value,
                unit_price=unit_price,
                discount_amount=unit_discount * quantity_value,
                item_status=_text(item.get("item_status"), 32),
                seller_sku=external_sku_id,
                platform_sku_id=_text(item.get("model_id"), 64),
            )
        )
    return lines


def parse_shopee_order(payload: dict) -> ParsedOrder:
    """把 Shopee 订单明细 JSON 归一化为 ``ParsedOrder``。"""
    if not isinstance(payload, dict):
        raise ValueError("平台订单数据必须是 JSON 对象")
    external_order_no = _text(payload.get("order_sn"), 64)
    if not external_order_no:
        raise ValueError("平台订单数据缺少 order_sn")
    platform_status = _text(payload.get("order_status"), 64)
    status = normalize_platform_status("shopee", platform_status)
    reason = "" if status != NormalizedOrderStatus.UNKNOWN else f"未知平台订单状态: {platform_status or '(空)'}"
    buyer = payload.get("buyer_username") or payload.get("buyer_user_name") or ""
    lines = _merge_lines(_parse_shopee_lines(payload))
    return ParsedOrder(
        external_order_no=external_order_no,
        platform_status=platform_status,
        status=status,
        currency=_text(payload.get("currency"), 8).upper(),
        total_amount=to_decimal(payload.get("total_amount")),
        buyer_name=_text(buyer, 128),
        placed_at=parse_epoch(payload.get("create_time")),
        lines=lines,
        abnormal_reason=reason,
    )


def _parse_tiktok_lines(payload: dict) -> list[ParsedOrderLine]:
    raw_items = payload.get("line_items") or payload.get("items") or []
    if not isinstance(raw_items, list):
        return []
    lines: list[ParsedOrderLine] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        external_item_id = _text(
            item.get("product_id") or item.get("item_id") or item.get("sku_id"), 64
        )
        external_sku_id = _text(
            item.get("seller_sku") or item.get("sku_id") or item.get("id"), 128
        )
        quantity = item.get("quantity", 1)
        try:
            quantity_value = max(1, int(quantity))
        except (TypeError, ValueError):
            quantity_value = 1
        sale_price = to_decimal(item.get("sale_price"))
        original_price = to_decimal(item.get("original_price"))
        unit_price = sale_price if sale_price > 0 else original_price
        unit_discount = max(Decimal("0"), original_price - unit_price)
        lines.append(
            ParsedOrderLine(
                external_item_id=external_item_id,
                external_sku_id=external_sku_id,
                item_name=_text(item.get("product_name"), 255),
                quantity=quantity_value,
                unit_price=unit_price,
                discount_amount=unit_discount * quantity_value,
                item_status=_text(item.get("display_status"), 32),
                seller_sku=_text(item.get("seller_sku"), 128),
                platform_sku_id=_text(item.get("sku_id"), 64),
            )
        )
    return lines


def parse_tiktok_order(payload: dict) -> ParsedOrder:
    """把 TikTok Shop 订单 JSON 归一化为 ``ParsedOrder``。"""
    if not isinstance(payload, dict):
        raise ValueError("平台订单数据必须是 JSON 对象")
    external_order_no = _text(payload.get("id") or payload.get("order_id") or payload.get("order_sn"), 64)
    if not external_order_no:
        raise ValueError("平台订单数据缺少订单号")
    platform_status = _text(payload.get("status") or payload.get("order_status"), 64)
    status = normalize_platform_status("tiktok", platform_status)
    reason = (
        "" if status != NormalizedOrderStatus.UNKNOWN
        else f"未知平台订单状态: {platform_status or '(空)'}"
    )
    payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else {}
    recipient = payload.get("recipient_address") if isinstance(payload.get("recipient_address"), dict) else {}
    lines = _merge_lines(_parse_tiktok_lines(payload))
    total = to_decimal(payment.get("total_amount") or payload.get("total_amount"))
    if total <= 0 and lines:
        total = sum((line.unit_price * line.quantity for line in lines), Decimal("0"))
    return ParsedOrder(
        external_order_no=external_order_no,
        platform_status=platform_status,
        status=status,
        currency=_text(payload.get("currency") or payment.get("currency"), 8).upper(),
        total_amount=total,
        buyer_name=_text(recipient.get("name") or payload.get("buyer_name"), 128),
        placed_at=parse_epoch(payload.get("create_time")),
        lines=lines,
        abnormal_reason=reason,
    )


PARSERS = {"shopee": parse_shopee_order, "tiktok": parse_tiktok_order}


def parse_order_payload(channel: str, payload: dict) -> ParsedOrder:
    """渠道差异入口：按渠道选择解析器；未接入渠道抛 ``ValueError``。"""
    parser = PARSERS.get((channel or "").strip().lower())
    if parser is None:
        raise ValueError(f"未接入的渠道: {channel}")
    return parser(payload)
