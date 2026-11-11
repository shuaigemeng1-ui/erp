import type { Router } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useTenantStore } from '@/stores/tenant'
import { resolveHomePath } from './home'

/**
 * 路由守卫：
 * - 公开页（登录/激活）直接放行；已登录用户访问登录页回首页
 * - 其余页面先探测 /me，401 交给登录页
 * - 平台页要求平台管理员
 * - 租户页要求已选择租户、成员状态 active、bootstrap 权限覆盖 meta.permission
 */
export function setupRouterGuards(router: Router): void {
  router.beforeEach(async (to) => {
    const auth = useAuthStore()
    const tenant = useTenantStore()

    if (to.meta.public) {
      if (to.name === 'login' && (await auth.loadMe())) {
        return { path: resolveHomePath() }
      }
      return true
    }

    const authenticated = await auth.loadMe()
    if (!authenticated) {
      return {
        name: 'login',
        query: to.fullPath && to.fullPath !== '/' ? { redirect: to.fullPath } : undefined,
      }
    }

    if (to.meta.platform) {
      if (!auth.isPlatformAdmin) return { name: 'forbidden' }
      return true
    }

    if (to.meta.tenant) {
      const ready = await tenant.ensureReady()
      if (!ready) return { name: 'tenant-select' }
      const membership = auth.tenants.find((item) => item.id === tenant.activeTenantId)
      if (!membership) return { name: 'tenant-select' }
      if (membership.status !== 'active') return { name: 'tenant-select' }
      if (to.meta.permission && !tenant.has(to.meta.permission)) return { name: 'forbidden' }
      if (to.meta.permissions && !tenant.hasAny(to.meta.permissions)) return { name: 'forbidden' }
      if (to.meta.home) return { path: resolveHomePath() }
    }

    return true
  })
}
