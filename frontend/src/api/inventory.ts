import { http, requestPage } from './http'
import { tenantPath } from './paths'
import type {
  AdjustmentPayload,
  AdjustmentResult,
  EntityId,
  InventoryBalance,
  PageParams,
  PagedData,
  QueryParams,
  StockMovement,
  StockPlace,
} from '@/types'

export interface InventoryQuery extends PageParams {
  sku_id?: EntityId
  stock_place_id?: EntityId
  q?: string
}

export interface MovementQuery extends PageParams {
  sku_id?: EntityId
  stock_place_id?: EntityId
}

function toList<T>(raw: T[] | { items?: T[] } | null): T[] {
  if (!raw) return []
  return Array.isArray(raw) ? raw : (raw.items ?? [])
}

export const inventoryApi = {
  balances(tenantId: EntityId, query: InventoryQuery = {}): Promise<PagedData<InventoryBalance>> {
    return requestPage<InventoryBalance>(tenantPath(tenantId, '/inventory'), query as QueryParams)
  },

  adjust(tenantId: EntityId, payload: AdjustmentPayload): Promise<AdjustmentResult> {
    return http.post<AdjustmentResult>(tenantPath(tenantId, '/inventory/adjustments'), payload)
  },

  movements(tenantId: EntityId, query: MovementQuery = {}): Promise<PagedData<StockMovement>> {
    return requestPage<StockMovement>(tenantPath(tenantId, '/inventory/movements'), query as QueryParams)
  },

  async stockPlaces(tenantId: EntityId): Promise<StockPlace[]> {
    const raw = await http.get<StockPlace[] | { items?: StockPlace[] }>(
      tenantPath(tenantId, '/inventory/stock-places'),
    )
    return toList(raw)
  },
}
