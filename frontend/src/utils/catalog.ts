import type { Product, ProductUpdatePayload, ValueSummary, CatalogOptions } from '@/types/catalog'
import { formatDateTime, formatDecimal } from './format'

export const salesStatuses = [
  { value: 'on_sale', label: '在售' }, { value: 'discontinued', label: '停售' },
  { value: 'trial', label: '试卖' }, { value: 'clearance', label: '清仓' },
]
export const spuStatuses = [...salesStatuses, { value: 'partial_clearance', label: '部分清仓' }, { value: 'partial_discontinued', label: '部分停售' }]
export const salesTypes = [
  { value: 'normal', label: '普通销售' }, { value: 'variation', label: '变参销售' },
  { value: 'bundle', label: '捆绑销售' }, { value: 'assembly', label: '组装产品' },
]
export function statusLabel(status?: string): string { return spuStatuses.find((s) => s.value === status)?.label ?? '—' }
export function typeLabel(type?: string): string { return salesTypes.find((s) => s.value === type)?.label ?? '—' }
export function catalogDateTime(value?: string): string {
  if (!value) return '—'
  const normalized = /T/.test(value) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? `${value}Z` : value
  return formatDateTime(normalized)
}
export function summaryText(summary?: ValueSummary, weight = false): string {
  if (!summary || summary.state === 'missing') return '未设置'
  if (summary.state === 'partial_missing') return '部分未设置'
  if (summary.state === 'multiple') return `${weight ? '含多个重量' : '含多个价格'}${summary.has_missing ? '（部分未设置）' : ''}`
  return `${formatDecimal(summary.value)}${weight ? ' g' : ''}`
}
export const emptyOptions = (): CatalogOptions => ({ can_read_cost: false, suppliers: [], members: [], brand: [], category: [], tag: [], packaging: [] })
export const spuTextFields = ['name_en', 'brand', 'category', 'hs_code', 'material_zh', 'material_en', 'usage', 'packaging_name', 'remark', 'features'] as const
export const spuIdFields = ['preferred_supplier_id', 'purchaser_id', 'developer_id', 'inquiry_buyer_id'] as const
export const spuDimensionFields = ['length_cm', 'width_cm', 'height_cm', 'package_length_cm', 'package_width_cm', 'package_height_cm'] as const
export type SpuForm = { code: string; name: string; sales_type: string; feature_tags: string[]; images: string[] } & Record<typeof spuTextFields[number] | typeof spuIdFields[number] | typeof spuDimensionFields[number], string>
export function spuForm(product?: Product): SpuForm {
  const form = { code: product?.code ?? '', name: product?.name ?? '', sales_type: product?.sales_type ?? 'normal', feature_tags: [...(product?.feature_tags ?? [])], images: (product?.images ?? []).filter((i) => !i.sku_id).map((i) => i.url) } as SpuForm
  for (const field of [...spuTextFields, ...spuIdFields, ...spuDimensionFields]) form[field] = product?.[field] == null ? '' : String(product[field])
  return form
}
export function decimalValue(value: string, label: string): string | null {
  const cleaned = value.trim()
  if (!cleaned) return null
  if (!/^\d+(\.\d+)?$/.test(cleaned)) throw new Error(`${label}请输入非负数`)
  return cleaned
}
export function spuPayload(form: SpuForm): ProductUpdatePayload {
  if (!form.code.trim() || !form.name.trim()) throw new Error('请填写 SPU 编号和商品中文名称')
  const result: Record<string, unknown> = { code: form.code.trim(), name: form.name.trim(), sales_type: form.sales_type, feature_tags: form.feature_tags, images: form.images }
  for (const field of [...spuTextFields, ...spuIdFields]) result[field] = form[field].trim() || null
  for (const field of spuDimensionFields) result[field] = decimalValue(form[field], '尺寸')
  return result as ProductUpdatePayload
}
