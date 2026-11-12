import { useAuthStore } from '@/stores/auth'
import { useTenantStore } from '@/stores/tenant'

/**
 * 首页按登录端与有效权限决定：
 * - 平台管理会话：固定进平台控制台
 * - 租户会话：按租户权限选第一个可访问页面；未选租户则去租户选择页
 * 平台管理员用租户入口登录时，同样只走租户 ERP 首页。
 */
export function resolveHomePath(): string {
  const auth = useAuthStore()
  const tenant = useTenantStore()

  if (auth.isPlatformMode) return '/platform/tenants'

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
  return '/tenant-select'
}
