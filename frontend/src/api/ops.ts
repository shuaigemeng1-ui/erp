import { http, requestPage } from './http'
import { segment } from './paths'
import type { EntityId, PageParams, PagedData, QueryParams } from '@/types'
import type {
  OpsAlert,
  OpsAlertAckResult,
  OpsInstance,
  OpsInstanceCreatePayload,
  OpsInstanceUpdatePayload,
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
}
