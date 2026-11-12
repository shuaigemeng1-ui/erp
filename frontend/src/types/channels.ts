import type { Decimal, EntityId } from './common'

export type StoreStatus =
  | 'connected'
  | 'pending_authorization'
  | 'reauth_required'
  | 'disconnected'
  | string

export interface StoreCredential {
  status?: string
  access_token_expires_at?: string | null
  refresh_token_expires_at?: string | null
}

export interface Store {
  id: EntityId
  channel: string
  store_code: string
  name: string
  external_store_id: string
  region?: string | null
  timezone?: string | null
  status: StoreStatus
  authorized_at?: string | null
  last_synced_at?: string | null
  /** 凭据只返回状态与过期时间，不返回密钥 */
  credential?: StoreCredential | null
  credential_status?: string | null
  access_token_expires_at?: string | null
}

export type MappingStatus = 'mapped' | 'pending' | 'ambiguous' | 'missing' | string

export interface Listing {
  id: EntityId
  store_id: EntityId
  store_name?: string
  external_item_id: string
  external_sku_id?: string | null
  title: string
  price?: Decimal | null
  currency?: string | null
  remote_stock?: Decimal | null
  status?: string
  link_status?: MappingStatus
  mapping_status?: MappingStatus
  mapping_source?: 'auto' | 'manual' | string | null
  product_id?: EntityId | null
  product_name?: string | null
  sku_id?: EntityId | null
  sku_code?: string | null
  synced_at?: string | null
}

export interface AuthorizeResult {
  auth_url: string
  expires_at: string
  environment?: string
  store_code?: string
  service_id?: string
  market?: string
}

export interface StoreCreatePayload {
  channel: 'tiktok' | 'shopee'
  name: string
  environment?: 'production' | 'test'
  region?: string
}

export interface ListingLinkPayload {
  product_id: EntityId
  sku_id: EntityId
}
