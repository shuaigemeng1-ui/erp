<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { CopyDocument, Plus, Refresh, Search } from '@element-plus/icons-vue'
import { opsApi } from '@/api/ops'
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

async function refreshPage(): Promise<void> {
  if (drawerVisible.value && currentInstance.value) {
    await loadDetail(currentInstance.value.id)
  }
  await Promise.all([reloadInstances(), reloadAlerts()])
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

const latestMetric = computed<OpsNodeMetric | null>(
  () => metricsData.value?.latest ?? selectedNode.value?.latest_metric ?? null,
)
const latestDiskPercent = computed(() => maxDiskPercent(latestMetric.value))
const metricRows = computed<OpsNodeMetric[]>(() => {
  const items = metricsData.value?.items ?? []
  return items.slice().reverse().slice(0, 30)
})

async function loadNodeMetrics(node: OpsServerNode): Promise<void> {
  const seq = ++metricsRequestSeq
  selectedNode.value = node
  metricsLoading.value = true
  metricsData.value = null
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
        <el-table-column label="版本" width="110">
          <template #default="{ row }">{{ textOrDash(row.app_version) }}</template>
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
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
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
          <el-descriptions-item label="应用版本">
            {{ textOrDash(currentInstance.app_version) }}
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
          <el-table-column label="版本" width="130">
            <template #default="{ row }">
              <div>Agent {{ textOrDash(row.agent_version) }}</div>
              <div class="text-muted">应用 {{ textOrDash(row.app_version) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="最近指标" min-width="150">
            <template #default="{ row }">{{ nodeMetricText(row as OpsServerNode) }}</template>
          </el-table-column>
          <el-table-column label="最后心跳" width="170">
            <template #default="{ row }">{{ formatDateTime(row.last_seen_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="160" fixed="right">
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
}
</style>
