import type { EntityId } from './common'
import type { TenantLifecycleStatus } from './tenant'

export interface MeProfile {
  id: EntityId
  email: string
  display_name: string
  status: string
  is_platform_admin: boolean
  locale: string
}

export interface LoginPayload {
  email: string
  password: string
}

export interface LoginResult {
  user: MeProfile
  tenants: import('./tenant').MeTenant[]
}

export interface CsrfResult {
  csrf_token: string
}

export interface InvitationInfo {
  tenant_name: string
  email: string
  status: string
  expires_at: string | null
}

export interface InvitationAcceptPayload {
  token: string
  password: string
  display_name: string
}

export interface PasswordPayload {
  current_password: string
  new_password: string
}

export type TenantStatus = TenantLifecycleStatus
