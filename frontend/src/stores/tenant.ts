import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { resetRequestScope } from '@/api/http'
import { tenantApi } from '@/api/tenant'
import { useAuthStore } from './auth'
import type { BootstrapData, MeTenant } from '@/types'

/**
 * 当前租户只保存在本标签页的 sessionStorage（不同标签页可用不同租户），
 * 不放在模块级单例里。
 */
const ACTIVE_TENANT_STORAGE_KEY = 'ventasync.active_tenant_id'

function readStoredTenantId(): string {
  try {
    return sessionStorage.getItem(ACTIVE_TENANT_STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

function writeStoredTenantId(tenantId: string): void {
  try {
    if (tenantId) sessionStorage.setItem(ACTIVE_TENANT_STORAGE_KEY, tenantId)
    else sessionStorage.removeItem(ACTIVE_TENANT_STORAGE_KEY)
  } catch {
    // 隐私模式下 sessionStorage 不可用时仅保留内存状态
  }
}

export const useTenantStore = defineStore('tenant', () => {
  const auth = useAuthStore()

  const activeTenantId = ref<string>(readStoredTenantId())
  const bootstrap = ref<BootstrapData | null>(null)
  const loading = ref(false)
  const error = ref('')
  /** 租户世代：切换租户时递增；页面按此重建，迟到响应按此丢弃。 */
  const epoch = ref(0)

  const availableTenants = computed<MeTenant[]>(() => auth.tenants)
  const activeTenant = computed<MeTenant | null>(
    () => auth.tenants.find((tenant) => tenant.id === activeTenantId.value) ?? null,
  )
  const tenantName = computed(() => activeTenant.value?.name ?? bootstrap.value?.tenant.name ?? '')
  const permissionVersion = computed(() => bootstrap.value?.permission_version ?? 0)
  const permissionCodes = computed<string[]>(() => bootstrap.value?.permissions ?? [])
  const permissionSet = computed<Set<string>>(() => new Set(permissionCodes.value))
  const featureFlags = computed<Record<string, boolean>>(() => bootstrap.value?.feature_flags ?? {})
  const isReady = computed(() => Boolean(activeTenantId.value && bootstrap.value))
  const memberId = computed(() => bootstrap.value?.member.id ?? '')
  const roles = computed<string[]>(() => bootstrap.value?.member.roles ?? [])

  function has(permission: string): boolean {
    return permissionSet.value.has(permission)
  }

  function hasAny(permissions: readonly string[]): boolean {
    return permissions.some((permission) => permissionSet.value.has(permission))
  }

  function featureEnabled(featureCode: string): boolean {
    return featureFlags.value[featureCode] !== false
  }

  /** 所有租户接口都必须显式带上该 ID。 */
  function requireTenantId(): string {
    if (!activeTenantId.value) throw new Error('当前未选择租户')
    return activeTenantId.value
  }

  function setActiveTenant(tenantId: string): void {
    activeTenantId.value = tenantId
    writeStoredTenantId(tenantId)
  }

  function clearTenantState(): void {
    bootstrap.value = null
    error.value = ''
    loading.value = false
  }

  async function loadBootstrap(tenantId: string = activeTenantId.value): Promise<boolean> {
    if (!tenantId) return false
    const requestEpoch = epoch.value
    loading.value = true
    error.value = ''
    try {
      const data = await tenantApi.bootstrap(tenantId)
      // 迟到的旧租户响应直接丢弃
      if (requestEpoch !== epoch.value || activeTenantId.value !== tenantId) return false
      bootstrap.value = data
      return true
    } catch (cause) {
      if (requestEpoch !== epoch.value) return false
      bootstrap.value = null
      error.value = cause instanceof Error ? cause.message : '加载租户权限失败'
      return false
    } finally {
      if (requestEpoch === epoch.value) loading.value = false
    }
  }

  /**
   * 切换租户：取消旧请求 → 清空旧数据与勾选（epoch 递增触发页面重建）→ 重新加载 bootstrap 权限。
   */
  async function switchTenant(tenantId: string): Promise<boolean> {
    if (!tenantId) return false
    if (tenantId === activeTenantId.value && bootstrap.value) return true
    resetRequestScope()
    epoch.value += 1
    clearTenantState()
    setActiveTenant(tenantId)
    return loadBootstrap(tenantId)
  }

  async function ensureReady(): Promise<boolean> {
    if (!activeTenantId.value) activeTenantId.value = readStoredTenantId()
    if (!activeTenantId.value) return false
    if (bootstrap.value && bootstrap.value.tenant.id === activeTenantId.value) return true
    return loadBootstrap()
  }

  async function reloadBootstrap(): Promise<boolean> {
    resetRequestScope()
    epoch.value += 1
    clearTenantState()
    return loadBootstrap()
  }

  function clear(): void {
    resetRequestScope()
    epoch.value += 1
    activeTenantId.value = ''
    writeStoredTenantId('')
    clearTenantState()
  }

  /** 退出登录时清空；登录后由页面显式选择租户。 */
  function resetForLogout(): void {
    clear()
  }

  return {
    activeTenantId,
    bootstrap,
    loading,
    error,
    epoch,
    availableTenants,
    activeTenant,
    tenantName,
    permissionCodes,
    permissionSet,
    permissionVersion,
    featureFlags,
    isReady,
    memberId,
    roles,
    has,
    hasAny,
    featureEnabled,
    requireTenantId,
    loadBootstrap,
    switchTenant,
    ensureReady,
    reloadBootstrap,
    clear,
    resetForLogout,
  }
})
