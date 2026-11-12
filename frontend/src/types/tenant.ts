import type { EntityId } from './common'

export type TenantLifecycleStatus =
  | 'provisioning'
  | 'active'
  | 'suspended'
  | 'provision_failed'
  | string

export type MemberStatus = 'active' | 'invited' | 'disabled' | string

export type ScopeType = 'all' | 'stores' | 'stock_places'

export interface TenantInfo {
  id: EntityId
  code: string
  name: string
  status: TenantLifecycleStatus
  timezone: string
}

export interface MeTenant extends TenantInfo {
  member_status: MemberStatus
  roles: string[]
}

export interface ScopeBinding {
  scope_type: ScopeType
  scope_ids: EntityId[]
}

export interface RoleBinding {
  role_id: EntityId
  role_code?: string
  role_name?: string
  scopes: ScopeBinding[]
}

export interface RoleBindingInput {
  role_id: EntityId
  scopes: ScopeBinding[]
}

/** GET /tenants/{tenant_id}/bootstrap */
export interface BootstrapMember {
  id: EntityId
  roles: string[]
  bindings: RoleBinding[]
}

export interface BootstrapData {
  tenant: TenantInfo
  member: BootstrapMember
  permissions: string[]
  feature_flags: Record<string, boolean>
  permission_version: number
  server_time: string
}

export interface Member {
  id: EntityId
  email: string
  display_name: string
  status: MemberStatus
  is_owner?: boolean
  roles?: string[]
  role_bindings?: RoleBinding[]
  joined_at?: string | null
  last_login_at?: string | null
}

export interface InviteMemberPayload {
  email: string
  role_bindings: RoleBindingInput[]
}

export interface InvitationCreated {
  invitation_id: EntityId
  expires_at: string
}

export interface Role {
  id: EntityId
  code: string
  name: string
  description?: string | null
  is_system: boolean
  permissions: string[]
  copied_from_role_id?: EntityId | null
}

export interface RolePayload {
  code: string
  name: string
  permissions: string[]
  description?: string
  copy_from_role_id?: EntityId
}

export interface RolePatchPayload {
  name?: string
  permissions?: string[]
  description?: string
}

export interface PermissionItem {
  code: string
  name: string
  /** 所属分组编码，如 catalog */
  group?: string
}

/** 权限目录分组（`GET /tenants/{tenant_id}/permissions` 归一化后的结构）。 */
export interface PermissionGroup {
  /** 分组编码，如 catalog */
  code: string
  /** 分组中文名，如 商品 */
  name: string
  items: PermissionItem[]
}

export interface MemberStatusPayload {
  status: 'active' | 'disabled'
}

export interface MemberRolesPayload {
  role_bindings: RoleBindingInput[]
}
