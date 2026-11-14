<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { CopyDocument, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { normalizeMonitorComponents, normalizeMonitorOverall, opsApi } from '@/api/ops'
import type { OpsRelease } from '@/api/ops'
import { isApiError } from '@/api/http'
import { useTableQuery } from '@/composables/useTableQuery'
import PaginationBar from '@/components/PaginationBar.vue'
import IdCopy from '@/components/IdCopy.vue'
import { environmentLabels, labelOf } from '@/utils/dict'
import { formatDateTime, stringOrUndefined, textOrDash } from '@/utils/format'
import type {
  OpsAlert,
  OpsEnvironment,
  OpsInstance,
  OpsInstanceStatus,
  OpsMaintenanceWindow,
  OpsMonitoringOverall,
  OpsMonitoringOverview,
  OpsMonitorComponent,
  OpsMonitorComponentStatus,
  OpsNodeAggregates,
  OpsNodeEnrollment,
  OpsNodeMetric,
  OpsNodeMetrics,
  OpsNodeSshConfig,
  OpsPerformanceSummary,
  OpsProbeLatency,
  OpsProbeResult,
  OpsPublicProbeSummary,
  OpsServerNode,
  OpsTenantOption,
} from '@/types/ops'

type TagType = 'success' | 'warning' | 'info' | 'danger'

const DOMAIN_PATTERN =
  /^(?=.{1,255}$)(?!-)[a-zA-Z0-9-]{1,63}(?<!-)(\.(?!-)[a-zA-Z0-9-]{1,63}(?<!-))+$/

const ALERT_TYPE_LABELS: Record<string, string> = {
  cpu_high: 'CPU 过高',
  memory_high: '内存过高',
  disk_high: '磁盘过高',
  agent_offline: 'Agent 离线',
  public_unreachable: '公网不可达',
  api_unreachable: 'API 不可达',
  cert_expiring: '证书即将过期',
  backup_overdue: '备份超期',
  webhook_e2e_p95_high: 'Webhook 端到端 P95 过高',
  webhook_pending_stale: 'Webhook 待刷新积压',
  webhook_refresh_dead: 'Webhook 刷新 dead',
  webhook_unroutable: 'Webhook unroutable',
  webhook_redis_publish_failed: 'Webhook Redis 发布失败',
}

type TrendRangeKey = '1h' | '24h' | '7d' | '30d'
type TrendAxisAnchor = 'start' | 'middle' | 'end'

interface TrendRangeOption {
  key: TrendRangeKey
  label: string
  resolution: '5m' | '1h'
  hours: number
}

interface TrendPoint {
  timestamp: number
  iso: string
  cpu: number
  memory: number
  samples: number
}

interface TrendYAxisTick {
  value: number
  y: number
}

interface TrendXAxisTick {
  x: number
  y: number
  label: string
  anchor: TrendAxisAnchor
}

interface TrendChartDot {
  timestamp: number
  x: number
  cpuY: number
  memoryY: number
}

interface TrendChartGeometry {
  cpuPath: string
  memoryPath: string
  yTicks: TrendYAxisTick[]
  xTicks: TrendXAxisTick[]
  dots: TrendChartDot[]
}

const TREND_RANGES: TrendRangeOption[] = [
  { key: '1h', label: '近 1 小时', resolution: '5m', hours: 1 },
  { key: '24h', label: '近 24 小时', resolution: '5m', hours: 24 },
  { key: '7d', label: '近 7 天', resolution: '1h', hours: 168 },
  { key: '30d', label: '近 30 天', resolution: '1h', hours: 720 },
]

const CHART_WIDTH = 760
const CHART_HEIGHT = 250
const CHART_LEFT = 42
const CHART_RIGHT = 16
const CHART_TOP = 18
const CHART_BOTTOM = 30
const CHART_PLOT_WIDTH = CHART_WIDTH - CHART_LEFT - CHART_RIGHT
const CHART_PLOT_HEIGHT = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM
const CHART_VIEW_BOX = `0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`
const CHART_Y_TICKS = [100, 75, 50, 25, 0]

function instanceTagType(status?: string | null): TagType {
  if (status === 'active') return 'success'
  if (status === 'suspended') return 'warning'
  if (status === 'pending') return 'info'
  return 'danger'
}

function instanceStatusText(status?: string | null): string {
  if (status === 'active') return '运行中'
  if (status === 'suspended') return '已暂停'
  if (status === 'pending') return '待接入'
  return textOrDash(status)
}

function deploymentModeType(mode?: string | null): TagType {
  return mode === 'independent' ? 'success' : 'info'
}

function deploymentModeText(mode?: string | null): string {
  if (mode === 'shared') return '共享中心'
  if (mode === 'independent') return '独立部署'
  return textOrDash(mode)
}

function migrationStatusType(status?: string | null): TagType {
  if (status === 'migrating') return 'warning'
  if (status === 'migrated') return 'success'
  if (status === 'rollback') return 'danger'
  return 'info'
}

function migrationStatusText(status?: string | null): string {
  if (status === 'registered') return '已登记'
  if (status === 'migrating') return '迁移中'
  if (status === 'migrated') return '已迁出'
  if (status === 'rollback') return '回滚'
  return textOrDash(status)
}

/** 迁移中/已迁出/回滚/独立部署均不允许旧中心写操作。 */
function centerOperationsBlockedReason(instance: OpsInstance): string {
  if (instance.deployment_mode === 'independent') {
    return '实例为独立部署，请通过迁移流程操作'
  }
  if (instance.migration_status === 'migrating') return '实例迁移中，旧中心操作已暂停'
  if (instance.migration_status === 'migrated') return '实例已迁出到独立部署，旧中心操作不可用'
  if (instance.migration_status === 'rollback') return '实例处于迁移回滚状态，旧中心操作已暂停'
  if (instance.write_blocked) return '实例当前禁止旧中心写操作，请通过迁移流程处理'
  return ''
}

function nodeTagType(status?: string | null): TagType {
  if (status === 'online') return 'success'
  if (status === 'offline') return 'danger'
  if (status === 'pending') return 'info'
  return 'warning'
}

function nodeStatusText(status?: string | null): string {
  if (status === 'online') return '在线'
  if (status === 'offline') return '离线'
  if (status === 'pending') return '待接入'
  if (status === 'unknown') return '未知'
  return textOrDash(status)
}

function alertSeverityType(severity?: string | null): TagType {
  return severity === 'critical' ? 'danger' : 'warning'
}

function alertSeverityText(severity?: string | null): string {
  if (severity === 'critical') return '严重'
  if (severity === 'warning') return '警告'
  return textOrDash(severity)
}

function alertStatusType(status?: string | null): TagType {
  if (status === 'resolved') return 'success'
  if (status === 'acknowledged') return 'warning'
  return 'danger'
}

function alertStatusText(status?: string | null): string {
  if (status === 'open') return '未恢复'
  if (status === 'acknowledged') return '已确认'
  if (status === 'resolved') return '已恢复'
  return textOrDash(status)
}

function alertTypeText(alertType?: string | null): string {
  if (!alertType) return '-'
  return ALERT_TYPE_LABELS[alertType] ?? alertType
}

function alertSuppressTitle(alert: OpsAlert): string {
  const parts: string[] = []
  if (alert.maintenance_reason) parts.push(`维护原因：${alert.maintenance_reason}`)
  if (alert.suppress_until) parts.push(`下次可通知：${formatDateTime(alert.suppress_until)}`)
  return parts.join('；') || '处于维护窗口或重复告警抑制期'
}

/* ------------------------------ 监控 / 性能展示 ------------------------------ */
function componentStatusType(status: OpsMonitorComponentStatus): TagType {
  if (status === 'ok') return 'success'
  if (status === 'degraded') return 'warning'
  if (status === 'error') return 'danger'
  return 'info'
}

function componentStatusText(status: OpsMonitorComponentStatus): string {
  if (status === 'ok') return '正常'
  if (status === 'degraded') return '降级'
  if (status === 'error') return '异常'
  return '未知'
}

function overallType(overall: OpsMonitoringOverall): TagType {
  if (overall === 'healthy') return 'success'
  if (overall === 'degraded') return 'warning'
  if (overall === 'unhealthy') return 'danger'
  return 'info'
}

function overallText(overall: OpsMonitoringOverall): string {
  if (overall === 'healthy') return '健康'
  if (overall === 'degraded') return '降级'
  if (overall === 'unhealthy') return '异常'
  return '未知'
}

function formatCount(value?: number | null): string {
  if (value === null || value === undefined) return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toLocaleString('zh-CN') : '-'
}

function formatMs(value?: number | null, digits = 1): string {
  if (value === null || value === undefined) return '-'
  const parsed = Number(value)
  return Number.isFinite(parsed) ? `${parsed.toFixed(digits)} ms` : '-'
}

/** 5xx 比例后端按 0~1 返回；兼容个别接口直接给百分比。 */
function formatRate(value?: number | null): string {
  if (value === null || value === undefined) return '-'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '-'
  const percent = parsed <= 1 ? parsed * 100 : parsed
  return `${percent.toFixed(2)}%`
}

function isHigh5xx(value?: number | null): boolean {
  const parsed = Number(value ?? 0)
  if (!Number.isFinite(parsed)) return false
  const ratio = parsed <= 1 ? parsed : parsed / 100
  return ratio > 0.01
}

function formatThroughput(bytes?: number | null): string {
  const value = Number(bytes ?? 0)
  if (!Number.isFinite(value) || value <= 0) return '0 B/s'
  if (value < 1024) return `${value.toFixed(0)} B/s`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB/s`
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB/s`
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB/s`
}

function performanceWindowText(performance: OpsPerformanceSummary | null): string {
  const seconds = Number(performance?.window_seconds ?? 0)
  if (!seconds) return '最近 5 分钟'
  if (seconds < 60) return `最近 ${seconds} 秒`
  if (seconds % 3600 === 0) return `最近 ${seconds / 3600} 小时`
  if (seconds % 60 === 0) return `最近 ${seconds / 60} 分钟`
  return `最近 ${seconds} 秒`
}

function probeTargetText(target?: string | null): string {
  if (target === 'root') return '首页 /'
  if (target === 'api') return 'API /readyz'
  if (target === 'healthz') return '健康检查 /healthz'
  return textOrDash(target)
}

function probeStatusType(probe: OpsPublicProbeSummary): TagType {
  if (probe.ok === true || probe.status === 'ok' || probe.status === 'up') return 'success'
  if (probe.ok === false || ['failed', 'error', 'down'].includes(probe.status ?? '')) return 'danger'
  return 'info'
}

function probeStatusText(probe: OpsPublicProbeSummary): string {
  if (probe.ok === true || probe.status === 'ok' || probe.status === 'up') return '正常'
  if (probe.ok === false || ['failed', 'error', 'down'].includes(probe.status ?? '')) return '异常'
  return '未知'
}

function performanceDegradedText(performance: OpsPerformanceSummary | null): string {
  return (
    performance?.degraded_reason ||
    'Redis 直方图暂不可用，当前数值可能来自本地样本或为 0。'
  )
}

function probeLabel(probe: OpsPublicProbeSummary): string {
  const label = probeTargetText(probe.target)
  return probe.domain ? `${label} · ${probe.domain}` : label
}

function percentText(value?: number | null, digits = 1): string {
  if (value === null || value === undefined) return '-'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '-'
  return `${parsed.toFixed(digits)}%`
}

function numberText(value?: number | null, digits = 2): string {
  if (value === null || value === undefined) return '-'
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return '-'
  return parsed.toFixed(digits)
}

function nodeSpecText(node: OpsServerNode): string {
  const memory = node.memory_total_mb ? `${Math.round(node.memory_total_mb / 1024)}G` : '-'
  const disk = node.disk_total_gb ? `${node.disk_total_gb}G` : '-'
  return `${node.cpu_cores || 0}C / ${memory} / ${disk}`
}

function nodeMetricText(node: OpsServerNode): string {
  const metric = node.latest_metric
  if (!metric) return '-'
  return `CPU ${percentText(metric.cpu_percent)} / 内存 ${percentText(metric.memory_percent)}`
}

function maxDiskPercent(metric?: OpsNodeMetric | null): number | null {
  const disks = metric?.disks ?? []
  let maximum: number | null = null
  for (const disk of disks) {
    const parsed = Number(disk.percent)
    if (!Number.isFinite(parsed)) continue
    if (maximum === null || parsed > maximum) maximum = parsed
  }
  return maximum
}

function diskSummary(metric?: OpsNodeMetric | null): string {
  const disks = metric?.disks ?? []
  if (!disks.length) return '-'
  let top = disks[0]
  for (const disk of disks) {
    const value = Number(disk.percent)
    const topValue = Number(top.percent)
    if ((Number.isFinite(value) ? value : 0) > (Number.isFinite(topValue) ? topValue : 0)) {
      top = disk
    }
  }
  return `${top.mount || '/'} ${percentText(top.percent)}`
}

function normalizeDomain(value: string): string {
  return value.trim().toLowerCase().replace(/\.$/, '')
}

/** 图表横轴标签仍基于 formatDateTime，短周期显示到时:分，长周期只显示日期。 */
function formatTrendAxisTime(timestamp: number, rangeKey: TrendRangeKey): string {
  const formatted = formatDateTime(new Date(timestamp).toISOString())
  return rangeKey === '1h' || rangeKey === '24h' ? formatted.slice(5, 16) : formatted.slice(5, 10)
}

function environmentText(environment?: string | null): string {
  return labelOf(environmentLabels, environment)
}

/* ------------------------------ 实例列表 ------------------------------ */
const instanceQuery = reactive({
  q: '',
  status: '',
  deployment_mode: '',
  migration_status: '',
})

const {
  rows: instanceRows,
  total: instanceTotal,
  page: instancePage,
  pageSize: instancePageSize,
  loading: instanceLoading,
  search: searchInstances,
  reload: reloadInstances,
  handlePageChange: handleInstancePageChange,
  handleSizeChange: handleInstanceSizeChange,
} = useTableQuery<OpsInstance>((params) =>
  opsApi.instances({
    page: params.page,
    page_size: params.page_size,
    q: stringOrUndefined(instanceQuery.q),
    status: stringOrUndefined(instanceQuery.status),
    deployment_mode: stringOrUndefined(instanceQuery.deployment_mode),
    migration_status: stringOrUndefined(instanceQuery.migration_status),
  }),
)

/* ------------------------------ 告警中心 ------------------------------ */
const alertQuery = reactive({
  status: '',
})

const {
  rows: alertRows,
  total: alertTotal,
  page: alertPage,
  pageSize: alertPageSize,
  loading: alertLoading,
  search: searchAlerts,
  reload: reloadAlerts,
  handlePageChange: handleAlertPageChange,
  handleSizeChange: handleAlertSizeChange,
} = useTableQuery<OpsAlert>(
  (params) =>
    opsApi.alerts({
      page: params.page,
      page_size: params.page_size,
      status: stringOrUndefined(alertQuery.status),
    }),
  { pageSize: 10 },
)

/* ------------------------------ 新建实例 ------------------------------ */
const tenantOptions = ref<OpsTenantOption[]>([])
const tenantLoading = ref(false)
const createVisible = ref(false)
const createSubmitting = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = ref({
  tenant_id: '',
  name: '',
  domain: '',
  environment: 'production' as OpsEnvironment,
  notes: '',
})
const createRules: FormRules = {
  tenant_id: [{ required: true, message: '请选择租户', trigger: 'change' }],
  name: [
    { required: true, message: '请输入实例名称', trigger: 'blur' },
    { max: 100, message: '实例名称不能超过 100 个字符', trigger: 'blur' },
  ],
  domain: [
    { required: true, message: '请输入域名', trigger: 'blur' },
    { pattern: DOMAIN_PATTERN, message: '请输入合法域名，不含协议和路径', trigger: 'blur' },
  ],
  environment: [{ required: true, message: '请选择环境', trigger: 'change' }],
}

async function loadTenantOptions(): Promise<void> {
  if (tenantLoading.value) return
  tenantLoading.value = true
  try {
    tenantOptions.value = await opsApi.tenantOptions()
  } catch {
    tenantOptions.value = []
  } finally {
    tenantLoading.value = false
  }
}

function openCreate(): void {
  createForm.value = {
    tenant_id: '',
    name: '',
    domain: '',
    environment: 'production',
    notes: '',
  }
  createVisible.value = true
  if (!tenantOptions.value.length) void loadTenantOptions()
}

async function submitCreate(): Promise<void> {
  const formInstance = createFormRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  createSubmitting.value = true
  try {
    const form = createForm.value
    await opsApi.createInstance({
      tenant_id: form.tenant_id,
      name: form.name.trim(),
      domain: normalizeDomain(form.domain),
      environment: form.environment,
      notes: form.notes.trim(),
    })
    ElMessage.success('实例已登记')
    createVisible.value = false
    await reloadInstances()
  } finally {
    createSubmitting.value = false
  }
}

/* ------------------------------ 实例详情 / 节点 ------------------------------ */
const drawerVisible = ref(false)
const detailLoading = ref(false)
const currentInstance = ref<OpsInstance | null>(null)
const nodes = ref<OpsServerNode[]>([])
let detailRequestSeq = 0

const drawerTitle = computed(() =>
  currentInstance.value ? `实例详情：${currentInstance.value.name}` : '实例详情',
)

function openDetail(row: OpsInstance): void {
  drawerVisible.value = true
  currentInstance.value = row
  nodes.value = []
  maintenanceWindows.value = []
  selectedNode.value = null
  metricsData.value = null
  metricsLoading.value = false
  void loadDetail(row.id)
}

function handleInstanceRowClick(row: OpsInstance): void {
  openDetail(row)
}

async function loadDetail(instanceId: string): Promise<void> {
  const seq = ++detailRequestSeq
  detailLoading.value = true
  try {
    const [detail, nodeList] = await Promise.all([opsApi.instance(instanceId), opsApi.nodes(instanceId)])
    if (seq !== detailRequestSeq) return
    currentInstance.value = detail
    nodes.value = nodeList
    void loadMaintenanceWindows(detail.id)
    if (selectedNode.value) {
      selectedNode.value = nodeList.find((node) => node.id === selectedNode.value?.id) ?? selectedNode.value
    }
  } catch {
    // 统一请求层已经展示错误，这里保留当前已加载内容
  } finally {
    if (seq === detailRequestSeq) detailLoading.value = false
  }
}

/* ------------------------------ 维护窗口 ------------------------------ */
type MaintenanceWindowStatus = 'active' | 'upcoming' | 'past' | 'unknown'

const maintenanceWindows = ref<OpsMaintenanceWindow[]>([])
const maintenanceLoading = ref(false)
const maintenanceDialogVisible = ref(false)
const maintenanceSubmitting = ref(false)
const maintenanceWindowFormRef = ref<FormInstance>()
const maintenanceWindowForm = ref({
  starts_at: '',
  ends_at: '',
  reason: '',
})
const maintenanceRules: FormRules = {
  starts_at: [{ required: true, message: '请选择开始时间', trigger: 'change' }],
  ends_at: [{ required: true, message: '请选择结束时间', trigger: 'change' }],
}

function maintenanceWindowStatus(window: OpsMaintenanceWindow): MaintenanceWindowStatus {
  if (!window.starts_at || !window.ends_at) return 'unknown'
  const start = Date.parse(window.starts_at)
  const end = Date.parse(window.ends_at)
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 'unknown'
  const now = Date.now()
  if (now < start) return 'upcoming'
  if (now <= end) return 'active'
  return 'past'
}

function maintenanceWindowStatusType(window: OpsMaintenanceWindow): TagType {
  const status = maintenanceWindowStatus(window)
  if (status === 'active') return 'warning'
  if (status === 'upcoming') return 'info'
  if (status === 'past') return 'success'
  return 'info'
}

function maintenanceWindowStatusText(window: OpsMaintenanceWindow): string {
  const status = maintenanceWindowStatus(window)
  if (status === 'active') return '进行中'
  if (status === 'upcoming') return '未开始'
  if (status === 'past') return '已结束'
  return '未知'
}

const activeMaintenanceWindow = computed(
  () =>
    maintenanceWindows.value.find((window) => maintenanceWindowStatus(window) === 'active') ?? null,
)

async function loadMaintenanceWindows(instanceId?: string): Promise<void> {
  const target = instanceId ?? currentInstance.value?.id
  if (!target) {
    maintenanceWindows.value = []
    return
  }
  maintenanceLoading.value = true
  try {
    maintenanceWindows.value = await opsApi.maintenanceWindows(target)
  } catch {
    maintenanceWindows.value = []
  } finally {
    maintenanceLoading.value = false
  }
}

function openMaintenanceCreate(): void {
  maintenanceWindowForm.value.starts_at = ''
  maintenanceWindowForm.value.ends_at = ''
  maintenanceWindowForm.value.reason = ''
  maintenanceDialogVisible.value = true
}

async function submitMaintenanceWindow(): Promise<void> {
  const instance = currentInstance.value
  const formInstance = maintenanceWindowFormRef.value
  if (!instance || !formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  const start = Date.parse(maintenanceWindowForm.value.starts_at)
  const end = Date.parse(maintenanceWindowForm.value.ends_at)
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    ElMessage.warning('结束时间必须晚于开始时间')
    return
  }
  maintenanceSubmitting.value = true
  try {
    await opsApi.createMaintenanceWindow(instance.id, {
      starts_at: maintenanceWindowForm.value.starts_at,
      ends_at: maintenanceWindowForm.value.ends_at,
      reason: maintenanceWindowForm.value.reason.trim(),
    })
    ElMessage.success('维护窗口已创建')
    maintenanceDialogVisible.value = false
    await loadMaintenanceWindows(instance.id)
    await reloadAlerts()
  } finally {
    maintenanceSubmitting.value = false
  }
}

async function deleteMaintenanceWindow(window: OpsMaintenanceWindow): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除 ${formatDateTime(window.starts_at)} 至 ${formatDateTime(window.ends_at)} 的维护窗口？`,
      '删除维护窗口',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await opsApi.deleteMaintenanceWindow(window.id)
  ElMessage.success('维护窗口已删除')
  await loadMaintenanceWindows()
  await reloadAlerts()
}

const releases = ref<OpsRelease[]>([])
const releasesLoading = ref(false)
const activePageTab = ref<'servers' | 'alerts'>('servers')

/* ------------------------------ SSH 终端 ------------------------------ */
const sshVisible = ref(false)
const sshNode = ref<OpsServerNode | null>(null)
const sshLoading = ref(false)
const sshSaving = ref(false)
const sshRunning = ref(false)
const sshFingerprintResetting = ref(false)
const sshConfig = ref<OpsNodeSshConfig | null>(null)
const sshFingerprintWarning = ref('')
const terminalFingerprintWarning = ref('')
const sshHost = ref('')
const sshPort = ref(22)
const sshUsername = ref('')
const sshPassword = ref('')
const sshCommand = ref('')
const sshOutput = ref('')

function sshFingerprintStatusType(status?: string | null): TagType {
  return status === 'verified' ? 'success' : 'warning'
}

function sshFingerprintStatusText(status?: string | null): string {
  if (status === 'verified') return '已验证'
  if (status === 'unverified') return '未验证'
  return textOrDash(status)
}

function fingerprintWarningFromError(error: unknown): string {
  if (!isApiError(error)) return ''
  const details =
    error.details && typeof error.details === 'object'
      ? (error.details as Record<string, unknown>)
      : null
  if (details?.reason !== 'fingerprint_mismatch') return ''
  const expected = typeof details.expected === 'string' ? details.expected : '-'
  const actual = typeof details.actual === 'string' ? details.actual : '-'
  const algorithm = typeof details.algorithm === 'string' ? details.algorithm : '-'
  return `SSH 主机指纹不匹配（${algorithm}）：预期 ${expected}，实际 ${actual}。如确认服务器已变更，请核对后重置指纹。`
}

async function openSsh(node: OpsServerNode): Promise<void> {
  sshNode.value = node
  sshVisible.value = true
  sshLoading.value = true
  sshOutput.value = ''
  sshCommand.value = ''
  sshConfig.value = null
  sshFingerprintWarning.value = ''
  terminalFingerprintWarning.value = ''
  sshHost.value = ''
  sshPort.value = 22
  sshUsername.value = ''
  sshPassword.value = ''
  try {
    const config = await opsApi.nodeSshConfig(node.id)
    sshConfig.value = config
    sshHost.value = config.host || ''
    sshPort.value = Number(config.port || 22)
    sshUsername.value = config.username || ''
    sshPassword.value = ''
  } catch {
    // 统一请求层已经展示错误
  } finally {
    sshLoading.value = false
  }
}

async function saveSsh(): Promise<void> {
  if (!sshNode.value) return
  sshSaving.value = true
  try {
    const config = await opsApi.saveNodeSshConfig(sshNode.value.id, {
      host: sshHost.value.trim(),
      port: Number(sshPort.value || 22),
      username: sshUsername.value.trim(),
      password: sshPassword.value ? sshPassword.value : undefined,
    })
    sshConfig.value = config
    sshHost.value = config.host || ''
    sshPassword.value = ''
    sshFingerprintWarning.value = ''
    ElMessage.success(config.has_password ? 'SSH 配置已保存' : 'SSH 配置已保存，但密码为空')
  } finally {
    sshSaving.value = false
  }
}

async function resetSshFingerprint(): Promise<void> {
  if (!sshNode.value) return
  try {
    await ElMessageBox.confirm(
      '重置后下次成功连接会重新记录主机指纹。请确认已核对服务器主机密钥变更，避免中间人风险。',
      '重置 SSH 主机指纹',
      { type: 'warning', confirmButtonText: '确认重置', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  sshFingerprintResetting.value = true
  try {
    const config = await opsApi.resetNodeSshFingerprint(sshNode.value.id)
    sshConfig.value = config
    sshFingerprintWarning.value = ''
    terminalFingerprintWarning.value = ''
    ElMessage.success('SSH 主机指纹已重置')
  } catch {
    // 统一请求层已经展示错误
  } finally {
    sshFingerprintResetting.value = false
  }
}

async function runSsh(): Promise<void> {
  if (!sshNode.value || !sshCommand.value.trim()) return
  sshRunning.value = true
  sshOutput.value = ''
  try {
    const result = await opsApi.execNodeSsh(sshNode.value.id, sshCommand.value.trim())
    sshOutput.value = `$ ${sshCommand.value.trim()}\n${result.output}\n[exit ${result.exit_status}]`
  } catch (error) {
    const warning = fingerprintWarningFromError(error)
    if (warning) sshFingerprintWarning.value = warning
  } finally {
    sshRunning.value = false
  }
}

/* ------------------------------ 交互式终端 ------------------------------ */
const terminalVisible = ref(false)
const terminalHost = ref<HTMLDivElement>()
let term: Terminal | null = null
let fitAddon: FitAddon | null = null
let sshSocket: WebSocket | null = null

function sendTerminalSize(): void {
  if (!term || !sshSocket || sshSocket.readyState !== WebSocket.OPEN) return
  sshSocket.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
}

function closeTerminal(): void {
  if (sshSocket) {
    sshSocket.onmessage = null
    sshSocket.close()
    sshSocket = null
  }
  if (term) {
    term.dispose()
    term = null
  }
  fitAddon = null
}

async function openTerminal(): Promise<void> {
  if (!sshNode.value) return
  terminalVisible.value = true
  terminalFingerprintWarning.value = ''
  await nextTick()
  if (!terminalHost.value) return
  closeTerminal()
  term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: "Consolas, 'SFMono-Regular', Menlo, monospace",
    scrollback: 5000,
    theme: { background: '#1f2430', foreground: '#d8dee9', cursor: '#d8dee9' },
  })
  fitAddon = new FitAddon()
  term.loadAddon(fitAddon)
  term.open(terminalHost.value)
  fitAddon.fit()
  term.writeln('正在连接 SSH ...')
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const socket = new WebSocket(
    `${scheme}://${window.location.host}/api/v1/platform/nodes/${sshNode.value.id}/ssh/ws`,
  )
  sshSocket = socket
  socket.onopen = () => {
    term?.clear()
    sendTerminalSize()
    term?.focus()
  }
  socket.onmessage = (event) => term?.write(String(event.data))
  socket.onerror = () => term?.writeln('\r\n[连接错误]')
  socket.onclose = (event) => {
    if (event.code === 4409) {
      const message =
        'SSH 主机指纹不匹配，连接已被拒绝。请核对服务器主机密钥，确认变更后在 SSH 配置中重置指纹。'
      terminalFingerprintWarning.value = message
      sshFingerprintWarning.value = message
      ElMessage.error('SSH 主机指纹不匹配，已拒绝连接')
      return
    }
    if (event.code === 4410) {
      term?.writeln('\r\n[空闲超时，连接已断开]')
      return
    }
    if (event.code !== 1000) term?.writeln(`\r\n[连接已关闭 ${event.code}]`)
  }
  term.onData((data) => {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data }))
  })
  term.onResize(sendTerminalSize)
}
const upgradingId = ref('')

function actualVersionFor(instance: OpsInstance): string {
  return textOrDash(instance.actual_version || instance.app_version)
}

function isUpgradeInProgress(status?: string | null): boolean {
  return ['pending', 'building', 'maintenance', 'migrating', 'verifying'].includes(status ?? '')
}

function upgradeStatusType(status?: string | null): TagType {
  if (status === 'succeeded') return 'success'
  if (status === 'pending') return 'info'
  if (status === 'failed') return 'danger'
  if (
    status === 'building' ||
    status === 'maintenance' ||
    status === 'migrating' ||
    status === 'verifying'
  ) {
    return 'warning'
  }
  return 'info'
}

function upgradeStatusText(status?: string | null): string {
  if (status === 'pending') return '待升级'
  if (status === 'building') return '构建中'
  if (status === 'maintenance') return '维护中'
  if (status === 'migrating') return '迁移中'
  if (status === 'verifying') return '校验中'
  if (status === 'succeeded') return '已升级'
  if (status === 'failed') return '升级失败'
  if (status === 'unknown') return '未知'
  return textOrDash(status)
}

function isFullCommit(commitSha?: string | null): boolean {
  return /^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/.test((commitSha ?? '').trim())
}

function releaseByVersion(version?: string | null): OpsRelease | null {
  if (!version) return null
  return releases.value.find((release) => release.version === version) ?? null
}

/** 优先实例指定的目标版本，否则取最新的 active 且可升级的发布版本。 */
function preferredRelease(instance: OpsInstance): OpsRelease | null {
  if (instance.target_version) return releaseByVersion(instance.target_version)
  const activeReleases = releases.value.filter((release) => release.status === 'active')
  return (
    activeReleases.find(
      (release) => release.build_status === 'verified' && isFullCommit(release.commit_sha),
    ) ??
    activeReleases[0] ??
    releases.value[0] ??
    null
  )
}

/** 空串表示可升级；否则用于禁用按钮 tooltip。 */
/** 独立部署实例允许显式升级，但仍禁止迁移中/回滚/写阻断。 */
function upgradeCenterBlockedReason(instance: OpsInstance): string {
  if (instance.migration_status === 'migrating') return '实例迁移中，升级操作已暂停'
  if (instance.migration_status === 'rollback') return '实例处于迁移回滚状态，升级操作已暂停'
  if (instance.write_blocked) return '实例当前禁止写操作，请通过迁移流程处理'
  return ''
}

function upgradeBlockedReason(instance: OpsInstance): string {
  const centerBlocked = upgradeCenterBlockedReason(instance)
  if (centerBlocked) return centerBlocked
  if (releasesLoading.value) return '正在加载发布清单'
  const release = preferredRelease(instance)
  if (!release) {
    return instance.target_version
      ? `目标版本 ${instance.target_version} 未登记发布清单`
      : '暂无已登记发布版本'
  }
  if (release.status !== 'active') {
    return `目标版本 ${release.version} 不是 active 状态`
  }
  if (release.build_status !== 'verified') {
    return `目标版本 ${release.version} 构建状态不是 verified`
  }
  if (!isFullCommit(release.commit_sha)) {
    return `目标版本 ${release.version} 未登记完整 commit_sha`
  }
  if (isUpgradeInProgress(instance.upgrade_status)) {
    return `实例升级状态为${upgradeStatusText(instance.upgrade_status)}`
  }
  if (actualVersionFor(instance) === release.version) {
    return `实例已运行 ${release.version}`
  }
  return ''
}

function canUpgrade(instance: OpsInstance): boolean {
  return upgradeBlockedReason(instance) === ''
}

function upgradeTargetVersion(instance: OpsInstance): string {
  return preferredRelease(instance)?.version ?? '-'
}

function releaseCommitShort(release: OpsRelease | null): string {
  const commit = (release?.commit_sha ?? '').trim()
  return commit ? commit.slice(0, 12) : '-'
}

function targetReleaseCommit(instance: OpsInstance): string {
  const commit = (preferredRelease(instance)?.commit_sha ?? '').trim()
  return commit || '-'
}

async function loadReleases(): Promise<void> {
  releasesLoading.value = true
  try {
    releases.value = await opsApi.releases()
  } catch {
    releases.value = []
  } finally {
    releasesLoading.value = false
  }
}

async function upgradeInstance(instance: OpsInstance): Promise<void> {
  const blockedReason = upgradeBlockedReason(instance)
  const target = preferredRelease(instance)
  if (blockedReason || !target) {
    ElMessage.warning(blockedReason || '当前没有可升级的目标版本')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将实例「${instance.name}」从 ${actualVersionFor(instance)} 升级到 ${target.version}？\n目标 commit：${releaseCommitShort(target)}…`,
      '一键升级',
      { type: 'warning', confirmButtonText: '开始升级', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  if (upgradingId.value) return
  upgradingId.value = instance.id
  try {
    await opsApi.createOperation(instance.id, {
      job_type: 'upgrade',
      target_version: target.version,
      payload: {},
    })
    ElMessage.success(`升级任务已创建：${target.version}（commit ${releaseCommitShort(target)}）`)
    await refreshPage()
  } finally {
    upgradingId.value = ''
  }
}

async function refreshPage(): Promise<void> {
  if (drawerVisible.value && currentInstance.value) {
    await loadDetail(currentInstance.value.id)
  }
  await Promise.all([reloadInstances(), reloadAlerts(), loadReleases()])
  void loadMonitoring()
}

onMounted(() => {
  void loadReleases()
  void loadMonitoring()
})

/* ------------------------------ 启用实例 ------------------------------ */
const enablingId = ref('')

function enableBlockedReason(instance: OpsInstance): string {
  return centerOperationsBlockedReason(instance)
}

async function enableInstance(instance: OpsInstance): Promise<void> {
  const reason = enableBlockedReason(instance)
  if (reason) {
    ElMessage.warning(reason)
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认重新启用实例「${instance.name}」？启用后将恢复中心侧状态为运行中。`,
      '启用实例',
      { type: 'warning', confirmButtonText: '确认启用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  enablingId.value = instance.id
  try {
    await opsApi.updateInstance(instance.id, { status: 'active' })
    ElMessage.success('实例已启用')
    await refreshPage()
  } catch {
    // 统一请求层已经展示错误
  } finally {
    enablingId.value = ''
  }
}

/* ------------------------------ 编辑实例 ------------------------------ */
const editVisible = ref(false)
const editSubmitting = ref(false)
const editFormRef = ref<FormInstance>()
const editForm = ref({
  name: '',
  domain: '',
  status: 'pending' as OpsInstanceStatus,
  app_version: '',
  notes: '',
})
const editRules: FormRules = {
  name: [
    { required: true, message: '请输入实例名称', trigger: 'blur' },
    { max: 100, message: '实例名称不能超过 100 个字符', trigger: 'blur' },
  ],
  domain: [
    { required: true, message: '请输入域名', trigger: 'blur' },
    { pattern: DOMAIN_PATTERN, message: '请输入合法域名，不含协议和路径', trigger: 'blur' },
  ],
  status: [{ required: true, message: '请选择状态', trigger: 'change' }],
}

function openEdit(): void {
  const instance = currentInstance.value
  if (!instance) return
  editForm.value = {
    name: instance.name,
    domain: instance.domain,
    status:
      instance.status === 'active' || instance.status === 'suspended' ? instance.status : 'pending',
    app_version: instance.app_version ?? '',
    notes: instance.notes ?? '',
  }
  editVisible.value = true
}

async function submitEdit(): Promise<void> {
  const instance = currentInstance.value
  const formInstance = editFormRef.value
  if (!instance || !formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  editSubmitting.value = true
  try {
    const form = editForm.value
    await opsApi.updateInstance(instance.id, {
      name: form.name.trim(),
      domain: normalizeDomain(form.domain),
      status: form.status,
      app_version: form.app_version.trim(),
      notes: form.notes.trim(),
    })
    ElMessage.success('实例已更新')
    editVisible.value = false
    await refreshPage()
  } finally {
    editSubmitting.value = false
  }
}

/* ------------------------------ 接入令牌 ------------------------------ */
const enrollmentVisible = ref(false)
const enrollmentLoading = ref(false)
const enrollment = ref<OpsNodeEnrollment | null>(null)
const enrollmentMinutes = computed(() => {
  const seconds = Number(enrollment.value?.expires_in ?? 0)
  return seconds > 0 ? Math.max(1, Math.ceil(seconds / 60)) : 30
})

async function generateEnrollment(): Promise<void> {
  const instance = currentInstance.value
  if (!instance) return
  enrollmentLoading.value = true
  try {
    enrollment.value = await opsApi.createNodeEnrollment(instance.id)
    enrollmentVisible.value = true
    await refreshPage()
  } finally {
    enrollmentLoading.value = false
  }
}

/* ------------------------------ 节点探测 ------------------------------ */
const probingNodeId = ref('')
const probeVisible = ref(false)
const probeResult = ref<OpsProbeResult | null>(null)
const probeLatency = ref<OpsProbeLatency | null>(null)
const probeLatencyLoading = ref(false)
const probeAllOk = computed(() => {
  const checks = probeResult.value?.checks ?? []
  return checks.length > 0 && checks.every((check) => check.ok)
})
const probeApiFailed = computed(() => {
  const result = probeResult.value
  if (!result) return false
  return result.targets?.api?.ok === false || result.api_ok === false
})
const probeApiFailures = computed(() =>
  Number(
    probeResult.value?.targets?.api?.consecutive_failures ??
      probeResult.value?.api_consecutive_failures ??
      0,
  ),
)
const probeSummary = computed(() => {
  const result = probeResult.value
  if (!result) return ''
  const home = result.targets?.root?.ok ?? result.home_ok ?? null
  const api = result.targets?.api?.ok ?? result.api_ok ?? null
  if (home !== null || api !== null) {
    const parts: string[] = []
    if (home !== null) parts.push(home ? '首页正常' : '首页异常')
    if (api !== null) {
      parts.push(api ? 'API 正常' : `API 异常（连续失败 ${probeApiFailures.value} 次）`)
    }
    return `公网探测：${parts.join('，')}`
  }
  if (!result.checks.length) return '探测未返回检查项'
  return probeAllOk.value
    ? `公网访问正常，${result.checks.length} 项检查全部通过`
    : '公网访问异常，请检查域名解析、证书与服务状态'
})

function probeTargetConsecutiveFailures(target?: string | null): string {
  if (!target) return '-'
  const value = probeResult.value?.targets?.[target]?.consecutive_failures
  return value === null || value === undefined ? '-' : String(value)
}

async function loadProbeLatency(node: OpsServerNode): Promise<void> {
  probeLatencyLoading.value = true
  try {
    probeLatency.value = await opsApi.probeLatency(node.id, 24, true)
  } catch {
    probeLatency.value = null
  } finally {
    probeLatencyLoading.value = false
  }
}

async function probeNode(node: OpsServerNode): Promise<void> {
  probingNodeId.value = node.id
  probeResult.value = null
  probeLatency.value = null
  try {
    probeResult.value = await opsApi.probeNode(node.id)
    probeVisible.value = true
    void loadProbeLatency(node)
    await refreshPage()
  } finally {
    probingNodeId.value = ''
  }
}

/* ------------------------------ 节点指标 ------------------------------ */
const selectedNode = ref<OpsServerNode | null>(null)
const metricsData = ref<OpsNodeMetrics | null>(null)
const metricsLoading = ref(false)
const metricsMinutes = 60
let metricsRequestSeq = 0

const trendRangeKey = ref<TrendRangeKey>('1h')
const activeTrendRange = computed(
  () => TREND_RANGES.find((range) => range.key === trendRangeKey.value) ?? TREND_RANGES[0],
)
const aggregatesData = ref<OpsNodeAggregates | null>(null)
const aggregatesLoading = ref(false)
let aggregatesRequestSeq = 0

const latestMetric = computed<OpsNodeMetric | null>(
  () => metricsData.value?.latest ?? selectedNode.value?.latest_metric ?? null,
)
const latestDiskPercent = computed(() => maxDiskPercent(latestMetric.value))
const metricRows = computed<OpsNodeMetric[]>(() => {
  const items = metricsData.value?.items ?? []
  return items.slice().reverse().slice(0, 30)
})

/* ------------------------------ 监控总览 / 请求性能 ------------------------------ */
const monitoringLoading = ref(false)
const monitoringOverview = ref<OpsMonitoringOverview | null>(null)
const httpMetrics = ref<OpsPerformanceSummary | null>(null)
const monitoringExplicitComponents = computed(() =>
  normalizeMonitorComponents(monitoringOverview.value?.components),
)
const monitoringInstanceComponents = computed(() =>
  normalizeMonitorComponents(currentInstance.value?.components),
)
const monitoringDerivedComponents = computed(() => deriveTelemetryComponents(latestMetric.value))
const monitoringComponents = computed<OpsMonitorComponent[]>(() => {
  if (monitoringExplicitComponents.value.length) return monitoringExplicitComponents.value
  if (monitoringInstanceComponents.value.length) return monitoringInstanceComponents.value
  return monitoringDerivedComponents.value
})
const monitoringDerived = computed(
  () =>
    monitoringExplicitComponents.value.length === 0 &&
    monitoringInstanceComponents.value.length === 0 &&
    monitoringDerivedComponents.value.length > 0,
)
const monitoringOverall = computed(() =>
  normalizeMonitorOverall(monitoringOverview.value?.overall, monitoringComponents.value),
)
const businessPerformance = computed<OpsPerformanceSummary | null>(
  () =>
    monitoringOverview.value?.business_requests ??
    httpMetrics.value ??
    monitoringOverview.value?.performance ??
    currentInstance.value?.performance ??
    null,
)
const publicProbes = computed(() => monitoringOverview.value?.public_probes ?? [])
const monitoringCheckedAt = computed(
  () =>
    monitoringOverview.value?.checked_at ??
    monitoringOverview.value?.generated_at ??
    businessPerformance.value?.generated_at ??
    null,
)
const showMonitoring = computed(
  () =>
    monitoringComponents.value.length > 0 ||
    Boolean(businessPerformance.value) ||
    publicProbes.value.length > 0,
)
const performanceRoutes = computed(() =>
  (businessPerformance.value?.routes ?? [])
    .slice()
    .sort((left, right) => (right.request_count ?? 0) - (left.request_count ?? 0))
    .slice(0, 10),
)

const TELEMETRY_COMPONENT_LABELS: Record<string, string> = {
  identity_db: '身份库',
  business_db: '业务库',
  redis: 'Redis',
  worker: 'Worker',
  scheduler: '调度器',
  api: 'API',
  nginx: 'Nginx',
}

function telemetryComponentKey(name: string): string {
  const lowered = name.toLowerCase()
  if (lowered.includes('identity') || lowered.includes('center')) return 'identity_db'
  if (lowered.includes('tenant') || lowered.includes('business')) return 'business_db'
  if (lowered.includes('mysql') || lowered.includes('mariadb')) return 'business_db'
  if (lowered.includes('redis')) return 'redis'
  if (lowered.includes('worker')) return 'worker'
  if (lowered.includes('scheduler') || lowered.includes('beat')) return 'scheduler'
  if (lowered.includes('nginx')) return 'nginx'
  if (lowered.includes('api')) return 'api'
  return ''
}

function telemetryComponentStatus(state: string, health: string): OpsMonitorComponentStatus {
  if (health === 'unhealthy') return 'error'
  if (state === 'running') return 'ok'
  if (['exited', 'dead', 'removing'].includes(state)) return 'error'
  if (['paused', 'restarting', 'created'].includes(state)) return 'degraded'
  return 'unknown'
}

/** 旧后端没有监控接口时，按节点最新指标里的 Docker 容器状态推导分项。 */
function deriveTelemetryComponents(metric?: OpsNodeMetric | null): OpsMonitorComponent[] {
  const services = metric?.services
  if (!services || typeof services !== 'object') return []
  const docker = (services as Record<string, unknown>).docker
  if (!docker || typeof docker !== 'object') return []
  const containers = (docker as Record<string, unknown>).containers
  if (!Array.isArray(containers)) return []
  const components: OpsMonitorComponent[] = []
  const seen = new Set<string>()
  for (const item of containers) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    const name = String(record.name ?? '').trim()
    if (!name) continue
    const key = telemetryComponentKey(name)
    if (!key || seen.has(key)) continue
    seen.add(key)
    const state = String(record.state ?? '').trim().toLowerCase()
    const health = String(record.health ?? '').trim().toLowerCase()
    components.push({
      key,
      label: TELEMETRY_COMPONENT_LABELS[key] ?? name,
      status: telemetryComponentStatus(state, health),
      message: `${name} · ${String(record.status ?? state ?? '').trim() || '状态未知'}`,
      latency_ms: null,
      last_heartbeat_at: null,
      checked_at: metric?.sampled_at ?? null,
    })
  }
  return components
}

async function loadMonitoring(): Promise<void> {
  monitoringLoading.value = true
  try {
    const [overview, metrics] = await Promise.all([
      opsApi.monitoringOverview(),
      opsApi.httpMetrics(300, true).catch(() => null),
    ])
    monitoringOverview.value = overview
    httpMetrics.value = metrics
  } finally {
    monitoringLoading.value = false
  }
}

const latestDataDisk = computed(() => latestMetric.value?.data_disk ?? null)
const networkRate = computed(() => {
  const network = latestMetric.value?.network
  if (!network || typeof network !== 'object') return null
  const record = network as Record<string, unknown>
  const rx = Number(record.rx_bytes_per_sec ?? NaN)
  const tx = Number(record.tx_bytes_per_sec ?? NaN)
  if (!Number.isFinite(rx) && !Number.isFinite(tx)) return null
  return {
    rx: Number.isFinite(rx) ? rx : 0,
    tx: Number.isFinite(tx) ? tx : 0,
    interval: Number(record.interval_seconds ?? 0) || null,
  }
})

const trendPoints = computed<TrendPoint[]>(() => {
  const items = aggregatesData.value?.items ?? []
  return items
    .map((item): TrendPoint | null => {
      if (!item.bucket_start) return null
      const timestamp = Date.parse(item.bucket_start)
      if (!Number.isFinite(timestamp)) return null
      return {
        timestamp,
        iso: item.bucket_start,
        cpu: Number(item.cpu_avg) || 0,
        memory: Number(item.memory_avg) || 0,
        samples: Number(item.sample_count) || 0,
      }
    })
    .filter((point): point is TrendPoint => point !== null)
    .sort((left, right) => left.timestamp - right.timestamp)
})

const trendTotalSamples = computed(() =>
  trendPoints.value.reduce((total, point) => total + point.samples, 0),
)
const trendExpectedBuckets = computed(() => {
  const range = activeTrendRange.value
  const bucketMinutes = range.resolution === '5m' ? 5 : 60
  return Math.max(1, Math.ceil((range.hours * 60) / bucketMinutes))
})
const trendRangeText = computed(() => {
  const points = trendPoints.value
  if (!points.length) return ''
  return `${formatDateTime(points[0].iso)} — ${formatDateTime(points[points.length - 1].iso)}`
})
const trendNotice = computed(() => {
  const count = trendPoints.value.length
  if (!count) return ''
  if (count < 2) {
    return `样本不足：当前只有 ${count} 个聚合桶，趋势线可能不完整。`
  }
  const expected = trendExpectedBuckets.value
  if (count < expected * 0.5) {
    return `样本覆盖较少：当前 ${count} / ${expected} 个聚合桶，趋势仅供参考。`
  }
  return ''
})
const trendGranularityText = computed(() =>
  activeTrendRange.value.resolution === '5m' ? '5 分钟' : '1 小时',
)

const trendChart = computed<TrendChartGeometry>(() => {
  const points = trendPoints.value
  const yTicks = CHART_Y_TICKS.map((value) => ({
    value,
    y: CHART_TOP + (1 - value / 100) * CHART_PLOT_HEIGHT,
  }))
  if (!points.length) {
    return { cpuPath: '', memoryPath: '', yTicks, xTicks: [], dots: [] }
  }

  const first = points[0]
  const last = points[points.length - 1]
  const timeSpan = Math.max(1, last.timestamp - first.timestamp)
  const centerX = CHART_LEFT + CHART_PLOT_WIDTH / 2
  const toX = (timestamp: number): number =>
    points.length === 1
      ? centerX
      : CHART_LEFT + ((timestamp - first.timestamp) / timeSpan) * CHART_PLOT_WIDTH
  const toY = (value: number): number =>
    CHART_TOP + (1 - Math.min(100, Math.max(0, Number(value) || 0)) / 100) * CHART_PLOT_HEIGHT
  const pathFor = (field: 'cpu' | 'memory'): string =>
    points
      .map(
        (point, index) =>
          `${index === 0 ? 'M' : 'L'} ${toX(point.timestamp).toFixed(2)} ${toY(point[field]).toFixed(2)}`,
      )
      .join(' ')

  const labelY = CHART_TOP + CHART_PLOT_HEIGHT + 18
  const xTicks: TrendXAxisTick[] =
    points.length === 1
      ? [
          {
            x: centerX,
            y: labelY,
            label: formatTrendAxisTime(first.timestamp, activeTrendRange.value.key),
            anchor: 'middle',
          },
        ]
      : [0, 0.25, 0.5, 0.75, 1].map((ratio, index) => ({
          x: CHART_LEFT + CHART_PLOT_WIDTH * ratio,
          y: labelY,
          label: formatTrendAxisTime(
            first.timestamp + timeSpan * ratio,
            activeTrendRange.value.key,
          ),
          anchor: index === 0 ? 'start' : index === 4 ? 'end' : 'middle',
        }))

  const dotSource = points.length <= 48 ? points : [last]
  const dots = dotSource.map((point) => ({
    timestamp: point.timestamp,
    x: toX(point.timestamp),
    cpuY: toY(point.cpu),
    memoryY: toY(point.memory),
  }))

  return {
    cpuPath: pathFor('cpu'),
    memoryPath: pathFor('memory'),
    yTicks,
    xTicks,
    dots,
  }
})

async function loadAggregates(node: OpsServerNode): Promise<void> {
  const seq = ++aggregatesRequestSeq
  const range = activeTrendRange.value
  aggregatesLoading.value = true
  aggregatesData.value = null
  try {
    const data = await opsApi.nodeAggregates(node.id, range.resolution, range.hours)
    if (seq !== aggregatesRequestSeq) return
    aggregatesData.value = data
  } catch {
    if (seq === aggregatesRequestSeq) aggregatesData.value = null
  } finally {
    if (seq === aggregatesRequestSeq) aggregatesLoading.value = false
  }
}

function handleTrendRangeChange(value?: string | number | boolean): void {
  const key = String(value)
  if (!TREND_RANGES.some((range) => range.key === key)) return
  if (key === trendRangeKey.value) return
  trendRangeKey.value = key as TrendRangeKey
  if (selectedNode.value) void loadAggregates(selectedNode.value)
}

async function loadNodeMetrics(node: OpsServerNode): Promise<void> {
  const seq = ++metricsRequestSeq
  selectedNode.value = node
  metricsLoading.value = true
  metricsData.value = null
  void loadAggregates(node)
  try {
    const data = await opsApi.nodeMetrics(node.id, metricsMinutes)
    if (seq !== metricsRequestSeq) return
    metricsData.value = data
  } catch {
    // 统一请求层已经展示错误
  } finally {
    if (seq === metricsRequestSeq) metricsLoading.value = false
  }
}

/* ------------------------------ 告警确认 ------------------------------ */
const ackLoadingId = ref('')

async function acknowledgeAlert(alert: OpsAlert): Promise<void> {
  ackLoadingId.value = alert.id
  try {
    await opsApi.acknowledgeAlert(alert.id, '平台管理员确认')
    ElMessage.success('告警已确认')
    await refreshPage()
  } finally {
    ackLoadingId.value = ''
  }
}

/* ------------------------------ 复制 ------------------------------ */
async function copyText(value: string | null | undefined, label: string): Promise<void> {
  const text = (value ?? '').trim()
  if (!text) {
    ElMessage.warning(`暂无可复制的${label}`)
    return
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      ElMessage.success(`${label}已复制`)
      return
    }
  } catch {
    // 非安全上下文继续走降级复制
  }
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.focus()
    textarea.select()
    const copied = document.execCommand('copy')
    document.body.removeChild(textarea)
    if (copied) {
      ElMessage.success(`${label}已复制`)
      return
    }
  } catch {
    // 继续提示手动复制
  }
  ElMessage.warning('自动复制失败，请手动选择复制')
}
</script>

<template>
  <div class="ops-page">
    <div class="ops-head">
      <div>
        <h2>服务器与实例</h2>
        <p>统一查看租户实例、服务器节点、公网探测、实时指标与未恢复告警。</p>
      </div>
      <div class="ops-actions">
        <el-button :icon="Refresh" :loading="instanceLoading" @click="refreshPage">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建实例</el-button>
      </div>
    </div>

    <el-tabs v-model="activePageTab" class="page-tabs">
      <el-tab-pane label="服务器" name="servers">
    <!-- 实例列表 -->
    <el-card shadow="never" class="page-card">
      <div class="filter-bar">
        <el-input
          v-model="instanceQuery.q"
          placeholder="实例名称 / 域名"
          clearable
          style="width: 240px"
          @keyup.enter="searchInstances()"
        />
        <el-select
          v-model="instanceQuery.status"
          placeholder="实例状态"
          clearable
          style="width: 150px"
          @change="searchInstances()"
        >
          <el-option label="待接入" value="pending" />
          <el-option label="运行中" value="active" />
          <el-option label="已暂停" value="suspended" />
        </el-select>
        <el-select
          v-model="instanceQuery.deployment_mode"
          placeholder="部署模式"
          clearable
          style="width: 145px"
          @change="searchInstances()"
        >
          <el-option label="共享中心" value="shared" />
          <el-option label="独立部署" value="independent" />
        </el-select>
        <el-select
          v-model="instanceQuery.migration_status"
          placeholder="迁移状态"
          clearable
          style="width: 145px"
          @change="searchInstances()"
        >
          <el-option label="已登记" value="registered" />
          <el-option label="迁移中" value="migrating" />
          <el-option label="已迁出" value="migrated" />
          <el-option label="回滚" value="rollback" />
        </el-select>
        <el-button type="primary" :icon="Search" @click="searchInstances()">查询</el-button>
        <div class="filter-spacer" />
        <span class="text-muted">共 {{ instanceTotal }} 个实例</span>
      </div>

      <el-table
        v-loading="instanceLoading"
        :data="instanceRows"
        border
        size="small"
        row-key="id"
        class="clickable-table"
        @row-click="handleInstanceRowClick"
      >
        <el-table-column label="ID" width="110">
          <template #default="{ row }"><IdCopy :id="row.id" /></template>
        </el-table-column>
        <el-table-column label="租户" min-width="170">
          <template #default="{ row }">
            <div>{{ textOrDash(row.tenant_name) }}</div>
            <IdCopy :id="row.tenant_id" />
          </template>
        </el-table-column>
        <el-table-column prop="name" label="实例" min-width="160" show-overflow-tooltip />
        <el-table-column prop="domain" label="域名" min-width="190" show-overflow-tooltip />
        <el-table-column label="环境" width="90">
          <template #default="{ row }">{{ environmentText(row.environment) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="95">
          <template #default="{ row }">
            <el-tag :type="instanceTagType(row.status)" size="small" disable-transitions>
              {{ instanceStatusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="部署模式" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="row.deployment_mode"
              :type="deploymentModeType(row.deployment_mode)"
              size="small"
              disable-transitions
            >
              {{ deploymentModeText(row.deployment_mode) }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="迁移状态" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="row.migration_status"
              :type="migrationStatusType(row.migration_status)"
              size="small"
              disable-transitions
            >
              {{ migrationStatusText(row.migration_status) }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="目标版本" width="125">
          <template #default="{ row }">
            <span class="mono">{{ textOrDash(row.target_version) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="实际版本" width="125">
          <template #default="{ row }">
            <span class="mono">{{ actualVersionFor(row as OpsInstance) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Agent 版本" width="120">
          <template #default="{ row }">{{ textOrDash(row.agent_version) }}</template>
        </el-table-column>
        <el-table-column label="Executor 版本" width="130">
          <template #default="{ row }">{{ textOrDash(row.executor_version) }}</template>
        </el-table-column>
        <el-table-column label="DB 版本" width="150">
          <template #default="{ row }">
            <span class="mono">{{ textOrDash(row.db_version) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="升级状态" width="105">
          <template #default="{ row }">
            <el-tag
              v-if="row.upgrade_status"
              :type="upgradeStatusType(row.upgrade_status)"
              size="small"
              disable-transitions
            >
              {{ upgradeStatusText(row.upgrade_status) }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="节点" width="110">
          <template #default="{ row }">
            <span>在线 {{ row.online_node_count }} / {{ row.node_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="未恢复告警" width="115">
          <template #default="{ row }">
            <el-tag v-if="row.open_alert_count" type="danger" size="small" disable-transitions>
              {{ row.open_alert_count }}
            </el-tag>
            <span v-else class="text-muted">0</span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="330" fixed="right">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.status === 'suspended'"
              :disabled="!enableBlockedReason(row as OpsInstance)"
              :content="enableBlockedReason(row as OpsInstance)"
              placement="top"
            >
              <span class="upgrade-disabled" @click.stop>
                <el-button
                  text
                  type="success"
                  size="small"
                  :disabled="Boolean(enableBlockedReason(row as OpsInstance))"
                  :loading="enablingId === row.id"
                  @click.stop="enableInstance(row as OpsInstance)"
                >
                  启用
                </el-button>
              </span>
            </el-tooltip>
            <el-button
              v-if="canUpgrade(row as OpsInstance)"
              text
              type="warning"
              size="small"
              :loading="upgradingId === row.id"
              @click.stop="upgradeInstance(row as OpsInstance)"
            >
              升级 {{ upgradeTargetVersion(row as OpsInstance) }}
            </el-button>
            <el-tooltip
              v-else
              :content="upgradeBlockedReason(row as OpsInstance)"
              placement="top"
            >
              <span class="upgrade-disabled" @click.stop>
                <el-button text type="info" size="small" disabled>升级</el-button>
              </span>
            </el-tooltip>
            <el-button text type="primary" size="small" @click.stop="openDetail(row as OpsInstance)">详情</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无实例，请先新建实例" :image-size="72" />
        </template>
      </el-table>

      <PaginationBar
        :total="instanceTotal"
        :page="instancePage"
        :page-size="instancePageSize"
        @page-change="handleInstancePageChange"
        @size-change="handleInstanceSizeChange"
      />
    </el-card>
      </el-tab-pane>
      <el-tab-pane label="告警中心" name="alerts">
    <!-- 告警中心 -->
    <el-card shadow="never" class="page-card">
      <div class="table-toolbar">
        <div class="section-title">告警中心</div>
        <div class="toolbar-actions">
          <el-select
            v-model="alertQuery.status"
            placeholder="告警状态"
            clearable
            style="width: 150px"
            @change="searchAlerts()"
          >
            <el-option label="未恢复" value="open" />
            <el-option label="已确认" value="acknowledged" />
            <el-option label="已恢复" value="resolved" />
          </el-select>
          <el-button :icon="Refresh" @click="reloadAlerts()">刷新告警</el-button>
        </div>
      </div>

      <el-table v-loading="alertLoading" :data="alertRows" border size="small" row-key="id">
        <el-table-column label="ID" width="105">
          <template #default="{ row }"><IdCopy :id="row.id" /></template>
        </el-table-column>
        <el-table-column label="节点" min-width="150">
          <template #default="{ row }">
            <div>{{ textOrDash(row.hostname) }}</div>
            <IdCopy :id="row.node_id" />
          </template>
        </el-table-column>
        <el-table-column label="实例" width="105">
          <template #default="{ row }"><IdCopy :id="row.instance_id" /></template>
        </el-table-column>
        <el-table-column label="类型" width="120">
          <template #default="{ row }">{{ alertTypeText(row.alert_type) }}</template>
        </el-table-column>
        <el-table-column label="级别" width="85">
          <template #default="{ row }">
            <el-tag :type="alertSeverityType(row.severity)" size="small" disable-transitions>
              {{ alertSeverityText(row.severity) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="170">
          <template #default="{ row }">
            <div class="alert-status-tags">
              <el-tag :type="alertStatusType(row.status)" size="small" disable-transitions>
                {{ alertStatusText(row.status) }}
              </el-tag>
              <el-tag
                v-if="row.in_maintenance"
                type="info"
                size="small"
                disable-transitions
                :title="row.maintenance_reason || '处于维护窗口'"
              >
                维护中
              </el-tag>
              <el-tag
                v-if="row.suppressed"
                type="warning"
                size="small"
                disable-transitions
                :title="alertSuppressTitle(row as OpsAlert)"
              >
                已抑制
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="message" label="告警内容" min-width="220" show-overflow-tooltip />
        <el-table-column label="指标 / 阈值" width="130">
          <template #default="{ row }">
            <span class="mono">{{ textOrDash(row.metric_value) }} / {{ textOrDash(row.threshold) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="触发时间" width="180">
          <template #default="{ row }">
            <div>首次 {{ formatDateTime(row.first_seen_at) }}</div>
            <div class="text-muted">最近 {{ formatDateTime(row.last_seen_at) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="恢复" width="180">
          <template #default="{ row }">
            <div v-if="row.resolved_at">恢复 {{ formatDateTime(row.resolved_at) }}</div>
            <div v-else class="text-muted">未恢复</div>
            <div v-if="row.recovery_notified_at" class="text-muted">
              通知 {{ formatDateTime(row.recovery_notified_at) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-popconfirm
              v-if="row.status === 'open'"
              title="确认该告警？确认后状态将更新为已确认。"
              confirm-button-text="确认"
              cancel-button-text="取消"
              @confirm="acknowledgeAlert(row as OpsAlert)"
            >
              <template #reference>
                <el-button text type="primary" size="small" :loading="ackLoadingId === row.id">
                  确认
                </el-button>
              </template>
            </el-popconfirm>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无告警" :image-size="72" />
        </template>
      </el-table>

      <PaginationBar
        :total="alertTotal"
        :page="alertPage"
        :page-size="alertPageSize"
        @page-change="handleAlertPageChange"
        @size-change="handleAlertSizeChange"
      />
    </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>

  <!-- 新建实例 -->
  <el-dialog v-model="createVisible" title="新建实例" width="560px" destroy-on-close>
    <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-width="90px">
      <el-form-item label="租户" prop="tenant_id">
        <el-select
          v-model="createForm.tenant_id"
          filterable
          :loading="tenantLoading"
          placeholder="请选择租户"
          no-data-text="暂无可选租户"
          style="width: 100%"
        >
          <el-option
            v-for="tenant in tenantOptions"
            :key="tenant.id"
            :label="`${tenant.name}（${tenant.code}）`"
            :value="tenant.id"
          />
        </el-select>
        <div class="form-tip">每个租户一期只允许登记一个生产实例。</div>
      </el-form-item>
      <el-form-item label="实例名称" prop="name">
        <el-input v-model="createForm.name" maxlength="100" placeholder="如 华东生产环境" />
      </el-form-item>
      <el-form-item label="域名" prop="domain">
        <el-input v-model="createForm.domain" maxlength="255" placeholder="如 erp.example.com，不含 https://" />
      </el-form-item>
      <el-form-item label="环境" prop="environment">
        <el-select v-model="createForm.environment" style="width: 100%">
          <el-option label="生产环境" value="production" />
          <el-option label="测试环境" value="test" />
        </el-select>
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="createForm.notes" type="textarea" :rows="2" maxlength="500" show-word-limit />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="createVisible = false">取消</el-button>
      <el-button type="primary" :loading="createSubmitting" @click="submitCreate">保存</el-button>
    </template>
  </el-dialog>

  <!-- 实例详情 -->
  <el-drawer v-model="drawerVisible" :title="drawerTitle" size="900px" destroy-on-close>
    <div v-loading="detailLoading" class="drawer-body">
      <template v-if="currentInstance">
        <div class="section-head">
          <span>基本信息</span>
          <el-button text type="primary" size="small" @click="openEdit">编辑实例</el-button>
        </div>

        <el-descriptions :column="2" border size="small" class="detail-descriptions">
          <el-descriptions-item label="实例 ID">
            <IdCopy :id="currentInstance.id" />
          </el-descriptions-item>
          <el-descriptions-item label="租户">
            <div>{{ textOrDash(currentInstance.tenant_name) }}</div>
            <IdCopy :id="currentInstance.tenant_id" />
          </el-descriptions-item>
          <el-descriptions-item label="实例名称">{{ currentInstance.name }}</el-descriptions-item>
          <el-descriptions-item label="域名">
            <span class="mono">{{ currentInstance.domain }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="环境">
            {{ environmentText(currentInstance.environment) }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="instanceTagType(currentInstance.status)" size="small" disable-transitions>
              {{ instanceStatusText(currentInstance.status) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="实际版本">
            {{ actualVersionFor(currentInstance) }}
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">
            {{ formatDateTime(currentInstance.created_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="服务器节点">
            {{ currentInstance.node_count }} 个（在线 {{ currentInstance.online_node_count }} 个）
          </el-descriptions-item>
          <el-descriptions-item label="未恢复告警">
            <el-tag v-if="currentInstance.open_alert_count" type="danger" size="small" disable-transitions>
              {{ currentInstance.open_alert_count }}
            </el-tag>
            <span v-else class="text-muted">0</span>
          </el-descriptions-item>
          <el-descriptions-item label="备注" :span="2">
            {{ textOrDash(currentInstance.notes) }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="section-head">
          <span>部署与迁移</span>
        </div>
        <el-descriptions :column="2" border size="small" class="detail-descriptions">
          <el-descriptions-item label="部署模式">
            <el-tag
              v-if="currentInstance.deployment_mode"
              :type="deploymentModeType(currentInstance.deployment_mode)"
              size="small"
              disable-transitions
            >
              {{ deploymentModeText(currentInstance.deployment_mode) }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </el-descriptions-item>
          <el-descriptions-item label="迁移状态">
            <el-tag
              v-if="currentInstance.migration_status"
              :type="migrationStatusType(currentInstance.migration_status)"
              size="small"
              disable-transitions
            >
              {{ migrationStatusText(currentInstance.migration_status) }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="centerOperationsBlockedReason(currentInstance)"
          type="warning"
          :closable="false"
          show-icon
          :title="`${centerOperationsBlockedReason(currentInstance)}；升级与启用按钮已禁用。`"
        />

        <div class="section-head">
          <span>发布与升级</span>
        </div>
        <el-descriptions :column="2" border size="small" class="detail-descriptions">
          <el-descriptions-item label="目标版本">
            <span class="mono">{{ textOrDash(currentInstance.target_version) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="升级状态">
            <el-tag
              v-if="currentInstance.upgrade_status"
              :type="upgradeStatusType(currentInstance.upgrade_status)"
              size="small"
              disable-transitions
            >
              {{ upgradeStatusText(currentInstance.upgrade_status) }}
            </el-tag>
            <span v-else class="text-muted">-</span>
          </el-descriptions-item>
          <el-descriptions-item label="Agent 版本">
            {{ textOrDash(currentInstance.agent_version) }}
          </el-descriptions-item>
          <el-descriptions-item label="Executor 版本">
            {{ textOrDash(currentInstance.executor_version) }}
          </el-descriptions-item>
          <el-descriptions-item label="DB 版本">
            <span class="mono">{{ textOrDash(currentInstance.db_version) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="目标发布 commit">
            <span class="commit-full mono">{{ targetReleaseCommit(currentInstance) }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="section-head">
          <span>
            维护窗口
            <span class="text-muted">（{{ maintenanceWindows.length }} 个）</span>
          </span>
          <div class="section-actions">
            <el-button
              size="small"
              :icon="Refresh"
              :loading="maintenanceLoading"
              @click="loadMaintenanceWindows()"
            >
              刷新
            </el-button>
            <el-button type="primary" size="small" @click="openMaintenanceCreate">
              新建维护窗口
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="activeMaintenanceWindow"
          class="maintenance-active-alert"
          type="warning"
          :closable="false"
          show-icon
          :title="`当前处于维护窗口${activeMaintenanceWindow.reason ? `：${activeMaintenanceWindow.reason}` : ''}（至 ${formatDateTime(activeMaintenanceWindow.ends_at)}）`"
        />
        <el-table
          v-loading="maintenanceLoading"
          :data="maintenanceWindows"
          border
          size="small"
          row-key="id"
          max-height="240"
        >
          <el-table-column label="开始时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.starts_at) }}</template>
          </el-table-column>
          <el-table-column label="结束时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.ends_at) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="95">
            <template #default="{ row }">
              <el-tag
                :type="maintenanceWindowStatusType(row as OpsMaintenanceWindow)"
                size="small"
                disable-transitions
              >
                {{ maintenanceWindowStatusText(row as OpsMaintenanceWindow) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="原因" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ textOrDash(row.reason) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button
                text
                type="danger"
                size="small"
                @click="deleteMaintenanceWindow(row as OpsMaintenanceWindow)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无维护窗口" :image-size="60" />
          </template>
        </el-table>

        <div class="section-head">
          <span>
            监控分项与请求性能
            <el-tag
              v-if="monitoringComponents.length"
              :type="overallType(monitoringOverall)"
              size="small"
              disable-transitions
            >
              {{ overallText(monitoringOverall) }}
            </el-tag>
            <span v-if="monitoringCheckedAt" class="text-muted">
              检查时间 {{ formatDateTime(monitoringCheckedAt) }}
            </span>
          </span>
          <el-button
            text
            type="primary"
            size="small"
            :loading="monitoringLoading"
            @click="loadMonitoring"
          >
            刷新监控
          </el-button>
        </div>
        <template v-if="showMonitoring">
          <div v-if="monitoringComponents.length" class="monitor-grid">
            <div
              v-for="component in monitoringComponents"
              :key="component.key"
              class="monitor-card-item"
            >
              <div class="monitor-card-head">
                <span class="monitor-card-name">{{ component.label }}</span>
                <el-tag
                  :type="componentStatusType(component.status)"
                  size="small"
                  disable-transitions
                >
                  {{ componentStatusText(component.status) }}
                </el-tag>
              </div>
              <div class="monitor-card-meta">
                <span
                  v-if="component.latency_ms !== null && component.latency_ms !== undefined"
                  class="mono"
                >
                  {{ formatMs(component.latency_ms) }}
                </span>
                <span v-else-if="component.last_heartbeat_at" class="text-muted">
                  心跳 {{ formatDateTime(component.last_heartbeat_at) }}
                </span>
                <span v-else class="text-muted">-</span>
              </div>
              <div v-if="component.message" class="monitor-card-detail" :title="component.message">
                {{ component.message }}
              </div>
            </div>
          </div>
          <div v-if="monitoringDerived" class="form-tip">
            监控分项接口未返回数据，当前按节点 Docker 容器状态推导（点击「查看指标」后更新）。
          </div>

          <div class="monitor-detail-grid">
            <div class="monitor-detail-card">
              <div class="monitor-detail-title">
                业务请求耗时
                <span class="text-muted">{{ performanceWindowText(businessPerformance) }}</span>
              </div>
              <template v-if="businessPerformance">
                <el-alert
                  v-if="businessPerformance.degraded"
                  class="monitor-alert"
                  type="warning"
                  :closable="false"
                  show-icon
                  :title="performanceDegradedText(businessPerformance)"
                />
                <div class="monitor-metric-grid">
                  <div class="monitor-metric">
                    <div class="monitor-metric-label">请求量</div>
                    <div class="monitor-metric-value">
                      {{ formatCount(businessPerformance.request_count) }}
                    </div>
                  </div>
                  <div class="monitor-metric">
                    <div class="monitor-metric-label">5xx 比例</div>
                    <div
                      class="monitor-metric-value"
                      :class="{ 'is-danger': isHigh5xx(businessPerformance.rate_5xx) }"
                    >
                      {{ formatRate(businessPerformance.rate_5xx) }}
                    </div>
                    <div class="monitor-metric-sub">
                      {{ formatCount(businessPerformance.error_5xx) }} 个 5xx
                    </div>
                  </div>
                  <div class="monitor-metric">
                    <div class="monitor-metric-label">P95 耗时</div>
                    <div class="monitor-metric-value">{{ formatMs(businessPerformance.p95_ms) }}</div>
                  </div>
                  <div class="monitor-metric">
                    <div class="monitor-metric-label">P99 耗时</div>
                    <div class="monitor-metric-value">{{ formatMs(businessPerformance.p99_ms) }}</div>
                  </div>
                </div>
                <el-table
                  v-if="performanceRoutes.length"
                  :data="performanceRoutes"
                  border
                  size="small"
                  max-height="220"
                >
                  <el-table-column label="路由" min-width="220" show-overflow-tooltip>
                    <template #default="{ row }"><span class="mono">{{ row.route }}</span></template>
                  </el-table-column>
                  <el-table-column prop="method" label="方法" width="80" />
                  <el-table-column label="请求量" width="90">
                    <template #default="{ row }">{{ formatCount(row.request_count) }}</template>
                  </el-table-column>
                  <el-table-column label="5xx" width="75">
                    <template #default="{ row }">{{ formatCount(row.error_5xx) }}</template>
                  </el-table-column>
                  <el-table-column label="P95" width="95">
                    <template #default="{ row }">{{ formatMs(row.p95_ms) }}</template>
                  </el-table-column>
                  <el-table-column label="P99" width="95">
                    <template #default="{ row }">{{ formatMs(row.p99_ms) }}</template>
                  </el-table-column>
                </el-table>
                <div v-else class="text-muted">暂无路由级指标</div>
              </template>
              <div v-else class="text-muted">暂无业务请求性能数据</div>
            </div>

            <div class="monitor-detail-card">
              <div class="monitor-detail-title">公网探测耗时</div>
              <div v-if="publicProbes.length" class="probe-latency-list">
                <div
                  v-for="probe in publicProbes"
                  :key="`${probe.node_id ?? ''}-${probe.target}-${probe.domain ?? ''}`"
                  class="probe-latency-row"
                >
                  <div class="probe-latency-label">
                    <span>{{ probeLabel(probe) }}</span>
                    <el-tag :type="probeStatusType(probe)" size="small" disable-transitions>
                      {{ probeStatusText(probe) }}
                    </el-tag>
                  </div>
                  <div class="probe-latency-meta">
                    <span class="mono">{{ formatMs(probe.latency_ms) }}</span>
                    <span v-if="typeof probe.total_nodes === 'number'" class="text-muted">
                      节点 {{ probe.ok_nodes ?? 0 }}/{{ probe.total_nodes }} 正常
                    </span>
                    <span v-else-if="probe.consecutive_failures" class="text-danger">
                      连续失败 {{ probe.consecutive_failures }} 次
                    </span>
                    <span class="text-muted">{{ formatDateTime(probe.checked_at) }}</span>
                  </div>
                </div>
              </div>
              <div v-else class="text-muted">暂无公网探测数据</div>
            </div>
          </div>
        </template>
        <el-empty
          v-else-if="!monitoringLoading"
          description="后端未提供监控/性能接口，或暂无数据"
          :image-size="60"
        />

        <div class="section-head">
          <span>
            服务器节点
            <span class="text-muted">（{{ nodes.length }} 个）</span>
          </span>
          <el-button
            type="primary"
            size="small"
            :loading="enrollmentLoading"
            @click="generateEnrollment"
          >
            生成接入令牌
          </el-button>
        </div>

        <el-table :data="nodes" border size="small" row-key="id" max-height="360">
          <el-table-column label="节点" min-width="210">
            <template #default="{ row }">
              <div class="node-name">{{ textOrDash(row.hostname) }}</div>
              <div class="node-sub mono" :title="row.node_uuid">{{ row.node_uuid }}</div>
              <div class="node-id"><IdCopy :id="row.id" /></div>
            </template>
          </el-table-column>
          <el-table-column label="所属实例" width="170">
            <template #default="{ row }">
              <div>{{ textOrDash(currentInstance.name) }}</div>
              <IdCopy :id="row.instance_id" />
            </template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="nodeTagType(row.status)" size="small" disable-transitions>
                {{ nodeStatusText(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="配置" width="150">
            <template #default="{ row }">{{ nodeSpecText(row as OpsServerNode) }}</template>
          </el-table-column>
          <el-table-column label="网络" min-width="180">
            <template #default="{ row }">
              <div>{{ row.internal_ips?.length ? row.internal_ips.join(', ') : '-' }}</div>
              <div v-if="row.observed_public_ip || row.configured_public_ip" class="text-muted">
                公网 {{ row.observed_public_ip || row.configured_public_ip }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="运行版本" width="205">
            <template #default="{ row }">
              <div>Agent {{ textOrDash(row.agent_version) }}</div>
              <div class="text-muted">
                Executor {{ textOrDash(row.executor_version) }} · 协议 {{ row.protocol_version ?? '-' }}
              </div>
              <div class="text-muted">应用 {{ textOrDash(row.actual_version || row.app_version) }}</div>
              <div class="text-muted">DB {{ textOrDash(row.db_version) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="最近指标" min-width="150">
            <template #default="{ row }">{{ nodeMetricText(row as OpsServerNode) }}</template>
          </el-table-column>
          <el-table-column label="最后心跳" width="170">
            <template #default="{ row }">{{ formatDateTime(row.last_seen_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="240" fixed="right">
            <template #default="{ row }">
              <el-button
                text
                type="primary"
                size="small"
                :loading="probingNodeId === row.id"
                @click.stop="probeNode(row as OpsServerNode)"
              >
                立即探测
              </el-button>
              <el-button text type="primary" size="small" @click.stop="loadNodeMetrics(row as OpsServerNode)">
                查看指标
              </el-button>
              <el-button text type="primary" size="small" @click.stop="openSsh(row as OpsServerNode)">
                SSH
              </el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无节点，生成接入令牌后按命令安装 Agent" :image-size="60" />
          </template>
        </el-table>

        <div class="section-head metrics-head">
          <span>
            节点指标
            <span v-if="selectedNode" class="text-muted">
              · {{ selectedNode.hostname || selectedNode.node_uuid }} · 最近 {{ metricsMinutes }} 分钟
            </span>
          </span>
          <el-button
            v-if="selectedNode"
            text
            type="primary"
            size="small"
            :loading="metricsLoading"
            @click="loadNodeMetrics(selectedNode)"
          >
            刷新指标
          </el-button>
        </div>

        <el-empty
          v-if="!selectedNode"
          description="点击节点行的「查看指标」加载最近 60 分钟样本"
          :image-size="60"
        />
        <div v-else v-loading="metricsLoading" class="metrics-body">
          <template v-if="latestMetric">
            <div class="metric-grid">
              <div class="metric-card">
                <div class="metric-label">CPU 使用率</div>
                <div class="metric-value" :class="{ 'is-danger': Number(latestMetric.cpu_percent) >= 85 }">
                  {{ percentText(latestMetric.cpu_percent) }}
                </div>
              </div>
              <div class="metric-card">
                <div class="metric-label">内存使用率</div>
                <div class="metric-value" :class="{ 'is-danger': Number(latestMetric.memory_percent) >= 90 }">
                  {{ percentText(latestMetric.memory_percent) }}
                </div>
                <div class="metric-sub">
                  {{ latestMetric.memory_used_mb }} / {{ latestMetric.memory_total_mb }} MB
                </div>
              </div>
              <div class="metric-card">
                <div class="metric-label">1 分钟负载</div>
                <div class="metric-value">{{ numberText(latestMetric.load1) }}</div>
                <div class="metric-sub">
                  {{ numberText(latestMetric.load5) }} / {{ numberText(latestMetric.load15) }}（5m / 15m）
                </div>
              </div>
              <div class="metric-card">
                <div class="metric-label">磁盘最高使用率</div>
                <div
                  class="metric-value"
                  :class="{ 'is-danger': latestDiskPercent !== null && Number(latestDiskPercent) >= 85 }"
                >
                  {{ percentText(latestDiskPercent) }}
                </div>
                <div class="metric-sub">
                  {{ selectedNode.latest_metric ? '节点最近上报' : '本次样本' }}
                </div>
              </div>
              <div v-if="latestDataDisk" class="metric-card">
                <div class="metric-label">数据盘使用率</div>
                <div
                  class="metric-value"
                  :class="{ 'is-danger': Number(latestDataDisk.percent ?? 0) >= 85 }"
                >
                  {{ percentText(latestDataDisk.percent) }}
                </div>
                <div class="metric-sub">
                  {{ latestDataDisk.mount || '/' }} ·
                  {{ numberText(latestDataDisk.used_gb, 1) }} / {{ numberText(latestDataDisk.total_gb, 1) }} GB
                </div>
              </div>
              <div v-if="networkRate" class="metric-card">
                <div class="metric-label">网络入站速率</div>
                <div class="metric-value">{{ formatThroughput(networkRate.rx) }}</div>
                <div class="metric-sub">
                  出站 {{ formatThroughput(networkRate.tx) }}
                  <span v-if="networkRate.interval"> · {{ networkRate.interval }}s</span>
                </div>
              </div>
            </div>
            <div class="metric-meta">
              采样时间 {{ formatDateTime(latestMetric.sampled_at) }}
              <span v-if="latestMetric.public_ip"> · 公网 IP {{ latestMetric.public_ip }}</span>
            </div>

            <div class="trend-panel">
              <div class="trend-head">
                <div>
                  <div class="trend-title">CPU / 内存平均趋势</div>
                  <div class="trend-subtitle">
                    {{ trendGranularityText }}聚合 · {{ activeTrendRange.label }}
                  </div>
                </div>
                <el-radio-group
                  :model-value="trendRangeKey"
                  size="small"
                  class="trend-range-switch"
                  @change="handleTrendRangeChange"
                >
                  <el-radio-button
                    v-for="range in TREND_RANGES"
                    :key="range.key"
                    :value="range.key"
                  >
                    {{ range.label }}
                  </el-radio-button>
                </el-radio-group>
              </div>

              <div class="trend-legend">
                <span class="trend-legend-item">
                  <span class="trend-swatch trend-swatch-cpu"></span>
                  CPU 平均
                </span>
                <span class="trend-legend-item">
                  <span class="trend-swatch trend-swatch-memory"></span>
                  内存平均
                </span>
              </div>

              <div v-loading="aggregatesLoading" class="trend-chart-wrap">
                <el-empty
                  v-if="!aggregatesLoading && !trendPoints.length"
                  :description="`${activeTrendRange.label}暂无聚合数据`"
                  :image-size="60"
                />
                <template v-else-if="trendPoints.length">
                  <el-alert
                    v-if="trendNotice"
                    class="trend-notice"
                    type="warning"
                    :title="trendNotice"
                    :closable="false"
                    show-icon
                  />
                  <svg
                    class="trend-svg"
                    :viewBox="CHART_VIEW_BOX"
                    role="img"
                    :aria-label="'CPU 与内存平均使用率趋势，' + activeTrendRange.label"
                    preserveAspectRatio="xMidYMid meet"
                  >
                    <title>CPU 与内存平均使用率趋势，{{ activeTrendRange.label }}</title>
                    <g class="trend-grid">
                      <line
                        v-for="tick in trendChart.yTicks"
                        :key="`grid-${tick.value}`"
                        :x1="CHART_LEFT"
                        :x2="CHART_WIDTH - CHART_RIGHT"
                        :y1="tick.y"
                        :y2="tick.y"
                      />
                    </g>
                    <g class="trend-y-axis">
                      <text
                        v-for="tick in trendChart.yTicks"
                        :key="`y-${tick.value}`"
                        :x="CHART_LEFT - 6"
                        :y="tick.y + 3"
                        text-anchor="end"
                      >
                        {{ tick.value }}%
                      </text>
                    </g>
                    <g class="trend-x-axis">
                      <text
                        v-for="tick in trendChart.xTicks"
                        :key="`x-${tick.x}-${tick.label}`"
                        :x="tick.x"
                        :y="tick.y"
                        :text-anchor="tick.anchor"
                      >
                        {{ tick.label }}
                      </text>
                    </g>
                    <path
                      v-if="trendPoints.length > 1"
                      class="trend-line trend-cpu-line"
                      :d="trendChart.cpuPath"
                    />
                    <path
                      v-if="trendPoints.length > 1"
                      class="trend-line trend-memory-line"
                      :d="trendChart.memoryPath"
                    />
                    <g class="trend-dots">
                      <circle
                        v-for="dot in trendChart.dots"
                        :key="`cpu-dot-${dot.timestamp}`"
                        class="trend-dot trend-cpu-dot"
                        :cx="dot.x"
                        :cy="dot.cpuY"
                        r="2.5"
                      />
                      <circle
                        v-for="dot in trendChart.dots"
                        :key="`memory-dot-${dot.timestamp}`"
                        class="trend-dot trend-memory-dot"
                        :cx="dot.x"
                        :cy="dot.memoryY"
                        r="2.5"
                      />
                    </g>
                  </svg>
                  <div class="trend-foot">
                    <span>{{ trendRangeText }}</span>
                    <span>
                      共 {{ trendPoints.length }} 个聚合桶 / {{ trendTotalSamples }} 个样本
                      <span v-if="trendPoints.length < trendExpectedBuckets" class="text-muted">
                        （预期 {{ trendExpectedBuckets }} 桶）
                      </span>
                    </span>
                  </div>
                </template>
              </div>
            </div>

            <template v-if="metricRows.length">
              <div class="metric-list-head">最近样本（新 → 旧，最多 30 条）</div>
              <el-table :data="metricRows" border size="small" max-height="280">
                <el-table-column label="采样时间" width="170">
                  <template #default="{ row }">{{ formatDateTime(row.sampled_at) }}</template>
                </el-table-column>
                <el-table-column label="CPU" width="90">
                  <template #default="{ row }">{{ percentText(row.cpu_percent) }}</template>
                </el-table-column>
                <el-table-column label="内存" width="145">
                  <template #default="{ row }">
                    <div>{{ percentText(row.memory_percent) }}</div>
                    <div class="text-muted">
                      {{ row.memory_used_mb }} / {{ row.memory_total_mb }} MB
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="负载 1 / 5 / 15" width="160">
                  <template #default="{ row }">
                    {{ numberText(row.load1) }} / {{ numberText(row.load5) }} /
                    {{ numberText(row.load15) }}
                  </template>
                </el-table-column>
                <el-table-column label="磁盘" min-width="150">
                  <template #default="{ row }">{{ diskSummary(row as OpsNodeMetric) }}</template>
                </el-table-column>
              </el-table>
            </template>
            <el-empty
              v-else-if="!metricsLoading"
              description="最近 60 分钟暂无历史样本"
              :image-size="60"
            />
          </template>
          <el-empty
            v-else-if="!metricsLoading"
            description="暂无指标样本，Agent 上报后将自动展示"
            :image-size="60"
          />
        </div>
      </template>
      <el-empty v-else description="实例不存在或加载失败" />
    </div>
  </el-drawer>

  <!-- 编辑实例 -->
  <el-dialog v-model="editVisible" title="编辑实例" width="560px" destroy-on-close>
    <el-form ref="editFormRef" :model="editForm" :rules="editRules" label-width="90px">
      <el-form-item label="实例名称" prop="name">
        <el-input v-model="editForm.name" maxlength="100" />
      </el-form-item>
      <el-form-item label="域名" prop="domain">
        <el-input v-model="editForm.domain" maxlength="255" placeholder="不含 https://" />
      </el-form-item>
      <el-form-item label="状态" prop="status">
        <el-select v-model="editForm.status" style="width: 100%">
          <el-option label="待接入" value="pending" />
          <el-option label="运行中" value="active" />
          <el-option label="已暂停" value="suspended" />
        </el-select>
      </el-form-item>
      <el-form-item label="应用版本">
        <el-input v-model="editForm.app_version" maxlength="64" placeholder="如 v1.2.3" />
      </el-form-item>
      <el-form-item label="备注">
        <el-input v-model="editForm.notes" type="textarea" :rows="2" maxlength="500" show-word-limit />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="editVisible = false">取消</el-button>
      <el-button type="primary" :loading="editSubmitting" @click="submitEdit">保存</el-button>
    </template>
  </el-dialog>

  <!-- 新建维护窗口 -->
  <el-dialog v-model="maintenanceDialogVisible" title="新建维护窗口" width="560px" destroy-on-close>
    <el-form
      ref="maintenanceWindowFormRef"
      :model="maintenanceWindowForm"
      :rules="maintenanceRules"
      label-width="90px"
    >
      <el-form-item label="开始时间" prop="starts_at">
        <el-date-picker
          v-model="maintenanceWindowForm.starts_at"
          type="datetime"
          format="YYYY-MM-DD HH:mm"
          value-format="YYYY-MM-DDTHH:mm:ssZ"
          placeholder="选择开始时间"
          style="width: 100%"
        />
      </el-form-item>
      <el-form-item label="结束时间" prop="ends_at">
        <el-date-picker
          v-model="maintenanceWindowForm.ends_at"
          type="datetime"
          format="YYYY-MM-DD HH:mm"
          value-format="YYYY-MM-DDTHH:mm:ssZ"
          placeholder="选择结束时间"
          style="width: 100%"
        />
      </el-form-item>
      <el-form-item label="维护原因">
        <el-input
          v-model="maintenanceWindowForm.reason"
          maxlength="255"
          placeholder="如 版本升级 / 数据库维护"
        />
      </el-form-item>
      <div class="form-tip">
        维护窗口内暂停告警邮件，告警列表会标记「维护中」；升级等变更任务不受影响。
      </div>
    </el-form>
    <template #footer>
      <el-button @click="maintenanceDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="maintenanceSubmitting" @click="submitMaintenanceWindow">
        保存
      </el-button>
    </template>
  </el-dialog>

  <!-- 一次性接入令牌 -->
  <el-dialog v-model="enrollmentVisible" title="服务器接入令牌" width="680px" destroy-on-close>
    <template v-if="enrollment">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="令牌仅显示一次，请立即复制并妥善保管；有效期约 30 分钟，过期后需重新生成。"
      />
      <el-descriptions :column="1" border size="small" class="enrollment-detail">
        <el-descriptions-item label="节点 UUID">
          <IdCopy :id="enrollment.node_uuid" />
        </el-descriptions-item>
        <el-descriptions-item label="过期时间">
          {{ formatDateTime(enrollment.expires_at) }}（约 {{ enrollmentMinutes }} 分钟）
        </el-descriptions-item>
        <el-descriptions-item label="接入令牌">
          <div class="copy-row">
            <el-input :model-value="enrollment.enrollment_token" readonly class="copy-input" />
            <el-button :icon="CopyDocument" @click="copyText(enrollment.enrollment_token, '接入令牌')">
              复制
            </el-button>
          </div>
        </el-descriptions-item>
        <el-descriptions-item label="安装命令">
          <div class="install-command">{{ enrollment.install_command }}</div>
          <el-button
            :icon="CopyDocument"
            size="small"
            @click="copyText(enrollment.install_command, '安装命令')"
          >
            复制命令
          </el-button>
        </el-descriptions-item>
      </el-descriptions>
    </template>
    <template #footer>
      <el-button @click="enrollmentVisible = false">关闭</el-button>
    </template>
  </el-dialog>

  <!-- 公网探测结果 -->
  <el-dialog v-model="probeVisible" title="节点探测结果" width="820px" destroy-on-close>
    <template v-if="probeResult">
      <el-alert :type="probeAllOk ? 'success' : 'error'" :title="probeSummary" :closable="false" show-icon />
      <el-alert
        v-if="probeApiFailed"
        class="probe-api-alert"
        type="error"
        :closable="false"
        show-icon
        :title="`API 探测连续失败 ${probeApiFailures} 次；首页成功不会消除 API 告警。`"
      />
      <div class="probe-domain text-muted">探测域名：<span class="mono">{{ probeResult.domain }}</span></div>
      <el-table :data="probeResult.checks" border size="small">
        <el-table-column label="目标" width="120">
          <template #default="{ row }">{{ probeTargetText(row.target) }}</template>
        </el-table-column>
        <el-table-column label="URL" min-width="200" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.url }}</span></template>
        </el-table-column>
        <el-table-column label="结果" width="80">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small" disable-transitions>
              {{ row.ok ? '通过' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态码" width="80">
          <template #default="{ row }">{{ row.status_code ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">{{ row.latency_ms }} ms</template>
        </el-table-column>
        <el-table-column label="连续失败" width="90">
          <template #default="{ row }">{{ probeTargetConsecutiveFailures(row.target) }}</template>
        </el-table-column>
        <el-table-column label="错误" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ textOrDash(row.error) }}</template>
        </el-table-column>
        <el-table-column label="检测时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.checked_at) }}</template>
        </el-table-column>
      </el-table>

      <div class="section-head probe-latency-head">近 24 小时探测耗时</div>
      <div v-loading="probeLatencyLoading">
        <el-table v-if="probeLatency?.targets?.length" :data="probeLatency.targets" border size="small">
          <el-table-column label="目标" width="120">
            <template #default="{ row }">{{ probeTargetText(row.target) }}</template>
          </el-table-column>
          <el-table-column label="样本数" width="85">
            <template #default="{ row }">{{ formatCount(row.samples) }}</template>
          </el-table-column>
          <el-table-column label="成功率" width="90">
            <template #default="{ row }">{{ formatRate(row.ok_rate) }}</template>
          </el-table-column>
          <el-table-column label="平均" width="95">
            <template #default="{ row }">{{ formatMs(row.avg_ms) }}</template>
          </el-table-column>
          <el-table-column label="P95" width="95">
            <template #default="{ row }">{{ formatMs(row.p95_ms) }}</template>
          </el-table-column>
          <el-table-column label="最大" width="95">
            <template #default="{ row }">{{ formatMs(row.max_ms) }}</template>
          </el-table-column>
          <el-table-column label="最近" width="95">
            <template #default="{ row }">{{ formatMs(row.last_latency_ms) }}</template>
          </el-table-column>
          <el-table-column label="最后检测" width="170">
            <template #default="{ row }">{{ formatDateTime(row.last_checked_at) }}</template>
          </el-table-column>
        </el-table>
        <el-empty
          v-else-if="!probeLatencyLoading"
          description="暂无探测耗时聚合数据"
          :image-size="50"
        />
      </div>
      <div v-if="businessPerformance" class="probe-compare">
        业务请求耗时：P95 {{ formatMs(businessPerformance?.p95_ms) }} / P99
        {{ formatMs(businessPerformance?.p99_ms) }}（{{ performanceWindowText(businessPerformance) }}）；
        公网探测耗时见上表。
      </div>
    </template>
    <template #footer>
      <el-button @click="probeVisible = false">关闭</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="sshVisible" :title="`SSH · ${sshNode?.hostname || sshNode?.node_uuid || ''}`" width="760px" destroy-on-close>
    <div v-loading="sshLoading">
      <el-alert
        v-if="sshFingerprintWarning"
        class="ssh-fingerprint-alert"
        type="error"
        :title="sshFingerprintWarning"
        :closable="false"
        show-icon
      />
      <el-descriptions v-if="sshConfig" :column="2" border size="small" class="ssh-fingerprint">
        <el-descriptions-item label="连接目标">
          <span class="mono">{{ textOrDash(sshConfig.host) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="探测 IP">
          <span class="mono">{{ textOrDash(sshConfig.detected_host) }}</span>
          <span class="form-tip">（仅展示，不用于连接）</span>
        </el-descriptions-item>
        <el-descriptions-item label="指纹状态">
          <el-tag
            v-if="sshConfig.fingerprint_status"
            :type="sshFingerprintStatusType(sshConfig.fingerprint_status)"
            size="small"
            disable-transitions
          >
            {{ sshFingerprintStatusText(sshConfig.fingerprint_status) }}
          </el-tag>
          <span v-else class="text-muted">-</span>
        </el-descriptions-item>
        <el-descriptions-item label="指纹确认时间">
          {{ formatDateTime(sshConfig.ssh_host_fingerprint_verified_at) }}
        </el-descriptions-item>
        <el-descriptions-item label="算法">
          {{ textOrDash(sshConfig.ssh_fingerprint_algorithm) }}
        </el-descriptions-item>
        <el-descriptions-item label="主机指纹" :span="2">
          <span v-if="sshConfig.ssh_host_fingerprint" class="ssh-fingerprint-value mono">
            {{ sshConfig.ssh_host_fingerprint }}
          </span>
          <span v-else class="text-muted">未记录（首次成功连接后自动记录并验证）</span>
        </el-descriptions-item>
        <el-descriptions-item label="指纹操作" :span="2">
          <el-button
            size="small"
            type="warning"
            :loading="sshFingerprintResetting"
            :disabled="!sshConfig.ssh_host_fingerprint"
            @click="resetSshFingerprint"
          >
            重置主机指纹
          </el-button>
          <span class="form-tip">仅限确认服务器主机密钥变更后操作；重置后下次连接重新记录。</span>
        </el-descriptions-item>
      </el-descriptions>
      <el-form label-width="90px" size="small">
        <el-form-item label="SSH 地址">
          <el-input v-model="sshHost" placeholder="留空表示禁用 SSH；不会自动使用 Agent 上报 IP" />
        </el-form-item>
        <el-form-item label="端口">
          <el-input-number v-model="sshPort" :min="1" :max="65535" controls-position="right" />
        </el-form-item>
        <el-form-item label="用户名">
          <el-input v-model="sshUsername" autocomplete="off" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="sshPassword" type="password" show-password autocomplete="new-password" placeholder="留空表示不修改" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="sshSaving" @click="saveSsh">保存 SSH 配置</el-button>
          <el-button :disabled="!sshNode" @click="openTerminal">打开交互终端</el-button>
        </el-form-item>
      </el-form>
      <el-divider content-position="left">执行命令</el-divider>
      <el-input v-model="sshCommand" type="textarea" :rows="3" placeholder="例如：df -h" />
      <div class="ssh-actions">
        <el-button type="primary" :loading="sshRunning" :disabled="!sshCommand.trim()" @click="runSsh">执行</el-button>
      </div>
      <pre class="ssh-output">{{ sshOutput || '命令输出会显示在这里' }}</pre>
    </div>
  </el-dialog>

  <el-dialog
    v-model="terminalVisible"
    :title="`SSH 终端 · ${sshNode?.hostname || sshNode?.node_uuid || ''}`"
    width="92vw"
    top="3vh"
    destroy-on-close
    @closed="closeTerminal"
  >
    <el-alert
      v-if="terminalFingerprintWarning"
      class="ssh-fingerprint-alert"
      type="error"
      :title="terminalFingerprintWarning"
      :closable="false"
      show-icon
    />
    <div ref="terminalHost" class="ssh-terminal" />
  </el-dialog>
</template>

<style scoped>
.ops-page {
  min-width: 0;
}

.ops-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

.ops-head h2 {
  margin: 0;
  font-size: 20px;
  line-height: 1.4;
}

.ops-head p {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.ops-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.filter-spacer {
  flex: 1;
}

.section-title {
  font-size: 15px;
  font-weight: 600;
}

.clickable-table :deep(.el-table__row) {
  cursor: pointer;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 16px 0 8px;
  font-weight: 600;
}

.section-head .text-muted {
  font-weight: 400;
}

.metrics-head {
  margin-top: 20px;
}

.node-name {
  font-weight: 600;
}

.node-sub {
  overflow: hidden;
  color: var(--el-text-color-secondary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-id {
  margin-top: 2px;
}

.commit-full {
  display: inline-block;
  max-width: 100%;
  line-height: 1.5;
  white-space: normal;
  word-break: break-all;
  font-size: 11px;
}

.upgrade-disabled {
  display: inline-flex;
  margin-right: 12px;
  vertical-align: middle;
}

.drawer-body {
  min-height: 200px;
}

.metrics-body {
  min-height: 120px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.metric-card {
  min-width: 0;
  padding: 10px 12px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.metric-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.metric-value {
  margin-top: 6px;
  font-size: 22px;
  font-weight: 600;
  line-height: 1.2;
}

.metric-value.is-danger {
  color: var(--el-color-danger);
}

.metric-sub {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 11px;
}

.section-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.maintenance-active-alert {
  margin-bottom: 8px;
}

.monitor-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 8px;
  margin-bottom: 10px;
}

.monitor-card-item {
  min-width: 0;
  padding: 10px 12px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.monitor-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.monitor-card-name {
  overflow: hidden;
  font-size: 13px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.monitor-card-meta {
  margin-top: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.monitor-card-detail {
  margin-top: 4px;
  overflow: hidden;
  color: var(--el-text-color-secondary);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.monitor-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.monitor-detail-card {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.monitor-detail-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
}

.monitor-detail-title .text-muted {
  font-weight: 400;
}

.monitor-metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 8px;
}

.monitor-metric {
  padding: 8px 10px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.monitor-metric-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.monitor-metric-value {
  margin-top: 2px;
  font-size: 18px;
  font-weight: 600;
  line-height: 1.2;
}

.monitor-metric-value.is-danger {
  color: var(--el-color-danger);
}

.monitor-metric-sub {
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: 11px;
}

.monitor-alert {
  margin-bottom: 8px;
}

.probe-latency-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.probe-latency-row {
  padding: 8px 10px;
  background: var(--el-fill-color-lighter);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}

.probe-latency-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
}

.probe-latency-meta {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.text-danger {
  color: var(--el-color-danger);
}

.alert-status-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.probe-api-alert {
  margin-top: 8px;
}

.probe-latency-head {
  margin-top: 12px;
}

.probe-compare {
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.metric-meta {
  margin: 8px 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.trend-panel {
  margin: 12px 0 16px;
  padding: 12px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.trend-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.trend-title {
  font-size: 13px;
  font-weight: 600;
}

.trend-subtitle {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 11px;
}

.trend-legend {
  display: flex;
  align-items: center;
  gap: 16px;
  margin: 10px 0 4px;
  color: var(--el-text-color-regular);
  font-size: 12px;
}

.trend-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.trend-swatch {
  display: inline-block;
  width: 18px;
  height: 2px;
  border-radius: 1px;
}

.trend-swatch-cpu {
  background: var(--el-color-primary);
}

.trend-swatch-memory {
  background: var(--el-color-warning);
}

.trend-chart-wrap {
  min-height: 150px;
}

.trend-notice {
  margin-bottom: 8px;
}

.trend-svg {
  display: block;
  width: 100%;
  height: auto;
}

.trend-grid line {
  stroke: var(--el-border-color-lighter);
  stroke-width: 1;
}

.trend-y-axis text,
.trend-x-axis text {
  fill: var(--el-text-color-secondary);
  font-size: 10px;
}

.trend-line {
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 2;
}

.trend-cpu-line {
  stroke: var(--el-color-primary);
}

.trend-memory-line {
  stroke: var(--el-color-warning);
}

.trend-dot {
  stroke: var(--el-bg-color);
  stroke-width: 1;
}

.trend-cpu-dot {
  fill: var(--el-color-primary);
}

.trend-memory-dot {
  fill: var(--el-color-warning);
}

.trend-foot {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.metric-list-head {
  margin: 4px 0 8px;
  font-size: 12px;
  font-weight: 600;
}

.copy-row {
  display: flex;
  width: 100%;
  gap: 8px;
}

.copy-input {
  flex: 1;
}

.install-command {
  width: 100%;
  margin-bottom: 8px;
  padding: 8px 10px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  font-family: Consolas, 'SFMono-Regular', Menlo, monospace;
  font-size: 12px;
}

.enrollment-detail {
  margin-top: 12px;
}

.ssh-fingerprint-alert {
  margin-bottom: 12px;
}

.ssh-fingerprint {
  margin-bottom: 12px;
}

.ssh-fingerprint-value {
  display: block;
  overflow-wrap: anywhere;
  word-break: break-all;
  font-size: 12px;
}

.ssh-fingerprint .form-tip {
  display: block;
  margin-top: 4px;
}

.ssh-terminal {
  width: 100%;
  height: 72vh;
  padding: 8px;
  background: #1f2430;
  border-radius: 4px;
}

.ssh-terminal :deep(.xterm) {
  height: 100%;
}

.ssh-actions {
  margin: 10px 0;
}

.ssh-output {
  max-height: 320px;
  padding: 10px;
  overflow: auto;
  white-space: pre-wrap;
  background: #1f2430;
  color: #d8dee9;
  border-radius: 4px;
  font-family: Consolas, 'SFMono-Regular', Menlo, monospace;
  font-size: 12px;
}

.probe-domain {
  margin: 10px 0;
  font-size: 12px;
}

@media (max-width: 900px) {
  .ops-head {
    flex-direction: column;
  }

  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .trend-head {
    flex-direction: column;
  }

  .monitor-detail-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .monitor-metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
