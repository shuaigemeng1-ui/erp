import { http, requestPage } from './http'
import { segment, tenantPath } from './paths'
import type {
  AuditLog,
  BootstrapData,
  EntityId,
  InvitationCreated,
  InviteMemberPayload,
  Member,
  MemberRolesPayload,
  MemberStatusPayload,
  PageParams,
  PagedData,
  PermissionGroup,
  PermissionItem,
  QueryParams,
  Role,
  RolePatchPayload,
  RolePayload,
} from '@/types'

export interface MemberQuery extends PageParams {
  q?: string
  status?: string
}

export interface AuditQuery extends PageParams {
  action?: string
  q?: string
}

interface PermissionCatalogResponse {
  groups?: PermissionGroup[]
  items?: PermissionItem[]
  permissions?: PermissionItem[]
}

function normalizePermissions(raw: PermissionItem[] | PermissionGroup[] | PermissionCatalogResponse | null): PermissionItem[] {
  if (!raw) return []
  if (Array.isArray(raw)) {
    // 可能是 [{group, items}] 或 [{code, name}]
    const first = raw[0] as unknown
    if (first && typeof first === 'object' && 'items' in (first as Record<string, unknown>)) {
      return (raw as PermissionGroup[]).flatMap((group) =>
        (group.items ?? []).map((item) => ({ ...item, group: item.group ?? group.group })),
      )
    }
    return raw as PermissionItem[]
  }
  if (Array.isArray(raw.groups)) {
    return raw.groups.flatMap((group) =>
      (group.items ?? []).map((item) => ({ ...item, group: item.group ?? group.group })),
    )
  }
  return raw.items ?? raw.permissions ?? []
}

export const tenantApi = {
  bootstrap(tenantId: EntityId): Promise<BootstrapData> {
    return http.get<BootstrapData>(tenantPath(tenantId, '/bootstrap'))
  },

  members(tenantId: EntityId, query: MemberQuery = {}): Promise<PagedData<Member>> {
    return requestPage<Member>(tenantPath(tenantId, '/members'), query as QueryParams)
  },

  inviteMember(tenantId: EntityId, payload: InviteMemberPayload): Promise<InvitationCreated> {
    return http.post<InvitationCreated>(tenantPath(tenantId, '/members/invitations'), payload)
  },

  updateMemberStatus(tenantId: EntityId, memberId: EntityId, payload: MemberStatusPayload): Promise<Member> {
    return http.patch<Member>(tenantPath(tenantId, `/members/${segment(memberId)}`), payload)
  },

  updateMemberRoles(tenantId: EntityId, memberId: EntityId, payload: MemberRolesPayload): Promise<Member> {
    return http.put<Member>(tenantPath(tenantId, `/members/${segment(memberId)}/roles`), payload)
  },

  roles(tenantId: EntityId): Promise<Role[]> {
    return http.get<Role[] | { items?: Role[] }>(tenantPath(tenantId, '/roles')).then((raw) =>
      Array.isArray(raw) ? raw : (raw.items ?? []),
    )
  },

  createRole(tenantId: EntityId, payload: RolePayload): Promise<Role> {
    return http.post<Role>(tenantPath(tenantId, '/roles'), payload)
  },

  updateRole(tenantId: EntityId, roleId: EntityId, payload: RolePatchPayload): Promise<Role> {
    return http.patch<Role>(tenantPath(tenantId, `/roles/${segment(roleId)}`), payload)
  },

  deleteRole(tenantId: EntityId, roleId: EntityId): Promise<void> {
    return http.delete<void>(tenantPath(tenantId, `/roles/${segment(roleId)}`))
  },

  async permissions(tenantId: EntityId): Promise<PermissionItem[]> {
    const raw = await http.get<PermissionItem[] | PermissionGroup[] | PermissionCatalogResponse>(
      tenantPath(tenantId, '/permissions'),
    )
    return normalizePermissions(raw)
  },

  auditLogs(tenantId: EntityId, query: AuditQuery = {}): Promise<PagedData<AuditLog>> {
    return requestPage<AuditLog>(tenantPath(tenantId, '/audit-logs'), query as QueryParams)
  },
}
