import type { EntityId } from './common'

export type EmailProvider = 'aliyun_directmail' | 'tencent_ses'

export interface EmailProviderMeta {
  code: EmailProvider
  name: string
  default_region: string
  region_label: string
}

export interface EmailProfile {
  id: EntityId
  name: string
  provider: EmailProvider
  region: string
  from_address: string
  from_name: string
  enabled: boolean
  is_default: boolean
  version: number
  has_access_key: boolean
  has_secret_key: boolean
}

export interface EmailProfilePayload {
  name: string
  provider: EmailProvider
  region: string
  from_address: string
  from_name: string
  enabled: boolean
  access_key_id?: string
  secret_access_key?: string
  version?: number
}

export interface EmailProfilesResponse {
  profiles: EmailProfile[]
  providers: EmailProviderMeta[]
}

export interface EmailTestResult {
  ok: boolean
  message: string
  provider: string
  request_id?: string
}
