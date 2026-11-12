import { http, requestPage } from './http'
import { flattenPermissionGroups, normalizePermissionGroups } from './permissionCatalog'
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

  /** 权限目录（后端分组 + 中文名）：`GET /tenants/{tenant_id}/permissions`。 */
  async permissionGroups(tenantId: EntityId): Promise<PermissionGroup[]> {
    const raw = await http.get<unknown>(tenantPath(tenantId, '/permissions'))
    return normalizePermissionGroups(raw)
  },

  /** 扁平权限项列表（保留原调用方式；需要分组展示时使用 `permissionGroups`）。 */
  async permissions(tenantId: EntityId): Promise<PermissionItem[]> {
    return flattenPermissionGroups(await tenantApi.permissionGroups(tenantId))
  },

  auditLogs(tenantId: EntityId, query: AuditQuery = {}): Promise<PagedData<AuditLog>> {
    return requestPage<AuditLog>(tenantPath(tenantId, '/audit-logs'), query as QueryParams)
  },
}
