import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authApi } from '@/api/auth'
import type { LoginPayload, MeProfile, MeTenant } from '@/types'

/**
 * 登录态：会话在 HttpOnly Cookie 中，前端只保存当前用户信息用于展示。
 * 不把用户/租户写入 localStorage。
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<MeProfile | null>(null)
  const tenants = ref<MeTenant[]>([])
  const loaded = ref(false)
  const loading = ref(false)
  let inflight: Promise<boolean> | null = null

  const isAuthenticated = computed(() => user.value !== null)
  const isPlatformAdmin = computed(() => user.value?.is_platform_admin === true)
  const displayName = computed(() => user.value?.display_name || user.value?.email || '未登录')
  const tenantCount = computed(() => tenants.value.length)

  function applySession(nextUser: MeProfile, nextTenants: MeTenant[]): void {
    user.value = nextUser
    tenants.value = nextTenants
    loaded.value = true
  }

  function clearSession(): void {
    user.value = null
    tenants.value = []
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

  async function login(payload: LoginPayload): Promise<void> {
    const result = await authApi.login(payload)
    applySession(result.user, result.tenants)
    await refreshTenants()
  }

  async function acceptInvitation(payload: {
    token: string
    password: string
    display_name: string
  }): Promise<void> {
    const result = await authApi.acceptInvitation(payload)
    applySession(result.user, result.tenants)
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
