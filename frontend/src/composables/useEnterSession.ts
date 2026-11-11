import { ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useTenantStore } from '@/stores/tenant'

/**
 * 登录/激活成功后的统一入口：
 * 单租户直接进入，多租户去选择页，无租户的平台管理员进入平台管理。
 */
export function useEnterSession() {
  const auth = useAuthStore()
  const tenant = useTenantStore()
  const router = useRouter()

  async function enterApp(): Promise<void> {
    const activeTenants = auth.tenants.filter((item) => item.status === 'active')
    if (activeTenants.length === 1) {
      const success = await tenant.switchTenant(activeTenants[0].id)
      if (!success) ElMessage.warning(tenant.error || '租户权限加载失败，请重试')
      await router.replace('/')
      return
    }
    if (activeTenants.length === 0) {
      await router.replace(auth.isPlatformAdmin ? '/platform/tenants' : '/tenant-select')
      return
    }
    await router.replace('/tenant-select')
  }

  return { enterApp }
}
