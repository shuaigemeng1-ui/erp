import type { Router } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useTenantStore } from '@/stores/tenant'
import { resolveHomePath } from './home'

/**
 * 路由守卫：
 * - 公开页（租户登录 / 平台登录 / 邀请激活）直接放行；已登录访问登录页按当前登录端回首页
 * - 平台管理页要求：平台管理员 + 平台管理登录会话（scope=platform）
 * - 租户页要求：租户登录会话（scope=tenant）+ 已选租户 + 成员 active + 页面权限
 * - 平台会话不能进入租户 ERP，租户会话不能进入平台控制台（前后端双重隔离）
 */
export function setupRouterGuards(router: Router): void {
  router.beforeEach(async (to) => {
    const auth = useAuthStore()
    const tenant = useTenantStore()

    if (to.meta.public) {
      if (to.name === 'login' || to.name === 'platform-login') {
        const authenticated = await auth.loadMe()
        if (authenticated) {
          if (to.name === 'platform-login') {
            // 已以平台身份登录时直接进控制台；租户身份访问则允许切换到平台登录
            if (auth.isPlatformMode && auth.isPlatformAdmin) return { path: '/platform/tenants' }
            return true
          }
          return auth.isPlatformMode
            ? { path: '/platform/tenants' }
            : { path: resolveHomePath() }
        }
      }
      return true
    }

    const authenticated = await auth.loadMe()
    if (!authenticated) {
      const target = to.meta.platform ? { name: 'platform-login' } : { name: 'login' }
      const query = to.fullPath && to.fullPath !== '/' ? { redirect: to.fullPath } : undefined
      return { ...target, query }
    }

    if (to.meta.platform) {
      if (!auth.isPlatformAdmin) return { name: 'forbidden' }
      if (!auth.isPlatformMode) {
        return { name: 'platform-login', query: { redirect: to.fullPath } }
      }
      return true
    }

    // 租户选择页：平台会话不允许进入
    if (to.name === 'tenant-select') {
      if (auth.isPlatformMode) return { path: '/platform/tenants' }
      return true
    }

    if (to.meta.tenant) {
      if (auth.isPlatformMode) return { path: '/platform/tenants' }
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
