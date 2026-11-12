export type StatusTagType = 'success' | 'warning' | 'info' | 'primary' | 'danger'

export interface StatusMeta {
  label: string
  type: StatusTagType
}

/** 通用状态 → 中文/标签色；未命中的状态直接展示原值。 */
export const statusMeta: Record<string, StatusMeta> = {
  active: { label: '正常', type: 'success' },
  invited: { label: '已邀请', type: 'warning' },
  disabled: { label: '已停用', type: 'info' },
  provisioning: { label: '开通中', type: 'warning' },
  suspended: { label: '已暂停', type: 'info' },
  provision_failed: { label: '开通失败', type: 'danger' },
  pending: { label: '等待中', type: 'warning' },
  running: { label: '执行中', type: 'primary' },
  processing: { label: '处理中', type: 'primary' },
  succeeded: { label: '成功', type: 'success' },
  success: { label: '成功', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  partial: { label: '部分成功', type: 'warning' },
  pending_authorization: { label: '待授权', type: 'warning' },
  connected: { label: '已连接', type: 'success' },
  reauth_required: { label: '需重新授权', type: 'danger' },
  disconnected: { label: '已断开', type: 'info' },
  mapped: { label: '已映射', type: 'success' },
  ambiguous: { label: '待人工确认', type: 'warning' },
  missing: { label: '未映射', type: 'info' },
  archived: { label: '已归档', type: 'info' },
  inactive: { label: '未上架', type: 'info' },
  mapping_pending: { label: '待映射', type: 'warning' },
  applied: { label: '已扣减', type: 'success' },
  skipped: { label: '已跳过', type: 'info' },
  dispatched: { label: '已投递', type: 'success' },
  cancelled: { label: '已取消', type: 'info' },
  canceled: { label: '已取消', type: 'info' },
  paid: { label: '已付款', type: 'primary' },
  unpaid: { label: '未付款', type: 'warning' },
  shipped: { label: '已发货', type: 'success' },
  completed: { label: '已完成', type: 'success' },
  ready_to_ship: { label: '待发货', type: 'warning' },
  to_confirm: { label: '待确认', type: 'warning' },
  abnormal: { label: '异常', type: 'danger' },
  received: { label: '已接收', type: 'primary' },
  ignored: { label: '已忽略', type: 'info' },
  bound: { label: '已绑定', type: 'success' },
  released: { label: '已释放', type: 'info' },
  auto: { label: '自动匹配', type: 'success' },
  manual: { label: '手动映射', type: 'primary' },
}

export const jobTypeLabels: Record<string, string> = {
  order_sync: '订单同步',
  listing_sync: '平台商品同步',
  stock_push: '库存推送',
  token_refresh: '凭据刷新',
  shopee_order_sync: 'Shopee 订单同步',
  shopee_listing_sync: 'Shopee 商品同步',
  shopee_stock_push: 'Shopee 库存推送',
}

export const triggeredByLabels: Record<string, string> = {
  manual: '手动',
  schedule: '定时',
  webhook: '平台回调',
  system: '系统',
}

export const movementTypeLabels: Record<string, string> = {
  manual_adjust: '人工调整',
  order_deduct: '订单扣减',
  order_restore: '订单恢复',
  initial: '初始入库',
  stock_push: '库存推送',
}

export const mappingStatusLabels: Record<string, string> = {
  mapped: '已映射',
  pending: '待处理',
  ambiguous: '待人工确认',
  missing: '未映射',
}

export const scopeTypeLabels: Record<string, string> = {
  all: '当前租户全部',
  stores: '指定店铺',
  stock_places: '指定库存地点',
}

export const environmentLabels: Record<string, string> = {
  production: '生产环境',
  test: '测试环境',
}

export function labelOf(dictionary: Record<string, string>, value?: string | null): string {
  if (!value) return '-'
  return dictionary[value] ?? value
}

export function metaOf(status?: string | null): StatusMeta {
  if (!status) return { label: '-', type: 'info' }
  return statusMeta[status] ?? { label: status, type: 'info' }
}
