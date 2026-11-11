import type { Decimal, EntityId } from './common'
import type { TenantLifecycleStatus } from './tenant'

export type ProvisioningStatus = 'pending' | 'running' | 'succeeded' | 'failed' | string

export interface PlatformTenant {
  id: EntityId
  code: string
  name: string
  status: TenantLifecycleStatus
  timezone: string
  plan_id?: EntityId | null
  plan_name?: string | null
  owner_user_id?: EntityId | null
  owner_email?: string | null
  suspended_at?: string | null
  created_at?: string
  updated_at?: string
}

export interface ProvisioningJob {
  id: EntityId
  tenant_id: EntityId
  step: string
  status: ProvisioningStatus
  attempts: number
  error_message?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export interface TenantDatabaseStatus {
  host?: string
  database_name?: string
  schema_version?: string | null
  status?: string
  credentials_version?: number
}

/** 租户负责人邀请（中心库 tenant_invitations，详情接口只返回脱敏摘要） */
export interface OwnerInvitation {
  invitation_id?: EntityId
  email: string
  status: string
  expires_at?: string | null
  invited_at?: string | null
}

/** POST /platform/tenants/{tenant_id}/owner-invitation 的返回 */
export interface OwnerInvitationCreated {
  invitation_id: EntityId
  email: string
  status?: string
  token?: string
  /** 相对路径，如 /activate/{token}；前端拼接当前站点 origin */
  invitation_url: string
  expires_at: string
  invited_at?: string | null
}

export interface PlatformTenantDetail extends PlatformTenant {
  provisioning_jobs?: ProvisioningJob[]
  database?: TenantDatabaseStatus | null
  member_count?: number
  /** 后端字段为 owner_invitation（snake_case） */
  owner_invitation?: OwnerInvitation | null
  /** camelCase 兼容别名，两种返回都能读取 */
  ownerInvitation?: OwnerInvitation | null
}

export interface PlanFeature {
  feature_code: string
  limit_value: number | null
}

export interface Plan {
  id: EntityId
  code: string
  name: string
  status: string
  description?: string | null
  features?: PlanFeature[]
  created_at?: string
}

export interface PlanPayload {
  code: string
  name: string
  description?: string
  features?: PlanFeature[]
}

export interface PlanPatchPayload {
  name?: string
  status?: string
  description?: string
  features?: PlanFeature[]
}

export interface ChannelApp {
  channel: string
  environment: string
  partner_id: string | null
  has_partner_key: boolean
  has_partner_secret: boolean
  redirect_url: string | null
  status: string
}

export interface ChannelAppPayload {
  partner_id: string
  partner_key?: string
  partner_secret?: string
  redirect_url: string
}

export interface CreateTenantPayload {
  code: string
  name: string
  timezone: string
  plan_id?: EntityId
  owner_email: string
}

export interface CreateTenantResult {
  task_id: string
  tenant?: PlatformTenant
}

export interface PlatformAuditLog {
  id: EntityId
  actor_user_id?: EntityId | null
  action: string
  resource_type: string
  resource_id?: EntityId | null
  tenant_id?: EntityId | null
  summary?: string
  payload?: unknown
  ip?: string
  request_id?: string
  created_at?: string
}

export interface TenantCreateQuota {
  plan_id?: EntityId
  limit_value?: Decimal
}
