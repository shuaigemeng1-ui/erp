import type { Decimal, EntityId } from './common'

export type SalesStatus = 'on_sale' | 'discontinued' | 'trial' | 'clearance'
export type SpuSalesStatus = SalesStatus | 'partial_clearance' | 'partial_discontinued'
export type SalesType = 'normal' | 'variation' | 'bundle' | 'assembly'
export interface ValueSummary { state: 'uniform' | 'multiple' | 'missing' | 'partial_missing'; value: Decimal | null; has_missing: boolean }
export interface SkuAlias { id: EntityId; identifier: string; is_active: boolean }
export interface ProductSku {
  id: EntityId
  product_id?: EntityId
  sku_code: string
  name: string
  specs?: Record<string, string> | null
  price: Decimal
  cost_amount?: Decimal | null
  guidance_cost?: Decimal | null
  average_cost?: Decimal | null
  weight_g?: Decimal | null
  image_url?: string | null
  aliases?: SkuAlias[]
  sales_status?: SalesStatus
  status?: string
  version?: number
}
export interface ProductImage { id?: EntityId; url: string; sort_order?: number; sku_id?: EntityId | null }
export interface MappingSummary { mapped?: number; pending?: number; ambiguous?: number; missing?: number }
export interface Product {
  id: EntityId
  code: string
  name: string
  name_en?: string | null
  brand?: string | null
  category?: string | null
  status: string
  sales_status?: SpuSalesStatus
  sales_type?: SalesType
  base_price: Decimal
  cost_amount?: Decimal | null
  cost_currency?: string | null
  preferred_supplier_id?: EntityId | null
  purchaser_id?: EntityId | null
  developer_id?: EntityId | null
  inquiry_buyer_id?: EntityId | null
  hs_code?: string | null
  material_zh?: string | null
  material_en?: string | null
  usage?: string | null
  feature_tags?: string[] | null
  packaging_name?: string | null
  length_cm?: Decimal | null
  width_cm?: Decimal | null
  height_cm?: Decimal | null
  package_length_cm?: Decimal | null
  package_width_cm?: Decimal | null
  package_height_cm?: Decimal | null
  remark?: string | null
  features?: string | null
  description?: string | null
  skus?: ProductSku[]
  sku_count?: number
  images?: ProductImage[]
  image_url?: string | null
  primary_image?: string | null
  preferred_supplier_name?: string | null
  purchaser_name?: string | null
  developer_name?: string | null
  inquiry_buyer_name?: string | null
  weight_summary?: ValueSummary
  cost_summary?: { current: ValueSummary; guidance: ValueSummary; average: ValueSummary }
  mapping_summary?: MappingSummary | null
  created_at?: string
  updated_at?: string
  version?: number
}
export interface ProductSkuPayload {
  id?: EntityId
  version?: number
  sku_code: string
  name?: string
  price?: Decimal
  cost_amount?: Decimal | null
  guidance_cost?: Decimal | null
  average_cost?: Decimal | null
  weight_g?: Decimal | null
  image_url?: string | null
  sales_status?: SalesStatus
  status?: string
  aliases?: string[]
  specs?: Record<string, string>
}
export interface ProductUpdatePayload {
  status?: string
  code?: string
  name?: string
  name_en?: string | null
  brand?: string | null
  category?: string | null
  base_price?: Decimal
  cost_amount?: Decimal | null
  cost_currency?: string
  description?: string | null
  preferred_supplier_id?: EntityId | null
  purchaser_id?: EntityId | null
  developer_id?: EntityId | null
  inquiry_buyer_id?: EntityId | null
  hs_code?: string | null
  material_zh?: string | null
  material_en?: string | null
  usage?: string | null
  feature_tags?: string[]
  packaging_name?: string | null
  length_cm?: Decimal | null
  width_cm?: Decimal | null
  height_cm?: Decimal | null
  package_length_cm?: Decimal | null
  package_width_cm?: Decimal | null
  package_height_cm?: Decimal | null
  sales_type?: SalesType
  remark?: string | null
  features?: string | null
  images?: string[]
  skus?: ProductSkuPayload[]
  version?: number
}
export interface ProductCreatePayload extends ProductUpdatePayload { code: string; name: string; skus: ProductSkuPayload[] }
export interface CatalogOption { id: EntityId; name: string; parent_id?: EntityId | null; status?: string }
export interface SupplierOption extends CatalogOption { code: string; contact_name?: string | null; contact_details?: string | null; remark?: string | null }
export type CatalogOptionKind = 'brand' | 'category' | 'tag' | 'packaging'
export interface SupplierPayload { code: string; name: string; contact_name?: string | null; contact_details?: string | null; remark?: string | null; status?: 'active' | 'disabled' }
export interface CatalogOptions { can_read_cost?: boolean; can_manage_options?: boolean; suppliers: SupplierOption[]; members: CatalogOption[]; brand: CatalogOption[]; category: CatalogOption[]; tag: CatalogOption[]; packaging: CatalogOption[] }
export interface ProductChange { entity: string; sku_id?: EntityId | null; sku_code?: string; field: string; label: string; old: unknown; new: unknown; sensitive?: boolean }
export interface ProductLog { id: EntityId; summary: string; actor_name: string; created_at: string; changes: ProductChange[] }
