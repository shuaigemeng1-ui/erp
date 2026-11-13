<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { CopyDocument, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { opsApi } from '@/api/ops'
import type { OpsRelease } from '@/api/ops'
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
  OpsNodeAggregates,
  OpsNodeEnrollment,
  OpsNodeMetric,
  OpsNodeMetrics,
  OpsProbeResult,
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
    if (selectedNode.value) {
      selectedNode.value = nodeList.find((node) => node.id === selectedNode.value?.id) ?? selectedNode.value
    }
  } catch {
    // 统一请求层已经展示错误，这里保留当前已加载内容
  } finally {
    if (seq === detailRequestSeq) detailLoading.value = false
  }
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
const sshHost = ref('')
const sshPort = ref(22)
const sshUsername = ref('')
const sshPassword = ref('')
const sshCommand = ref('')
const sshOutput = ref('')

async function openSsh(node: OpsServerNode): Promise<void> {
  sshNode.value = node
  sshVisible.value = true
  sshLoading.value = true
  sshOutput.value = ''
  sshCommand.value = ''
  try {
    const config = await opsApi.nodeSshConfig(node.id)
    sshHost.value = config.host || config.detected_host || ''
    sshPort.value = Number(config.port || 22)
    sshUsername.value = config.username || ''
    sshPassword.value = ''
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
    sshPassword.value = ''
    ElMessage.success(config.has_password ? 'SSH 配置已保存' : 'SSH 配置已保存，但密码为空')
  } finally {
    sshSaving.value = false
  }
}

async function runSsh(): Promise<void> {
  if (!sshNode.value || !sshCommand.value.trim()) return
  sshRunning.value = true
  sshOutput.value = ''
  try {
    const result = await opsApi.execNodeSsh(sshNode.value.id, sshCommand.value.trim())
    sshOutput.value = `$ ${sshCommand.value.trim()}\n${result.output}\n[exit ${result.exit_status}]`
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
function upgradeBlockedReason(instance: OpsInstance): string {
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
}

onMounted(loadReleases)

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
const probeAllOk = computed(() => {
  const checks = probeResult.value?.checks ?? []
  return checks.length > 0 && checks.every((check) => check.ok)
})
const probeSummary = computed(() => {
  const result = probeResult.value
  if (!result) return ''
  if (!result.checks.length) return '探测未返回检查项'
  return probeAllOk.value
    ? `公网访问正常，${result.checks.length} 项检查全部通过`
    : '公网访问异常，请检查域名解析、证书与服务状态'
})

async function probeNode(node: OpsServerNode): Promise<void> {
  probingNodeId.value = node.id
  try {
    probeResult.value = await opsApi.probeNode(node.id)
    probeVisible.value = true
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
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
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
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="alertStatusType(row.status)" size="small" disable-transitions>
              {{ alertStatusText(row.status) }}
            </el-tag>
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
  <el-dialog v-model="probeVisible" title="节点探测结果" width="780px" destroy-on-close>
    <template v-if="probeResult">
      <el-alert :type="probeAllOk ? 'success' : 'error'" :title="probeSummary" :closable="false" show-icon />
      <div class="probe-domain text-muted">探测域名：<span class="mono">{{ probeResult.domain }}</span></div>
      <el-table :data="probeResult.checks" border size="small">
        <el-table-column label="目标" prop="target" width="90" />
        <el-table-column label="URL" min-width="220" show-overflow-tooltip>
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
        <el-table-column label="错误" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">{{ textOrDash(row.error) }}</template>
        </el-table-column>
        <el-table-column label="检测时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.checked_at) }}</template>
        </el-table-column>
      </el-table>
    </template>
    <template #footer>
      <el-button @click="probeVisible = false">关闭</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="sshVisible" :title="`SSH · ${sshNode?.hostname || sshNode?.node_uuid || ''}`" width="760px" destroy-on-close>
    <div v-loading="sshLoading">
      <el-form label-width="90px" size="small">
        <el-form-item label="SSH 地址">
          <el-input v-model="sshHost" placeholder="留空自动使用探测到的公网/内网 IP" />
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
}
</style>
