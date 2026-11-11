import 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    /** 无需登录：登录页、邀请激活页、404 */
    public?: boolean
    /** 平台管理页，要求 is_platform_admin */
    platform?: boolean
    /** 租户业务页，要求已选择租户并通过校验 */
    tenant?: boolean
    /** 首页占位，由守卫重定向到首个有权限的页面 */
    home?: boolean
    /** 页面所需权限编码（与后端 bootstrap permissions 一致） */
    permission?: string
    /** 任一权限满足即可 */
    permissions?: string[]
    title?: string
  }
}
