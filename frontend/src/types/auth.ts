import type { EntityId } from './common'
import type { TenantLifecycleStatus } from './tenant'

export type SessionScope = 'tenant' | 'platform'

export interface MeProfile {
  id: EntityId
  email: string
  display_name: string
  status: string
  is_platform_admin: boolean
  locale: string
  session_scope?: SessionScope
}

export interface LoginPayload {
  email: string
  password: string
  scope?: SessionScope
}

export interface LoginResult {
  user: MeProfile
  tenants: import('./tenant').MeTenant[]
  session_scope?: SessionScope
}

export interface CsrfResult {
  csrf_token: string
}

export interface InvitationInfo {
  tenant_name: string
  email: string
  status: string
  expires_at: string | null
  requires_login: boolean
}

export interface InvitationAcceptPayload {
  token: string
  password?: string
  display_name?: string
}

export interface PasswordPayload {
  current_password: string
  new_password: string
}

export type TenantStatus = TenantLifecycleStatus
