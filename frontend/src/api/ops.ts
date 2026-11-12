import { http, requestPage } from './http'
import { segment } from './paths'
import type { EntityId, PageParams, PagedData, QueryParams } from '@/types'
import type {
  OpsAggregateResolution,
  OpsAlert,
  OpsAlertAckResult,
  OpsInstance,
  OpsInstanceCreatePayload,
  OpsInstanceUpdatePayload,
  OpsNodeAggregates,
  OpsNodeEnrollment,
  OpsNodeMetrics,
  OpsProbeResult,
  OpsServerNode,
  OpsTenantOption,
} from '@/types/ops'

export interface OpsInstanceQuery extends PageParams {
  q?: string
  status?: string
}

export interface OpsAlertQuery extends PageParams {
  status?: string
  node_id?: EntityId
}

/* ------------------------------ 运维中心 ------------------------------ */

/** 发布版本状态（后端 draft/active/retired） */
export type OpsReleaseStatus = 'draft' | 'active' | 'retired'

/** 运维任务类型（后端白名单） */
export type OpsJobType = 'deploy' | 'upgrade' | 'restart' | 'backup' | 'logs' | 'config'

/** 运维任务状态 */
export type OpsOperationStatus = 'pending' | 'leased' | 'running' | 'succeeded' | 'failed' | 'unknown'

/** 发布版本：GET /platform/releases 直接返回数组 */
export interface OpsRelease {
  id: EntityId
  version: string
  image_tag: string
  image_digest: string
  status: OpsReleaseStatus | string
  notes: string
  created_at?: string | null
}

export interface OpsReleaseCreatePayload {
  version: string
  image_tag?: string
  image_digest?: string
  notes?: string
}

/** 登记发布版本返回：后端只回 id/version */
export interface OpsReleaseCreateResult {
  id: EntityId
  version: string
}

/** 运维任务项：GET /platform/operations 分页数据 */
export interface OpsOperation {
  id: EntityId
  instance_id: EntityId
  node_id?: EntityId | null
  job_type: OpsJobType | string
  status: OpsOperationStatus | string
  target_version: string
  attempts: number
  error: string
  result: Record<string, unknown> | null
  created_at?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export interface OpsOperationCreatePayload {
  job_type: OpsJobType
  target_version?: string
  payload?: Record<string, unknown>
  /** 留空时不下发，由后端生成幂等键。 */
  idempotency_key?: string
}

/** 备份记录项：GET /platform/backups 分页数据 */
export interface OpsBackup {
  id: EntityId
  instance_id: EntityId
  status: string
  storage_key: string
  size_bytes: number
  started_at?: string | null
  finished_at?: string | null
  error: string
}

export interface OpsOperationQuery extends PageParams {
  instance_id?: EntityId
}

export interface OpsBackupQuery extends PageParams {
  instance_id?: EntityId
}

export const opsApi = {
  instances(query: OpsInstanceQuery = {}): Promise<PagedData<OpsInstance>> {
    return requestPage<OpsInstance>('/platform/instances', query as QueryParams)
  },

  createInstance(payload: OpsInstanceCreatePayload): Promise<OpsInstance> {
    return http.post<OpsInstance>('/platform/instances', payload)
  },

  instance(instanceId: EntityId): Promise<OpsInstance> {
    return http.get<OpsInstance>(`/platform/instances/${segment(instanceId)}`)
  },

  updateInstance(instanceId: EntityId, payload: OpsInstanceUpdatePayload): Promise<OpsInstance> {
    return http.patch<OpsInstance>(`/platform/instances/${segment(instanceId)}`, payload)
  },

  nodes(instanceId: EntityId): Promise<OpsServerNode[]> {
    return http.get<OpsServerNode[]>(`/platform/instances/${segment(instanceId)}/nodes`)
  },

  /** 生成一次性接入令牌：明文 token 仅本次返回，后端 30 分钟过期。 */
  createNodeEnrollment(instanceId: EntityId): Promise<OpsNodeEnrollment> {
    return http.post<OpsNodeEnrollment>(`/platform/instances/${segment(instanceId)}/nodes`)
  },

  probeNode(nodeId: EntityId): Promise<OpsProbeResult> {
    return http.post<OpsProbeResult>(`/platform/nodes/${segment(nodeId)}/probe`)
  },

  nodeMetrics(nodeId: EntityId, minutes = 60): Promise<OpsNodeMetrics> {
    return http.get<OpsNodeMetrics>(`/platform/nodes/${segment(nodeId)}/metrics`, { minutes })
  },

  /** 节点指标聚合趋势：5m 适合 1/24 小时，1h 适合 7/30 天。 */
  nodeAggregates(
    nodeId: EntityId,
    resolution: OpsAggregateResolution = '5m',
    hours = 24,
  ): Promise<OpsNodeAggregates> {
    return http.get<OpsNodeAggregates>(`/platform/nodes/${segment(nodeId)}/aggregates`, {
      resolution,
      hours,
    })
  },

  alerts(query: OpsAlertQuery = {}): Promise<PagedData<OpsAlert>> {
    return requestPage<OpsAlert>('/platform/alerts', query as QueryParams)
  },

  acknowledgeAlert(alertId: EntityId, note: string): Promise<OpsAlertAckResult> {
    return http.post<OpsAlertAckResult>(`/platform/alerts/${segment(alertId)}/ack`, { note })
  },

  /**
   * 创建实例时的租户下拉：直接请求平台租户列表，只取 id/name/code。
   * 后端返回分页结构；这里同时兼容裸数组，避免接口形态差异导致下拉为空。
   */
  async tenantOptions(): Promise<OpsTenantOption[]> {
    const raw = await http.get<PagedData<OpsTenantOption> | OpsTenantOption[]>('/platform/tenants', {
      page: 1,
      page_size: 100,
    })
    const items = Array.isArray(raw) ? raw : (raw?.items ?? [])
    return items.map((item) => ({
      id: String(item.id),
      name: item.name,
      code: item.code,
    }))
  },

  /* ------------------------------ 运维中心 ------------------------------ */

  /** 发布版本列表：后端 data 直接是数组。 */
  releases(): Promise<OpsRelease[]> {
    return http.get<OpsRelease[]>('/platform/releases')
  },

  createRelease(payload: OpsReleaseCreatePayload): Promise<OpsReleaseCreateResult> {
    return http.post<OpsReleaseCreateResult>('/platform/releases', payload)
  },

  operations(query: OpsOperationQuery = {}): Promise<PagedData<OpsOperation>> {
    return requestPage<OpsOperation>('/platform/operations', query as QueryParams)
  },

  /** 创建运维任务：idempotency_key 留空时不传，后端自动生成。 */
  retryOperation(operationId: EntityId): Promise<OpsOperation> {
    return http.post<OpsOperation>(`/platform/operations/${segment(operationId)}/retry`)
  },

  createOperation(instanceId: EntityId, payload: OpsOperationCreatePayload): Promise<OpsOperation> {
    const body: Record<string, unknown> = {
      job_type: payload.job_type,
      target_version: payload.target_version ?? '',
      payload: payload.payload ?? {},
    }
    const idempotencyKey = payload.idempotency_key?.trim()
    if (idempotencyKey) body.idempotency_key = idempotencyKey
    return http.post<OpsOperation>(`/platform/instances/${segment(instanceId)}/operations`, body)
  },

  backups(query: OpsBackupQuery = {}): Promise<PagedData<OpsBackup>> {
    return requestPage<OpsBackup>('/platform/backups', query as QueryParams)
  },
}
