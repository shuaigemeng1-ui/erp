import { http, requestPage } from './http'
import { segment, tenantPath } from './paths'
import type {
  AcceptedTask,
  EntityId,
  Order,
  OrderDetail,
  OrderItem,
  OrderStatusHistory,
  OrderStockEffect,
  PageParams,
  PagedData,
  QueryParams,
} from '@/types'

export interface OrderQuery extends PageParams {
  status?: string
  store_id?: EntityId
  q?: string
  date_from?: string
  date_to?: string
}

interface OrderDetailResponse {
  order?: Order
  items?: OrderItem[]
  status_history?: OrderStatusHistory[]
  stock_effects?: OrderStockEffect[]
  raw?: Record<string, unknown> | null
}

/** 兼容后端「扁平」与「{order, items, ...}」两种详情结构。 */
function normalizeOrderDetail(raw: OrderDetail | OrderDetailResponse): OrderDetail {
  const wrapped = raw as OrderDetailResponse
  if (wrapped.order) {
    return {
      ...wrapped.order,
      items: wrapped.items ?? [],
      status_history: wrapped.status_history ?? [],
      stock_effects: wrapped.stock_effects ?? [],
      raw: wrapped.raw ?? null,
    }
  }
  const flat = raw as OrderDetail
  return {
    ...flat,
    items: flat.items ?? [],
    status_history: flat.status_history ?? [],
    stock_effects: flat.stock_effects ?? [],
  }
}

export const salesApi = {
  orders(tenantId: EntityId, query: OrderQuery = {}): Promise<PagedData<Order>> {
    return requestPage<Order>(tenantPath(tenantId, '/orders'), query as QueryParams)
  },

  async order(tenantId: EntityId, orderId: EntityId): Promise<OrderDetail> {
    const raw = await http.get<OrderDetail | OrderDetailResponse>(
      tenantPath(tenantId, `/orders/${segment(orderId)}`),
    )
    return normalizeOrderDetail(raw)
  },

  syncOrders(tenantId: EntityId, storeId?: EntityId): Promise<AcceptedTask> {
    const body = storeId ? { store_id: storeId } : {}
    return http.post<AcceptedTask>(tenantPath(tenantId, '/orders/sync'), body)
  },
}
