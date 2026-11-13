import { http, requestPage } from './http'
import { segment } from './paths'
import type { EntityId, PageParams, PagedData, QueryParams } from '@/types'
import type {
  OpsAggregateResolution,
  OpsAlert,
  OpsAlertAckResult,
  OpsBackupStage,
  OpsInstance,
  OpsInstanceCreatePayload,
  OpsInstanceUpdatePayload,
  OpsMaintenanceWindow,
  OpsMaintenanceWindowPayload,
  OpsMonitoringOverall,
  OpsMonitoringOverview,
  OpsMonitorComponent,
  OpsMonitorComponentStatus,
  OpsNodeAggregates,
  OpsNodeEnrollment,
  OpsNodeMetrics,
  OpsNodeSshConfig,
  OpsNodeSshConfigPayload,
  OpsNodeSshExecResult,
  OpsPerformanceSummary,
  OpsProbeLatency,
  OpsProbeResult,
  OpsServerNode,
  OpsTenantOption,
} from '@/types/ops'

export interface OpsInstanceQuery extends PageParams {
  q?: string
  status?: string
  deployment_mode?: string
  migration_status?: string
}

export interface OpsAlertQuery extends PageParams {
  status?: string
  node_id?: EntityId
}

/* ------------------------------ 运维中心 ------------------------------ */

/** 发布版本状态（后端 draft/active/retired） */
export type OpsReleaseStatus = 'draft' | 'active' | 'retired'

/** 构建状态（发布可用性仍由 status 控制） */
export type OpsBuildStatus = 'built' | 'verified' | 'failed'

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
  /** 完整 40/64 位 commit；旧记录为空串 */
  commit_sha?: string | null
  /** 构建配置摘要；旧记录为空串 */
  build_config_summary?: string | null
  /** 最低 Agent 协议版本；旧记录为 0 */
  min_agent_protocol?: number | null
  center_db_version?: string | null
  tenant_db_version?: string | null
  /** 是否允许旧应用兼容新库（回退兼容声明） */
  rollback_compatible?: boolean | null
  build_status?: OpsBuildStatus | string | null
  notes: string
  created_at?: string | null
}

export interface OpsReleaseCreatePayload {
  version: string
  /** 必填，40/64 位完整 Git commit */
  commit_sha: string
  image_tag?: string
  image_digest?: string
  status?: OpsReleaseStatus
  build_config_summary?: string
  min_agent_protocol?: number
  center_db_version?: string
  tenant_db_version?: string
  rollback_compatible?: boolean
  build_status?: OpsBuildStatus
  notes?: string
}

/** 回填旧发布版本：version/image 不可改，其余字段局部更新。 */
export interface OpsReleaseUpdatePayload {
  commit_sha?: string
  build_config_summary?: string
  min_agent_protocol?: number
  center_db_version?: string
  tenant_db_version?: string
  rollback_compatible?: boolean
  build_status?: OpsBuildStatus
  status?: OpsReleaseStatus
  notes?: string
}

/** 登记发布版本返回（后端现返回完整 ReleaseItem） */
export interface OpsReleaseCreateResult {
  id: EntityId
  version: string
}

export interface OpsOperationCheck {
  name: string
  ok: boolean
  value?: string
  message?: string
}

export interface OpsOperationStep {
  step_no: number
  name: string
  status: string
  message?: string
}

/** 运维任务项：GET /platform/operations 分页数据 */
export interface OpsOperation {
  id: EntityId
  instance_id: EntityId
  node_id?: EntityId | null
  job_type: OpsJobType | string
  status: OpsOperationStatus | string
  target_version: string
  /** 批次1新增字段，旧接口缺失 */
  actual_version?: string | null
  agent_version?: string | null
  agent_protocol_version?: number | null
  db_version?: string | null
  upgrade_status?: string | null
  attempt_id?: string | null
  lease_expires_at?: string | null
  last_heartbeat_at?: string | null
  event_seq?: number | null
  checks_status?: string | null
  checks?: OpsOperationCheck[] | null
  steps?: OpsOperationStep[] | null
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
  /* 批次3：本地生成/校验/异地上传/恢复验证，旧后端缺失 */
  trigger?: string | null
  local_status?: string | null
  local_path?: string | null
  verified_at?: string | null
  checksum_status?: string | null
  checksum?: string | null
  offsite_status?: string | null
  offsite_key?: string | null
  offsite_bucket?: string | null
  encryption?: string | null
  restore_status?: string | null
  restore_verified_at?: string | null
  restore_notes?: string | null
  failure_reason?: string | null
  retention_until?: string | null
  encrypted?: boolean | null
  stages?: OpsBackupStage[] | null
}

export interface OpsOperationQuery extends PageParams {
  instance_id?: EntityId
}

export interface OpsBackupQuery extends PageParams {
  instance_id?: EntityId
}

/* ------------------------------------------------------------------ *
 * 批次3：监控分项归一化（兼容 /monitoring/overview 与 /monitoring/status）
 * ------------------------------------------------------------------ */

const MONITOR_COMPONENT_LABELS: Record<string, string> = {
  identity_db: '身份库',
  business_db: '业务库',
  redis: 'Redis',
  worker: 'Worker',
  scheduler: '调度器',
  api: 'API',
  nginx: 'Nginx',
}

const MONITOR_COMPONENT_ALIASES: Record<string, string> = {
  identitydb: 'identity_db',
  identity: 'identity_db',
  center_db: 'identity_db',
  center: 'identity_db',
  auth_db: 'identity_db',
  businessdb: 'business_db',
  tenant_db: 'business_db',
  tenant: 'business_db',
  business: 'business_db',
  redis_cache: 'redis',
  cache: 'redis',
  celery_worker: 'worker',
  celery_scheduler: 'scheduler',
  beat: 'scheduler',
}

const MONITOR_OK_VALUES = new Set([
  'ok',
  'up',
  'healthy',
  'pass',
  'passed',
  'success',
  'succeeded',
  'running',
  'online',
  'active',
])
const MONITOR_DEGRADED_VALUES = new Set(['degraded', 'warning', 'warn', 'stale', 'partial', 'slow'])
const MONITOR_ERROR_VALUES = new Set([
  'error',
  'down',
  'failed',
  'fail',
  'unhealthy',
  'offline',
  'critical',
])

function monitorComponentKey(value: string): string {
  const normalized = value.trim().toLowerCase().replace(/[\s-]+/g, '_')
  return MONITOR_COMPONENT_ALIASES[normalized] ?? normalized
}

function toNullableNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

function normalizeComponentStatusValue(value: unknown): OpsMonitorComponentStatus {
  const text = String(value ?? '').trim().toLowerCase()
  if (MONITOR_OK_VALUES.has(text)) return 'ok'
  if (MONITOR_DEGRADED_VALUES.has(text)) return 'degraded'
  if (MONITOR_ERROR_VALUES.has(text)) return 'error'
  return 'unknown'
}

function componentFromRaw(raw: unknown, keyHint = ''): OpsMonitorComponent | null {
  if (typeof raw === 'string') {
    const key = monitorComponentKey(keyHint || raw)
    return {
      key: key || keyHint || raw,
      label: MONITOR_COMPONENT_LABELS[key] ?? (keyHint || raw),
      status: normalizeComponentStatusValue(raw),
      message: '',
      latency_ms: null,
      last_heartbeat_at: null,
      checked_at: null,
    }
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const record = raw as Record<string, unknown>
  const keySource = String(record.key ?? record.name ?? keyHint ?? '').trim()
  const key = monitorComponentKey(keySource)
  const explicitLabel = typeof record.label === 'string' ? record.label.trim() : ''
  const message =
    typeof record.message === 'string'
      ? record.message
      : typeof record.detail === 'string'
        ? record.detail
        : ''
  const status =
    typeof record.ok === 'boolean'
      ? record.ok
        ? 'ok'
        : 'error'
      : normalizeComponentStatusValue(record.status)
  return {
    key: key || keySource,
    label: explicitLabel || MONITOR_COMPONENT_LABELS[key] || keySource || '-',
    status,
    message,
    latency_ms: toNullableNumber(record.latency_ms),
    last_heartbeat_at: nullableString(record.last_heartbeat_at),
    checked_at: nullableString(record.checked_at),
  }
}

/** 把 /overview（数组或对象）与 /status 的 components 统一为可渲染列表；缺失返回空数组。 */
export function normalizeMonitorComponents(raw: unknown): OpsMonitorComponent[] {
  if (!raw) return []
  if (Array.isArray(raw)) {
    return raw
      .map((item) => componentFromRaw(item))
      .filter((item): item is OpsMonitorComponent => item !== null)
  }
  if (typeof raw === 'object') {
    return Object.entries(raw as Record<string, unknown>)
      .map(([key, value]) => componentFromRaw(value, key))
      .filter((item): item is OpsMonitorComponent => item !== null)
  }
  return []
}

/** 整体状态：优先后端 overall，缺失时按分项推导。 */
export function normalizeMonitorOverall(
  raw: unknown,
  components: OpsMonitorComponent[] = [],
): OpsMonitoringOverall {
  const text = String(raw ?? '').trim().toLowerCase()
  if (['healthy', 'ok', 'up', 'good', 'normal'].includes(text)) return 'healthy'
  if (['degraded', 'warning', 'warn', 'stale', 'partial'].includes(text)) return 'degraded'
  if (['unhealthy', 'error', 'down', 'failed', 'critical', 'offline'].includes(text)) {
    return 'unhealthy'
  }
  if (components.some((item) => item.status === 'error')) return 'unhealthy'
  if (components.some((item) => item.status === 'degraded')) return 'degraded'
  if (components.length > 0 && components.every((item) => item.status === 'ok')) return 'healthy'
  return 'unknown'
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

  nodeSshConfig(nodeId: EntityId): Promise<OpsNodeSshConfig> {
    return http.get<OpsNodeSshConfig>(`/platform/nodes/${segment(nodeId)}/ssh`)
  },

  saveNodeSshConfig(nodeId: EntityId, payload: OpsNodeSshConfigPayload): Promise<OpsNodeSshConfig> {
    return http.put<OpsNodeSshConfig>(`/platform/nodes/${segment(nodeId)}/ssh`, payload)
  },

  /** 管理员确认服务器变更后重置 SSH 主机指纹；必须显式 confirm=true。 */
  resetNodeSshFingerprint(nodeId: EntityId): Promise<OpsNodeSshConfig> {
    return http.post<OpsNodeSshConfig>(`/platform/nodes/${segment(nodeId)}/ssh/fingerprint/reset`, {
      confirm: true,
    })
  },

  execNodeSsh(nodeId: EntityId, command: string): Promise<OpsNodeSshExecResult> {
    return http.post<OpsNodeSshExecResult>(`/platform/nodes/${segment(nodeId)}/ssh/exec`, { command })
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

  createRelease(payload: OpsReleaseCreatePayload): Promise<OpsRelease> {
    return http.post<OpsRelease>('/platform/releases', payload)
  },

  /** 回填旧发布版本 commit/构建信息；version/image_* 不允许修改。 */
  updateRelease(releaseId: EntityId, payload: OpsReleaseUpdatePayload): Promise<OpsRelease> {
    return http.patch<OpsRelease>(`/platform/releases/${segment(releaseId)}`, payload)
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

  /* ------------------------------ 监控 / 性能 / 维护窗口 ------------------------------ */

  /** 批次3：HTTP 请求性能（P95/P99/5xx）；window 60~86400 秒。 */
  httpMetrics(windowSeconds = 300, silent = false): Promise<OpsPerformanceSummary> {
    return http.get<OpsPerformanceSummary>(
      '/platform/metrics/http',
      { window_seconds: windowSeconds },
      { silent },
    )
  },

  /** 批次3：公网探测耗时曲线（root/healthz/api）。 */
  probeLatency(nodeId: EntityId, hours = 24, silent = false): Promise<OpsProbeLatency> {
    return http.get<OpsProbeLatency>(
      `/platform/nodes/${segment(nodeId)}/probe-latency`,
      { hours },
      { silent },
    )
  },

  /**
   * 批次3：监控总览。Lead 定稿为 /platform/monitoring/overview，
   * task-29 契约同时提供 /platform/monitoring/status；先取 overview，404 回退 status。
   * 旧后端两者都缺失时返回 null，页面按块隐藏。
   */
  async monitoringOverview(instanceId?: EntityId): Promise<OpsMonitoringOverview | null> {
    const query = instanceId ? { instance_id: instanceId } : undefined
    for (const path of ['/platform/monitoring/overview', '/platform/monitoring/status']) {
      try {
        const data = await http.get<OpsMonitoringOverview>(path, query, { silent: true })
        if (data) return data
      } catch {
        // 旧后端没有该接口，继续尝试兼容路径
      }
    }
    return null
  },

  /** 实例维护窗口（现有接口）：兼容旧后端/空列表。 */
  async maintenanceWindows(instanceId: EntityId): Promise<OpsMaintenanceWindow[]> {
    const raw = await http.get<OpsMaintenanceWindow[] | { items?: OpsMaintenanceWindow[] }>(
      `/platform/instances/${segment(instanceId)}/maintenance-windows`,
    )
    return Array.isArray(raw) ? raw : (raw?.items ?? [])
  },

  createMaintenanceWindow(
    instanceId: EntityId,
    payload: OpsMaintenanceWindowPayload,
  ): Promise<OpsMaintenanceWindow> {
    return http.post<OpsMaintenanceWindow>(
      `/platform/instances/${segment(instanceId)}/maintenance-windows`,
      payload,
    )
  },

  deleteMaintenanceWindow(windowId: EntityId): Promise<{ deleted: boolean }> {
    return http.delete<{ deleted: boolean }>(`/platform/maintenance-windows/${segment(windowId)}`)
  },
}
