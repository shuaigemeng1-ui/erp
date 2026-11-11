import type { EntityId } from '@/types'

/** 所有租户接口显式携带路径 tenant_id，禁止从模块级单例读取。 */
export function tenantPath(tenantId: EntityId, suffix = ''): string {
  if (!tenantId) throw new Error('缺少 tenant_id，无法发起租户请求')
  return `/tenants/${encodeURIComponent(tenantId)}${suffix}`
}

export function segment(value: string | number): string {
  return encodeURIComponent(String(value))
}
