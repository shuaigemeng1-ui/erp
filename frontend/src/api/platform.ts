import { http, requestPage } from './http'
import { segment } from './paths'
import type { PlatformSystemSettings, StorageConnectionResult, StorageProfile, StorageProfilePayload } from '@/types/storage'
import type {
  EmailProfile,
  EmailProfilePayload,
  EmailProfilesResponse,
  EmailTestResult,
} from '@/types/email'
import type {
  AcceptedTask,
  ChannelApp,
  ChannelAppPayload,
  CreateTenantPayload,
  CreateTenantResult,
  EntityId,
  PageParams,
  PagedData,
  OwnerInvitationCreated,
  Plan,
  PlanPatchPayload,
  PlanPayload,
  PlatformAuditLog,
  PlatformTenant,
  PlatformTenantDetail,
  ProvisioningJob,
  QueryParams,
} from '@/types'

export interface PlatformTenantQuery extends PageParams {
  status?: string
  q?: string
}

export interface PlatformAuditQuery extends PageParams {
  action?: string
  tenant_id?: EntityId
}

function toList<T>(raw: T[] | { items?: T[] } | null): T[] {
  if (!raw) return []
  return Array.isArray(raw) ? raw : (raw.items ?? [])
}

export const platformApi = {
  systemSettings(): Promise<PlatformSystemSettings> {
    return http.get<PlatformSystemSettings>('/platform/system-settings')
  },

  createStorageProfile(payload: StorageProfilePayload): Promise<StorageProfile> {
    return http.post<StorageProfile>('/platform/storage-profiles', payload)
  },

  updateStorageProfile(id: EntityId, payload: StorageProfilePayload): Promise<StorageProfile> {
    return http.put<StorageProfile>(`/platform/storage-profiles/${segment(id)}`, payload)
  },

  setDefaultStorage(profileId: EntityId, version: number): Promise<PlatformSystemSettings> {
    return http.put<PlatformSystemSettings>('/platform/system-settings/default-storage', { profile_id: profileId, version })
  },

  testStorageProfile(id: EntityId): Promise<StorageConnectionResult> {
    return http.post<StorageConnectionResult>(`/platform/storage-profiles/${segment(id)}/test`)
  },

  emailProfiles(): Promise<EmailProfilesResponse> {
    return http.get<EmailProfilesResponse>('/platform/email-profiles')
  },

  createEmailProfile(payload: EmailProfilePayload): Promise<EmailProfile> {
    return http.post<EmailProfile>('/platform/email-profiles', payload)
  },

  updateEmailProfile(id: EntityId, payload: EmailProfilePayload): Promise<EmailProfile> {
    return http.put<EmailProfile>(`/platform/email-profiles/${segment(id)}`, payload)
  },

  setDefaultEmailProfile(id: EntityId): Promise<EmailProfile> {
    return http.post<EmailProfile>(`/platform/email-profiles/${segment(id)}/default`)
  },

  testEmailProfile(id: EntityId, to: string, subject?: string): Promise<EmailTestResult> {
    return http.post<EmailTestResult>(`/platform/email-profiles/${segment(id)}/test`, {
      to,
      ...(subject ? { subject } : {}),
    })
  },

  tenants(query: PlatformTenantQuery = {}): Promise<PagedData<PlatformTenant>> {
    return requestPage<PlatformTenant>('/platform/tenants', query as QueryParams)
  },

  tenant(tenantId: EntityId): Promise<PlatformTenantDetail> {
    return http.get<PlatformTenantDetail>(`/platform/tenants/${segment(tenantId)}`)
  },

  createTenant(payload: CreateTenantPayload): Promise<CreateTenantResult> {
    return http.post<CreateTenantResult>('/platform/tenants', payload)
  },

  retryProvision(tenantId: EntityId): Promise<AcceptedTask> {
    return http.post<AcceptedTask>(`/platform/tenants/${segment(tenantId)}/retry-provision`)
  },

  /** 服务器无邮件通道：由平台管理员生成负责人邀请链接并复制发送 */
  issueOwnerInvitation(tenantId: EntityId): Promise<OwnerInvitationCreated> {
    return http.post<OwnerInvitationCreated>(`/platform/tenants/${segment(tenantId)}/owner-invitation`)
  },

  suspendTenant(tenantId: EntityId, reason: string): Promise<AcceptedTask> {
    return http.post<AcceptedTask>(`/platform/tenants/${segment(tenantId)}/suspend`, { reason })
  },

  activateTenant(tenantId: EntityId): Promise<AcceptedTask> {
    return http.post<AcceptedTask>(`/platform/tenants/${segment(tenantId)}/activate`)
  },

  provisioningJob(jobId: EntityId): Promise<ProvisioningJob> {
    return http.get<ProvisioningJob>(`/platform/provisioning-jobs/${segment(jobId)}`)
  },

  async plans(): Promise<Plan[]> {
    const raw = await http.get<Plan[] | { items?: Plan[] }>('/platform/plans')
    return toList(raw)
  },

  createPlan(payload: PlanPayload): Promise<Plan> {
    return http.post<Plan>('/platform/plans', payload)
  },

  updatePlan(planId: EntityId, payload: PlanPatchPayload): Promise<Plan> {
    return http.patch<Plan>(`/platform/plans/${segment(planId)}`, payload)
  },

  channelApp(channel: string, environment: string): Promise<ChannelApp> {
    // 未配置时后端返回 404，这是正常的“空配置”状态，页面会展示空表单，不弹全局错误
    return http.get<ChannelApp>(
      `/platform/channel-apps/${segment(channel)}/${segment(environment)}`,
      undefined,
      { silent: true },
    )
  },

  saveChannelApp(channel: string, environment: string, payload: ChannelAppPayload): Promise<ChannelApp> {
    return http.put<ChannelApp>(`/platform/channel-apps/${segment(channel)}/${segment(environment)}`, payload)
  },

  auditLogs(query: PlatformAuditQuery = {}): Promise<PagedData<PlatformAuditLog>> {
    return requestPage<PlatformAuditLog>('/platform/audit-logs', query as QueryParams)
  },
}
