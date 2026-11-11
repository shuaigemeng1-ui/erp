import type { Directive } from 'vue'
import { useTenantStore } from '@/stores/tenant'

type PermissionValue = string | readonly string[] | undefined

/**
 * v-permission="'member.invite'" 或 v-permission="['order.read','order.sync']"
 * 无权限时移除元素（菜单/按钮按有效权限显示），后端仍独立授权。
 */
function isAllowed(value: PermissionValue): boolean {
  const tenant = useTenantStore()
  if (!value) return true
  if (Array.isArray(value)) {
    return value.length === 0 ? true : tenant.hasAny(value)
  }
  return tenant.has(value as string)
}

export const permissionDirective: Directive<HTMLElement, PermissionValue> = {
  mounted(el, binding) {
    if (!isAllowed(binding.value)) el.parentNode?.removeChild(el)
  },
  updated(el, binding) {
    if (!isAllowed(binding.value)) el.parentNode?.removeChild(el)
  },
}

declare module 'vue' {
  interface GlobalDirectives {
    vPermission: typeof permissionDirective
  }
}
