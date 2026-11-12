import type { RouteRecordRaw } from 'vue-router'

/** 布局外的独立页面 */
const standaloneRoutes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/auth/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/admin/login',
    name: 'platform-login',
    component: () => import('@/views/auth/PlatformLoginView.vue'),
    meta: { public: true, title: '平台管理登录' },
  },
  {
    path: '/platform/login',
    redirect: (to) => ({ name: 'platform-login', query: to.query }),
    meta: { public: true, title: '平台管理登录' },
  },
  {
    path: '/activate/:token',
    name: 'activate',
    component: () => import('@/views/auth/ActivateView.vue'),
    meta: { public: true, title: '邀请激活' },
  },
  {
    path: '/tenant-select',
    name: 'tenant-select',
    component: () => import('@/views/auth/TenantSelectView.vue'),
    meta: { title: '选择租户' },
  },
  {
    path: '/forbidden',
    name: 'forbidden',
    component: () => import('@/views/system/ForbiddenView.vue'),
    meta: { title: '无访问权限' },
  },
]

export const routes: RouteRecordRaw[] = [
  ...standaloneRoutes,
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    children: [
      {
        path: '',
        name: 'home',
        component: () => import('@/views/system/HomeRedirectView.vue'),
        meta: { tenant: true, home: true, title: '首页' },
      },
      {
        path: 'products',
        name: 'products',
        component: () => import('@/views/tenant/ProductsView.vue'),
        meta: { tenant: true, permission: 'catalog.product.read', title: '商品' },
      },
      {
        path: 'products/:productId',
        name: 'product-detail',
        component: () => import('@/views/tenant/ProductDetailView.vue'),
        meta: { tenant: true, permission: 'catalog.product.read', title: 'SPU 详情' },
      },
      {
        path: 'settings/products',
        name: 'product-settings',
        component: () => import('@/views/tenant/ProductSettingsView.vue'),
        meta: { tenant: true, permission: 'catalog.product.read', title: '商品设置' },
      },
      {
        path: 'inventory',
        name: 'inventory',
        component: () => import('@/views/tenant/InventoryView.vue'),
        meta: { tenant: true, permission: 'inventory.read', title: '库存' },
      },
      {
        path: 'stores',
        name: 'stores',
        component: () => import('@/views/tenant/StoresView.vue'),
        meta: { tenant: true, permission: 'store.read', title: '店铺连接' },
      },
      {
        path: 'listings',
        name: 'listings',
        component: () => import('@/views/tenant/ListingsView.vue'),
        meta: { tenant: true, permission: 'channel.listing.read', title: '平台商品映射' },
      },
      {
        path: 'orders',
        name: 'orders',
        component: () => import('@/views/tenant/OrdersView.vue'),
        meta: { tenant: true, permission: 'order.read', title: '订单' },
      },
      {
        path: 'orders/:orderId',
        name: 'order-detail',
        component: () => import('@/views/tenant/OrderDetailView.vue'),
        meta: { tenant: true, permission: 'order.read', title: '订单详情' },
      },
      {
        path: 'sync-runs',
        name: 'sync-runs',
        component: () => import('@/views/tenant/SyncRunsView.vue'),
        meta: { tenant: true, permission: 'task.read', title: '任务记录' },
      },
      {
        path: 'members',
        name: 'members',
        component: () => import('@/views/tenant/MembersView.vue'),
        meta: { tenant: true, permission: 'member.read', title: '成员' },
      },
      {
        path: 'roles',
        name: 'roles',
        component: () => import('@/views/tenant/RolesView.vue'),
        meta: { tenant: true, permission: 'role.manage', title: '角色权限' },
      },
      {
        path: 'audit-logs',
        name: 'audit-logs',
        component: () => import('@/views/tenant/AuditLogsView.vue'),
        meta: { tenant: true, permission: 'audit.read', title: '审计日志' },
      },
      {
        path: 'platform/tenants',
        name: 'platform-tenants',
        component: () => import('@/views/platform/PlatformTenantsView.vue'),
        meta: { platform: true, title: '租户管理' },
      },
      {
        path: 'platform/plans',
        redirect: { name: 'platform-system-config', query: { tab: 'plans' } },
        meta: { platform: true, title: '系统配置' },
      },
      {
        path: 'platform/system-config',
        name: 'platform-system-config',
        component: () => import('@/views/platform/PlatformSystemConfigView.vue'),
        meta: { platform: true, title: '系统配置' },
      },
      {
        path: 'platform/channel-apps',
        redirect: { name: 'platform-system-config', query: { tab: 'channels' } },
        meta: { platform: true, title: '系统配置' },
      },
      {
        path: 'platform/system-settings',
        redirect: { name: 'platform-system-config', query: { tab: 'storage' } },
        meta: { platform: true, title: '系统配置' },
      },
      {
        path: 'platform/logs',
        name: 'platform-logs',
        component: () => import('@/views/platform/PlatformLogsView.vue'),
        meta: { platform: true, title: '日志' },
      },
      {
        path: 'platform/audit-logs',
        redirect: { name: 'platform-logs', query: { tab: 'audit' } },
        meta: { platform: true, title: '日志' },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/system/NotFoundView.vue'),
    meta: { public: true, title: '页面不存在' },
  },
]
