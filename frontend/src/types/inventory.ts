import type { Decimal, EntityId } from './common'

export interface StockPlace {
  id: EntityId
  code: string
  name: string
  is_default: boolean
  status: string
}

export interface InventoryBalance {
  id: EntityId
  sku_id: EntityId
  sku_code?: string
  product_id?: EntityId
  product_name?: string
  stock_place_id: EntityId
  stock_place_name?: string
  available_quantity: Decimal
  reserved_quantity: Decimal
  version?: number
  updated_at?: string
}

export interface StockMovement {
  id: EntityId
  sku_id: EntityId
  sku_code?: string
  stock_place_id: EntityId
  stock_place_name?: string
  quantity_change: Decimal
  balance_after: Decimal
  movement_type: string
  reference_type?: string | null
  reference_id?: EntityId | null
  operator_id?: EntityId | null
  note?: string | null
  created_at?: string
}

export interface AdjustmentPayload {
  sku_id: EntityId
  stock_place_id: EntityId
  quantity_change: number
  note: string
}

export interface AdjustmentResult {
  available_quantity?: Decimal
  movement_id?: EntityId
  balance_id?: EntityId
}
