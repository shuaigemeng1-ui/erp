import { useAuthStore } from '@/stores/auth'
import { useTenantStore } from '@/stores/tenant'

/**
 * 首页按有效权限决定，避免打开无权限页面。
 * 平台管理员且无租户权限时进入平台管理。
 */
export function resolveHomePath(): string {
  const auth = useAuthStore()
  const tenant = useTenantStore()

  if (tenant.isReady) {
    const candidates: Array<[string, string]> = [
      ['order.read', '/orders'],
      ['catalog.product.read', '/products'],
      ['inventory.read', '/inventory'],
      ['store.read', '/stores'],
      ['channel.listing.read', '/listings'],
      ['task.read', '/sync-runs'],
      ['member.read', '/members'],
      ['audit.read', '/audit-logs'],
      ['role.manage', '/roles'],
    ]
    for (const [permission, path] of candidates) {
      if (tenant.has(permission)) return path
    }
  }
  if (auth.isPlatformAdmin) return '/platform/tenants'
  return '/tenant-select'
}
