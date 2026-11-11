import type { Decimal, EntityId } from './common'

export interface ProductSku {
  id: EntityId
  product_id?: EntityId
  sku_code: string
  name: string
  specs?: Record<string, string> | null
  price: Decimal
  cost_amount?: Decimal | null
  status?: string
}

export interface ProductImage {
  id?: EntityId
  url: string
  sort_order?: number
}

export interface MappingSummary {
  mapped?: number
  pending?: number
  ambiguous?: number
  missing?: number
}

export interface Product {
  id: EntityId
  code: string
  name: string
  brand?: string | null
  category?: string | null
  status: string
  base_price: Decimal
  cost_amount?: Decimal | null
  cost_currency?: string | null
  description?: string | null
  skus?: ProductSku[]
  images?: ProductImage[]
  mapping_summary?: MappingSummary | null
  created_at?: string
  updated_at?: string
}

export interface ProductSkuPayload {
  sku_code: string
  name: string
  price: Decimal
  cost_amount?: Decimal
  specs?: Record<string, string>
}

export interface ProductCreatePayload {
  code: string
  name: string
  brand?: string
  category?: string
  cost_amount?: Decimal
  base_price: Decimal
  description?: string
  skus: ProductSkuPayload[]
  images?: string[]
}

export interface ProductUpdatePayload {
  code?: string
  name?: string
  brand?: string
  category?: string
  cost_amount?: Decimal
  base_price?: Decimal
  description?: string
}
