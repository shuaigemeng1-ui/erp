import { http, requestPage } from './http'
import { segment, tenantPath } from './paths'
import type {
  AcceptedTask,
  AuthorizeResult,
  EntityId,
  Listing,
  ListingLinkPayload,
  PageParams,
  PagedData,
  QueryParams,
  Store,
} from '@/types'

export interface ListingQuery extends PageParams {
  store_id?: EntityId
  mapping_status?: string
  q?: string
}

function toList<T>(raw: T[] | { items?: T[] } | null): T[] {
  if (!raw) return []
  return Array.isArray(raw) ? raw : (raw.items ?? [])
}

export const channelsApi = {
  async stores(tenantId: EntityId): Promise<Store[]> {
    const raw = await http.get<Store[] | { items?: Store[] }>(tenantPath(tenantId, '/stores'))
    return toList(raw)
  },

  authorizeShopee(tenantId: EntityId): Promise<AuthorizeResult> {
    return http.post<AuthorizeResult>(tenantPath(tenantId, '/stores/shopee/authorize'))
  },

  disconnectStore(tenantId: EntityId, storeId: EntityId): Promise<void> {
    return http.post<void>(tenantPath(tenantId, `/stores/${segment(storeId)}/disconnect`))
  },

  listings(tenantId: EntityId, query: ListingQuery = {}): Promise<PagedData<Listing>> {
    return requestPage<Listing>(tenantPath(tenantId, '/listings'), query as QueryParams)
  },

  syncListings(tenantId: EntityId, storeId: EntityId): Promise<AcceptedTask> {
    return http.post<AcceptedTask>(tenantPath(tenantId, '/listings/sync'), { store_id: storeId })
  },

  linkListing(tenantId: EntityId, listingId: EntityId, payload: ListingLinkPayload): Promise<void> {
    return http.post<void>(tenantPath(tenantId, `/listings/${segment(listingId)}/link`), payload)
  },

  unlinkListing(tenantId: EntityId, listingId: EntityId): Promise<void> {
    return http.post<void>(tenantPath(tenantId, `/listings/${segment(listingId)}/unlink`))
  },
}
