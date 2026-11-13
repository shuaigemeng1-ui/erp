import type { EntityId } from './common'

/** 实例环境（后端仅接受 production/test） */
export type OpsEnvironment = 'production' | 'test'

/** 实例状态：待接入 / 运行中 / 已暂停 */
export type OpsInstanceStatus = 'pending' | 'active' | 'suspended'

/** 服务器节点状态 */
export type OpsNodeStatus = 'pending' | 'online' | 'offline' | 'unknown'

/** 告警状态 */
export type OpsAlertStatus = 'open' | 'acknowledged' | 'resolved'

/** 创建实例时的租户下拉项（GET /platform/tenants） */
export interface OpsTenantOption {
  id: EntityId
  name: string
  code: string
}

export interface OpsInstance {
  id: EntityId
  tenant_id: EntityId
  tenant_name: string
  name: string
  domain: string
  environment: OpsEnvironment | string
  status: OpsInstanceStatus | string
  app_version: string
  /** 批次1新增发布/升级展示字段；旧接口可能缺失 */
  target_version?: string | null
  actual_version?: string | null
  agent_version?: string | null
  executor_version?: string | null
  db_version?: string | null
  upgrade_status?: string | null
  notes: string
  created_at?: string | null
  node_count: number
  online_node_count: number
  open_alert_count: number
  nodes?: OpsServerNode[]
}

/** 指标样本中的单块磁盘信息 */
export interface OpsMetricDisk {
  mount?: string | null
  percent?: number | null
  used_gb?: number | null
  total_gb?: number | null
  free_gb?: number | null
}

/** 节点指标样本（字段与后端 telemetry 响应一致） */
export interface OpsNodeMetric {
  sampled_at?: string | null
  received_at?: string | null
  cpu_percent: number
  load1: number
  load5: number
  load15: number
  memory_total_mb: number
  memory_used_mb: number
  memory_available_mb: number
  memory_percent: number
  swap_total_mb: number
  swap_used_mb: number
  disks: OpsMetricDisk[]
  network: Record<string, unknown>
  services: Record<string, unknown>
  public_ip?: string
}

export interface OpsServerNode {
  id: EntityId
  instance_id: EntityId
  tenant_id: EntityId
  node_uuid: string
  hostname: string
  os_name: string
  arch: string
  cpu_cores: number
  memory_total_mb: number
  disk_total_gb: number
  internal_ips: string[]
  configured_public_ip: string
  observed_public_ip: string
  agent_version: string
  app_version: string
  /** 节点实际上报的应用版本（批次1新增） */
  actual_version?: string | null
  executor_version?: string | null
  protocol_version?: number | null
  db_version?: string | null
  status: OpsNodeStatus | string
  enrolled_at?: string | null
  last_seen_at?: string | null
  last_metrics_at?: string | null
  latest_metric?: OpsNodeMetric | null
}

export interface OpsProbeCheck {
  target: string
  url: string
  ok: boolean
  status_code?: number | null
  latency_ms: number
  error: string
  checked_at?: string | null
}

export interface OpsProbeResult {
  node_id: EntityId
  instance_id: EntityId
  domain: string
  checks: OpsProbeCheck[]
}

export interface OpsNodeMetrics {
  node_id: EntityId
  minutes: number
  items: OpsNodeMetric[]
  latest: OpsNodeMetric | null
}

/** 聚合粒度：5 分钟 / 1 小时 */
export type OpsAggregateResolution = '5m' | '1h'

/** 节点指标聚合桶 */
export interface OpsNodeAggregate {
  bucket_start: string | null
  sample_count: number
  cpu_avg: number
  cpu_max: number
  memory_avg: number
  memory_max: number
  load_avg: number
  disk_max: number
}

export interface OpsNodeAggregates {
  node_id: EntityId
  resolution: OpsAggregateResolution | string
  items: OpsNodeAggregate[]
}

export interface OpsNodeEnrollment {
  node: OpsServerNode
  node_uuid: string
  enrollment_token: string
  expires_at: string
  expires_in: number
  install_command: string
}

export interface OpsAlert {
  id: EntityId
  node_id: EntityId
  hostname: string
  instance_id: EntityId
  alert_type: string
  severity: string
  status: OpsAlertStatus | string
  message: string
  metric_value: string
  threshold: string
  first_seen_at?: string | null
  last_seen_at?: string | null
  acknowledged_at?: string | null
  resolved_at?: string | null
}

export interface OpsInstanceCreatePayload {
  tenant_id: EntityId
  name: string
  domain: string
  environment: OpsEnvironment
  notes?: string
}

export interface OpsInstanceUpdatePayload {
  name?: string
  domain?: string
  status?: OpsInstanceStatus
  app_version?: string
  notes?: string
}

export interface OpsAlertAckResult {
  id: EntityId
  status: string
  message: string
}

export interface OpsNodeSshConfig {
  host: string
  detected_host: string
  port: number
  username: string
  has_password: boolean
}

export interface OpsNodeSshConfigPayload {
  host: string
  port: number
  username?: string | null
  password?: string | null
}

export interface OpsNodeSshExecResult {
  output: string
  exit_status: number
  host: string
}
