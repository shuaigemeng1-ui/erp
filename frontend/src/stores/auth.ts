import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authApi } from '@/api/auth'
import type { InvitationAcceptPayload, LoginPayload, MeProfile, MeTenant, SessionScope } from '@/types'

/**
 * 登录态：会话在 HttpOnly Cookie 中，前端只保存当前用户信息用于展示。
 * ``mode`` 区分登录入口：租户 ERP（tenant）/ 平台管理控制台（platform）。
 * 不把用户/租户写入 localStorage。
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<MeProfile | null>(null)
  const tenants = ref<MeTenant[]>([])
  const loaded = ref(false)
  const loading = ref(false)
  const mode = ref<SessionScope>('tenant')
  let inflight: Promise<boolean> | null = null

  const isAuthenticated = computed(() => user.value !== null)
  const isPlatformAdmin = computed(() => user.value?.is_platform_admin === true)
  const isPlatformMode = computed(() => mode.value === 'platform')
  const displayName = computed(() => user.value?.display_name || user.value?.email || '未登录')
  const tenantCount = computed(() => tenants.value.length)

  function applySession(
    nextUser: MeProfile,
    nextTenants: MeTenant[],
    nextMode?: SessionScope,
  ): void {
    user.value = nextUser
    tenants.value = nextTenants
    mode.value = nextMode ?? nextUser.session_scope ?? 'tenant'
    loaded.value = true
  }

  function clearSession(): void {
    user.value = null
    tenants.value = []
    mode.value = 'tenant'
    loaded.value = false
    inflight = null
    loading.value = false
  }

  async function refreshTenants(): Promise<void> {
    try {
      tenants.value = await authApi.myTenants()
    } catch {
      // 租户列表失败不阻断主流程，页面会提示无可用租户
    }
  }

  /** 探测登录状态；401 时静默返回 false（由路由守卫决定跳转）。 */
  async function loadMe(): Promise<boolean> {
    if (user.value) return true
    if (inflight) return inflight
    loading.value = true
    inflight = (async (): Promise<boolean> => {
      try {
        const me = await authApi.me()
        user.value = me
        mode.value = me.session_scope ?? 'tenant'
        loaded.value = true
        await refreshTenants()
        return true
      } catch {
        user.value = null
        tenants.value = []
        loaded.value = true
        return false
      } finally {
        loading.value = false
        inflight = null
      }
    })()
    return inflight
  }

  async function login(payload: LoginPayload, scope: SessionScope = 'tenant'): Promise<void> {
    const result = await authApi.login({ ...payload, scope })
    applySession(result.user, result.tenants, result.session_scope ?? scope)
    await refreshTenants()
  }

  async function acceptInvitation(payload: InvitationAcceptPayload): Promise<void> {
    const result = await authApi.acceptInvitation(payload)
    applySession(result.user, result.tenants, 'tenant')
    await refreshTenants()
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // 会话已失效也按退出处理
    } finally {
      clearSession()
    }
  }

  return {
    user,
    tenants,
    loaded,
    loading,
    isAuthenticated,
    isPlatformAdmin,
    isPlatformMode,
    mode,
    displayName,
    tenantCount,
    loadMe,
    refreshTenants,
    login,
    acceptInvitation,
    logout,
    clearSession,
  }
})
