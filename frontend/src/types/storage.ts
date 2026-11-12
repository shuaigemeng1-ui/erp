import type { EntityId } from './common'

export type StorageProvider = 'oss' | 'cos' | 'obs' | 's3' | 's3_compatible'
export type StorageAddressingStyle = 'auto' | 'virtual' | 'path'

export interface StorageProfile {
  id: EntityId
  name: string
  provider: StorageProvider
  endpoint: string
  bucket: string
  region: string
  public_base_url: string
  key_prefix: string
  addressing_style: StorageAddressingStyle
  enabled: boolean
  version: number
  has_access_key: boolean
  has_secret_key: boolean
}

export interface StorageProfilePayload {
  name: string
  provider: StorageProvider
  endpoint: string
  bucket: string
  region: string
  public_base_url: string
  key_prefix: string
  addressing_style: StorageAddressingStyle
  enabled: boolean
  access_key_id?: string
  secret_access_key?: string
  version?: number
}

export interface PlatformSystemSettings {
  default_storage_profile_id: EntityId | null
  version: number
  profiles: StorageProfile[]
  providers: Array<{ code: StorageProvider; name: string }>
}

export interface StorageConnectionResult {
  ok: boolean
  message: string
}
