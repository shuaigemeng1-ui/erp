export const ORDER_STATUS_LABELS: Record<string, string> = {
  pending: '待付款',
  paid: '已付款',
  ready_to_ship: '待发货',
  shipped: '已发货',
  completed: '已完成',
  cancelled: '已取消',
  abnormal: '异常',
  unknown: '未知',
}

export const ORDER_STATUS_OPTIONS = Object.entries(ORDER_STATUS_LABELS).map(([value, label]) => ({
  value,
  label,
}))

export function orderStatusLabel(status?: string | null): string {
  if (!status) return '-'
  return ORDER_STATUS_LABELS[status] ?? status
}

export function orderStatusType(
  status?: string | null,
): 'success' | 'warning' | 'info' | 'danger' | 'primary' {
  if (status === 'completed') return 'success'
  if (status === 'paid' || status === 'ready_to_ship' || status === 'shipped') return 'primary'
  if (status === 'cancelled') return 'info'
  if (status === 'abnormal') return 'danger'
  return 'warning'
}
