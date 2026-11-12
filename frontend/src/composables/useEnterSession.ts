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
    // 平台管理登录：只进入平台控制台，不混入租户 ERP
    if (auth.isPlatformMode) {
      await router.replace('/platform/tenants')
      return
    }
    const activeTenants = auth.tenants.filter((item) => item.status === 'active')
    if (activeTenants.length === 1) {
      const success = await tenant.switchTenant(activeTenants[0].id)
      if (!success) ElMessage.warning(tenant.error || '租户权限加载失败，请重试')
      await router.replace('/')
      return
    }
    // 租户入口登录后始终走 ERP 流程；平台管理员也需从平台登录入口进入控制台
    await router.replace('/tenant-select')
  }

  return { enterApp }
}
