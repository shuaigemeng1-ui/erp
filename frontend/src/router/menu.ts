import type { Component } from 'vue'
import {
  Box,
  Document,
  Goods,
  Key,
  Link,
  OfficeBuilding,
  SetUp,
  Shop,
  Tickets,
  Timer,
  User,
} from '@element-plus/icons-vue'

export interface MenuItem {
  path: string
  title: string
  icon: Component
  permission?: string
  permissions?: string[]
  children?: MenuItem[]
}

/** 租户内菜单：按 bootstrap 返回的有效权限过滤。 */
export const tenantMenuItems: MenuItem[] = [
  { path: '/products', title: '商品', icon: Goods, permission: 'catalog.product.read' },
  { path: '/inventory', title: '库存', icon: Box, permission: 'inventory.read' },
  { path: '/stores', title: '店铺连接', icon: Shop, permission: 'store.read' },
  { path: '/listings', title: '平台商品映射', icon: Link, permission: 'channel.listing.read' },
  { path: '/orders', title: '订单', icon: Tickets, permission: 'order.read' },
  { path: '/sync-runs', title: '任务记录', icon: Timer, permission: 'task.read' },
  {
    path: '/settings', title: '基础设置', icon: SetUp,
    children: [{ path: '/settings/products', title: '商品设置', icon: Goods, permission: 'catalog.product.read' }],
  },
  { path: '/members', title: '成员', icon: User, permission: 'member.read' },
  { path: '/roles', title: '角色权限', icon: Key, permission: 'role.manage' },
  { path: '/audit-logs', title: '操作日志', icon: Document, permission: 'audit.read' },
]

/** 平台管理菜单：仅平台管理员可见（后端独立校验）。 */
export const platformMenuItems: MenuItem[] = [
  { path: '/platform/tenants', title: '租户管理', icon: OfficeBuilding },
  { path: '/platform/system-config', title: '系统配置', icon: SetUp },
  { path: '/platform/logs', title: '日志', icon: Document },
]
