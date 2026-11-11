import { clearCsrfCache, http } from './http'
import type {
  CsrfResult,
  InvitationAcceptPayload,
  InvitationInfo,
  LoginPayload,
  LoginResult,
  MeProfile,
  MeTenant,
  PasswordPayload,
} from '@/types'

function toTenantList(raw: MeTenant[] | { items?: MeTenant[] } | null): MeTenant[] {
  if (!raw) return []
  return Array.isArray(raw) ? raw : (raw.items ?? [])
}

export const authApi = {
  async csrf(): Promise<CsrfResult> {
    return http.get<CsrfResult>('/auth/csrf', undefined, { silent: true, skipUnauthorizedHandler: true })
  },

  async login(payload: LoginPayload): Promise<LoginResult> {
    const result = await http.post<LoginResult>('/auth/login', payload, { skipUnauthorizedHandler: true })
    clearCsrfCache()
    return { user: result.user, tenants: toTenantList(result.tenants) }
  },

  async logout(): Promise<void> {
    await http.post<void>('/auth/logout', undefined, { silent: true, skipUnauthorizedHandler: true })
    clearCsrfCache()
  },

  async changePassword(payload: PasswordPayload): Promise<void> {
    await http.post<void>('/auth/password', payload)
  },

  async getInvitation(token: string): Promise<InvitationInfo> {
    return http.get<InvitationInfo>(`/auth/invitations/${encodeURIComponent(token)}`, undefined, {
      silent: true,
      skipUnauthorizedHandler: true,
    })
  },

  async acceptInvitation(payload: InvitationAcceptPayload): Promise<LoginResult> {
    const result = await http.post<LoginResult>('/auth/invitations/accept', payload, {
      skipUnauthorizedHandler: true,
    })
    clearCsrfCache()
    return { user: result.user, tenants: toTenantList(result.tenants) }
  },

  async me(): Promise<MeProfile> {
    return http.get<MeProfile>('/me', undefined, { silent: true, skipUnauthorizedHandler: true })
  },

  async myTenants(): Promise<MeTenant[]> {
    const raw = await http.get<MeTenant[] | { items?: MeTenant[] }>('/me/tenants', undefined, {
      silent: true,
      skipUnauthorizedHandler: true,
    })
    return toTenantList(raw)
  },
}
