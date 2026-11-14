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
  StoreCreatePayload,
} from '@/types'

export interface ListingQuery extends PageParams {
  store_id?: EntityId
  mapping_status?: string
  q?: string
}

export interface StoreWebhookStatus {
  store_id: string
  enabled: boolean
  last_event_at?: string | null
  callback_url: string
  subscriptions: Array<{
    event_type: string
    status: string
    callback_url: string
    last_checked_at?: string | null
    last_error?: string | null
  }>
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

  authorizeShopee(
    tenantId: EntityId,
    payload: { environment?: 'production' | 'test'; redirect_path?: string } = {},
  ): Promise<AuthorizeResult> {
    // 总是发送 JSON 对象：旧后端要求请求体，新后端 body 可选，保持双向兼容
    return http.post<AuthorizeResult>(tenantPath(tenantId, '/stores/shopee/authorize'), payload)
  },

  createStore(tenantId: EntityId, payload: StoreCreatePayload): Promise<Store> {
    return http.post<Store>(tenantPath(tenantId, '/stores'), payload)
  },

  authorizeTikTok(
    tenantId: EntityId,
    payload: { environment?: 'production' | 'test'; redirect_path?: string } = {},
  ): Promise<AuthorizeResult> {
    return http.post<AuthorizeResult>(tenantPath(tenantId, '/stores/tiktok/authorize'), payload)
  },

  authorizeStore(
    tenantId: EntityId,
    storeId: EntityId,
    channel: 'tiktok' | 'shopee',
  ): Promise<AuthorizeResult> {
    return http.post<AuthorizeResult>(
      tenantPath(tenantId, `/stores/${segment(storeId)}/${channel}/authorize`),
    )
  },

  revokeAuthorizationLink(tenantId: EntityId, storeId: EntityId): Promise<void> {
    return http.delete<void>(tenantPath(tenantId, `/stores/${segment(storeId)}/authorization-link`))
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

  webhookStatus(tenantId: EntityId, storeId: EntityId): Promise<StoreWebhookStatus> {
    return http.get<StoreWebhookStatus>(tenantPath(tenantId, `/stores/${segment(storeId)}/webhook/status`))
  },



}
