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
  /** 批次2新增：共享中心 / 独立部署 */
  deployment_mode?: 'shared' | 'independent' | string | null
  /** 批次2新增迁移状态 */
  migration_status?: 'registered' | 'migrating' | 'migrated' | 'rollback' | string | null
  /** 后端派生只读：独立部署/迁移相关状态下拒绝中心写操作 */
  write_blocked?: boolean | null
  app_version: string
  /** 批次1新增发布/升级展示字段；旧接口可能缺失 */
  target_version?: string | null
  actual_version?: string | null
  agent_version?: string | null
  executor_version?: string | null
  db_version?: string | null
  upgrade_status?: string | null
  /** 批次3：实例详情可能内联监控分项/业务性能；缺失时回退监控总览接口或节点指标 */
  components?: OpsMonitorComponentRaw[] | Record<string, OpsMonitorComponentRaw | string> | null
  performance?: OpsPerformanceSummary | null
  monitoring?: OpsMonitoringOverview | null
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
  /** 批次3：服务端按真实数据盘汇总（排除 /boot 等），旧样本缺失 */
  data_disk?: OpsMetricDisk | null
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
  /** SSH 指纹只读摘要（task-25） */
  ssh_host?: string | null
  ssh_host_fingerprint?: string | null
  ssh_fingerprint_algorithm?: string | null
  ssh_host_fingerprint_verified_at?: string | null
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
  /** HTTPS 证书剩余天数（后端已有；旧字段） */
  cert_days_left?: number | null
  /** 批次3：该目标连续失败次数（若后端内联到 checks） */
  consecutive_failures?: number | null
  error: string
  checked_at?: string | null
}

/** 批次3：probe 响应按 root/api 汇总的目标摘要，旧后端缺失 */
export interface OpsProbeTargetSummary {
  ok?: boolean | null
  url?: string | null
  status?: string | null
  latency_ms?: number | null
  consecutive_failures?: number | null
  checked_at?: string | null
}

export interface OpsProbeResult {
  node_id: EntityId
  instance_id: EntityId
  domain: string
  checks: OpsProbeCheck[]
  /** 批次3：target → 摘要；旧后端缺失 */
  targets?: Record<string, OpsProbeTargetSummary> | null
  home_ok?: boolean | null
  api_ok?: boolean | null
  api_consecutive_failures?: number | null
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
  /** 批次3：维护窗口/抑制/恢复通知字段，旧后端缺失 */
  in_maintenance?: boolean | null
  maintenance_reason?: string | null
  suppressed?: boolean | null
  suppress_until?: string | null
  recovery_notified_at?: string | null
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
  host_configured?: boolean
  credentials_configured?: boolean
  /** SSH 主机指纹字段，旧后端缺失 */
  ssh_host_fingerprint?: string | null
  ssh_fingerprint_algorithm?: string | null
  ssh_host_fingerprint_verified_at?: string | null
  /** verified / unverified */
  fingerprint_status?: string | null
  /** 重置接口返回标记 */
  reset?: boolean
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

/* ------------------------------------------------------------------ *
 * 批次3：监控分项 / 请求性能 / 公网探测耗时 / 维护窗口 / 备份阶段
 * ------------------------------------------------------------------ */

/** 监控分项状态（归一化）：ok=正常，degraded=降级，error=异常，unknown=未知 */
export type OpsMonitorComponentStatus = 'ok' | 'degraded' | 'error' | 'unknown'

/** 监控分项（归一化后，页面直接渲染） */
export interface OpsMonitorComponent {
  key: string
  label: string
  status: OpsMonitorComponentStatus
  message: string
  latency_ms?: number | null
  last_heartbeat_at?: string | null
  checked_at?: string | null
}

/** 后端原始监控分项：/overview 用 name/status/message，/status 用 key/label/status/detail */
export interface OpsMonitorComponentRaw {
  key?: string | null
  name?: string | null
  label?: string | null
  status?: string | null
  message?: string | null
  detail?: string | null
  latency_ms?: number | null
  last_heartbeat_at?: string | null
  checked_at?: string | null
}

export type OpsMonitoringOverall = 'healthy' | 'degraded' | 'unhealthy' | 'unknown'

/** 路由级请求性能 */
export interface OpsPerformanceRoute {
  route: string
  method?: string | null
  request_count?: number | null
  error_count?: number | null
  error_5xx?: number | null
  rate_5xx?: number | null
  p95_ms?: number | null
  p99_ms?: number | null
}

/** GET /platform/metrics/http；同时用于 overview.performance / business_requests */
export interface OpsPerformanceSummary {
  window_seconds?: number | null
  generated_at?: string | null
  checked_at?: string | null
  enabled?: boolean | null
  degraded?: boolean | null
  degraded_reason?: string | null
  status?: string | null
  request_count?: number | null
  error_count?: number | null
  error_5xx?: number | null
  rate_5xx?: number | null
  rate_error?: number | null
  avg_ms?: number | null
  p50_ms?: number | null
  p95_ms?: number | null
  p99_ms?: number | null
  slow_count?: number | null
  slow_threshold_ms?: number | null
  route_count?: number | null
  thresholds?: Record<string, number> | null
  routes?: OpsPerformanceRoute[] | null
}

/** overview.public_probes 项：每 target 的最新探测汇总 */
export interface OpsPublicProbeSummary {
  node_id?: EntityId | null
  instance_id?: EntityId | null
  domain?: string | null
  target: string
  url?: string | null
  ok?: boolean | null
  status?: string | null
  status_code?: number | null
  latency_ms?: number | null
  consecutive_failures?: number | null
  ok_nodes?: number | null
  failed_nodes?: number | null
  total_nodes?: number | null
  checked_at?: string | null
  error?: string | null
}

/** 监控总览：GET /platform/monitoring/overview（定稿）或 /status（task-29 兼容） */
export interface OpsMonitoringOverview {
  overall?: string | null
  checked_at?: string | null
  generated_at?: string | null
  components?: OpsMonitorComponentRaw[] | Record<string, OpsMonitorComponentRaw | string> | null
  performance?: OpsPerformanceSummary | null
  business_requests?: OpsPerformanceSummary | null
  public_probes?: OpsPublicProbeSummary[] | null
}

/** GET /platform/nodes/{id}/probe-latency：公网探测耗时汇总 */
export interface OpsProbeLatencyTarget {
  target: string
  url?: string | null
  samples?: number | null
  ok_count?: number | null
  ok_rate?: number | null
  avg_ms?: number | null
  p95_ms?: number | null
  min_ms?: number | null
  max_ms?: number | null
  last_checked_at?: string | null
  last_latency_ms?: number | null
  last_ok?: boolean | null
}

export interface OpsProbeLatencyPoint {
  checked_at?: string | null
  target: string
  ok?: boolean | null
  latency_ms?: number | null
}

export interface OpsProbeLatency {
  node_id: EntityId
  instance_id: EntityId
  domain: string
  hours: number
  generated_at?: string | null
  targets: OpsProbeLatencyTarget[]
  series: OpsProbeLatencyPoint[]
}

/** 实例维护窗口（现有接口） */
export interface OpsMaintenanceWindow {
  id: EntityId
  instance_id: EntityId
  starts_at?: string | null
  ends_at?: string | null
  reason: string
  created_by?: EntityId | null
}

export interface OpsMaintenanceWindowPayload {
  starts_at: string
  ends_at: string
  reason?: string
}

/** 备份阶段（本地生成/校验/异地上传/恢复验证） */
export interface OpsBackupStage {
  key?: string | null
  name?: string | null
  label?: string | null
  status?: string | null
  message?: string | null
  at?: string | null
  started_at?: string | null
  finished_at?: string | null
}

/* ------------------------------------------------------------------ *
 * 批次4：TikTok Webhook 可观测性（task-42）
 * ------------------------------------------------------------------ */

/** Gateway Webhook 统计（来自网关只读 admin webhook-stats；缺失字段为 null） */
export interface OpsWebhookGatewayMetrics {
  available?: boolean
  source?: string
  window_seconds?: number | null
  windowed?: boolean
  reason?: string
  unavailable_fields?: string[]
  unavailable_reason?: string
  instances?: {
    total?: number
    active?: number
    verified?: number
  } | null
  received_total?: number | null
  signature_failed?: number | null
  duplicate?: number | null
  pending?: number | null
  oldest_pending_age_seconds?: number | null
  oldest_pending_at?: string | null
  delivered?: number | null
  dead?: number | null
  unroutable?: number | null
  unroutable_recent?: number | null
  unroutable_recent_source?: string
  retry?: number | null
  leased?: number | null
  due?: number | null
  success_rate?: number | null
  p95_ms?: number | null
  last_delivery_latency_ms?: number | null
  last_delivery_at?: string | null
  last_status_code?: number | null
  checked_at?: string | null
}

/** 单店铺 Webhook 指标 */
export interface OpsWebhookStoreMetrics {
  store_id?: string
  store_name?: string
  external_store_id?: string
  events_received?: number
  events_pending?: number
  events_failed?: number
  orders_pending?: number
  oldest_pending_age_seconds?: number | null
  orders_written?: number
  refresh_completed?: number
  orders_dead?: number
  refresh_success_rate?: number | null
  end_to_end_p95_ms?: number | null
  end_to_end_samples?: number
  backfill_watermark?: string | null
  last_event_at?: string | null
}

/** 租户 Webhook 指标（含店铺下钻） */
export interface OpsWebhookTenantMetrics extends OpsWebhookStoreMetrics {
  tenant_id: string
  tenant_name?: string
  instance_id?: string
  instance_name?: string
  instance_status?: string
  outbox_failed?: number
  error?: string | null
  stores?: OpsWebhookStoreMetrics[]
}

/** Webhook 全局汇总 */
export interface OpsWebhookTotals {
  events_received?: number
  events_pending?: number
  events_failed?: number
  orders_pending?: number
  orders_written?: number
  refresh_completed?: number
  orders_dead?: number
  outbox_failed?: number
  oldest_pending_age_seconds?: number | null
  refresh_success_rate?: number | null
  end_to_end_p95_ms?: number | null
  end_to_end_samples?: number
  backfill_watermark?: string | null
}

export interface OpsWebhookRedisMemory {
  available?: boolean
  label?: string
  used_memory_bytes?: number | null
  used_memory_human?: string | null
  maxmemory_bytes?: number | null
  maxmemory_human?: string | null
  connected_clients?: number | null
  error?: string
}

export interface OpsWebhookRedisMetrics {
  available?: boolean
  cache?: OpsWebhookRedisMemory
  queue?: OpsWebhookRedisMemory
  publish_failed?: number
  publish_failed_source?: string
  rate_limited?: number | null
  rate_limited_reason?: string
}

/** GET /platform/monitoring/webhooks 响应 */
export interface OpsWebhookMetrics {
  checked_at?: string | null
  window_seconds?: number
  filters?: {
    tenant_id?: string | null
    store_id?: string | null
  }
  gateway?: OpsWebhookGatewayMetrics
  totals?: OpsWebhookTotals
  tenants?: OpsWebhookTenantMetrics[]
  redis?: OpsWebhookRedisMetrics
  notes?: string[]
}
