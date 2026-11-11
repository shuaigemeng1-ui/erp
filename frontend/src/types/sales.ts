import type { Decimal, EntityId } from './common'

export interface OrderItem {
  id: EntityId
  external_item_id?: string | null
  external_sku_id?: string | null
  sku_id?: EntityId | null
  sku_code?: string | null
  product_name?: string | null
  quantity: Decimal
  unit_price?: Decimal | null
  discount_amount?: Decimal | null
  item_status?: string | null
  mapping_status?: string | null
}

export interface OrderStatusHistory {
  id: EntityId
  from_status?: string | null
  to_status: string
  platform_status?: string | null
  source?: string | null
  changed_at?: string
}

export interface OrderStockEffect {
  id: EntityId
  order_item_id: EntityId
  sku_id?: EntityId | null
  sku_code?: string | null
  stock_place_id: EntityId
  applied_quantity: Decimal
  target_quantity: Decimal
  status: string
  last_task_id?: EntityId | null
}

export interface Order {
  id: EntityId
  store_id: EntityId
  store_name?: string
  channel: string
  external_order_no: string
  status: string
  platform_status?: string | null
  currency: string
  total_amount: Decimal
  buyer_name?: string | null
  placed_at?: string | null
  abnormal_reason?: string | null
  created_at?: string
}

export interface OrderDetail extends Order {
  items: OrderItem[]
  status_history: OrderStatusHistory[]
  stock_effects: OrderStockEffect[]
  raw?: Record<string, unknown> | null
}
