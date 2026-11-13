<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { opsApi } from '@/api/ops'
import type {
  OpsBackup,
  OpsBuildStatus,
  OpsJobType,
  OpsOperation,
  OpsRelease,
  OpsReleaseStatus,
} from '@/api/ops'
import { useTableQuery } from '@/composables/useTableQuery'
import PaginationBar from '@/components/PaginationBar.vue'
import IdCopy from '@/components/IdCopy.vue'
import { formatDateTime, parseJsonObject, stringOrUndefined, textOrDash } from '@/utils/format'
import type { OpsInstance } from '@/types/ops'

type TagType = 'success' | 'warning' | 'info' | 'danger'
type OpsCenterTab = 'releases' | 'operations' | 'backups'

const TAB_LABELS: Record<OpsCenterTab, string> = {
  releases: '发布版本',
  operations: '运维任务',
  backups: '备份记录',
}

const JOB_TYPE_LABELS: Record<string, string> = {
  deploy: '部署',
  upgrade: '升级',
  restart: '重启服务',
  backup: '备份',
  logs: '查看日志',
  config: '更新配置',
}

const JOB_TYPE_OPTIONS: Array<{ value: OpsJobType; label: string }> = [
  { value: 'deploy', label: '部署（拉取镜像并更新服务）' },
  { value: 'upgrade', label: '升级（拉取镜像并更新服务）' },
  { value: 'restart', label: '重启服务' },
  { value: 'backup', label: '备份' },
  { value: 'logs', label: '查看日志' },
  { value: 'config', label: '更新配置' },
]

const DEFAULT_JOB_PAYLOADS: Record<string, string> = {
  restart: '{"service":"api"}',
  logs: '{"service":"api","tail":200}',
  backup: '{}',
}

const JOB_PAYLOAD_TIPS: Record<string, string> = {
  deploy: '参数可留空；任务会拉取镜像并更新 compose 服务，目标版本用于登记发布版本号。',
  upgrade: '参数可留空；任务会拉取镜像并更新 compose 服务，目标版本用于登记发布版本号。',
  restart: '参数示例：{"service":"api"}；service 仅支持 api / worker / scheduler / nginx。',
  backup: '参数可留空；任务执行部署目录内固定的 backup.sh，完成后生成备份记录。',
  logs: '参数示例：{"service":"api","tail":200}；tail 取 10-500，后端会脱敏输出。',
  config: '参数为配置键值 JSON 对象，留空表示不下发配置变更。',
}

function defaultJobPayload(jobType: string): string {
  return DEFAULT_JOB_PAYLOADS[jobType] ?? '{}'
}

/* ------------------------------ Tab ------------------------------ */
const route = useRoute()
const router = useRouter()

function tabFromQuery(): OpsCenterTab {
  if (route.query.tab === 'operations') return 'operations'
  if (route.query.tab === 'backups') return 'backups'
  return 'releases'
}

const activeTab = ref<OpsCenterTab>(tabFromQuery())
const activeTabLabel = computed(() => TAB_LABELS[activeTab.value])

watch(activeTab, (tab) => {
  if (route.query.tab === tab) return
  void router.replace({ query: { ...route.query, tab } })
})

watch(
  () => route.query.tab,
  () => {
    const next = tabFromQuery()
    if (next !== activeTab.value) activeTab.value = next
  },
)

/* ------------------------------ 状态展示 ------------------------------ */
function releaseStatusType(status?: string | null): TagType {
  if (status === 'active') return 'success'
  if (status === 'draft') return 'warning'
  return 'info'
}

function releaseStatusText(status?: string | null): string {
  if (status === 'draft') return '草稿'
  if (status === 'active') return '启用'
  if (status === 'retired') return '已下线'
  return textOrDash(status)
}

function buildStatusType(status?: string | null): TagType {
  if (status === 'verified') return 'success'
  if (status === 'built') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

function buildStatusText(status?: string | null): string {
  if (status === 'verified') return '已验证'
  if (status === 'built') return '已构建'
  if (status === 'failed') return '构建失败'
  return textOrDash(status)
}

function rollbackType(compatible?: boolean | null): TagType {
  if (compatible === true) return 'success'
  if (compatible === false) return 'danger'
  return 'info'
}

function rollbackText(compatible?: boolean | null): string {
  if (compatible === true) return '旧应用兼容'
  if (compatible === false) return '不兼容'
  return '-'
}

function minAgentProtocolText(value?: number | null): string {
  const parsed = Number(value ?? 0)
  return Number.isFinite(parsed) && parsed > 0 ? String(parsed) : '-'
}

function isFullCommit(commitSha?: string | null): boolean {
  return /^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/.test((commitSha ?? '').trim())
}

function commitShaText(commitSha?: string | null): string {
  const value = (commitSha ?? '').trim()
  return value || '-'
}

function operationStatusType(status?: string | null): TagType {
  if (status === 'succeeded') return 'success'
  if (status === 'leased' || status === 'running') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

function operationStatusText(status?: string | null): string {
  if (status === 'pending') return '待执行'
  if (status === 'leased') return '已下发'
  if (status === 'running') return '执行中'
  if (status === 'succeeded') return '成功'
  if (status === 'failed') return '失败'
  if (status === 'unknown') return '未知'
  return textOrDash(status)
}

function backupStatusType(status?: string | null): TagType {
  if (status === 'succeeded') return 'success'
  if (status === 'running') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

function backupStatusText(status?: string | null): string {
  if (status === 'succeeded') return '成功'
  if (status === 'running') return '执行中'
  if (status === 'failed') return '失败'
  return textOrDash(status)
}

function jobTypeText(jobType?: string | null): string {
  if (!jobType) return '-'
  return JOB_TYPE_LABELS[jobType] ?? jobType
}

/** 备份大小：字节 → B/KB/MB/GB */
function formatSize(bytes?: number | null): string {
  const value = Number(bytes ?? 0)
  if (!Number.isFinite(value) || value <= 0) return '-'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`
}

/* ------------------------------ 实例下拉 ------------------------------ */
const instanceOptions = ref<OpsInstance[]>([])
const instanceOptionsLoading = ref(false)
const instanceNameMap = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const instance of instanceOptions.value) map[instance.id] = instance.name
  return map
})

function instanceLabel(instance: OpsInstance): string {
  return instance.domain ? `${instance.name}（${instance.domain}）` : instance.name
}

function instanceName(instanceId?: string | null): string {
  if (!instanceId) return ''
  return instanceNameMap.value[instanceId] ?? ''
}

async function loadInstanceOptions(): Promise<void> {
  if (instanceOptionsLoading.value) return
  instanceOptionsLoading.value = true
  try {
    const data = await opsApi.instances({ page: 1, page_size: 100 })
    instanceOptions.value = data.items
  } catch {
    instanceOptions.value = []
  } finally {
    instanceOptionsLoading.value = false
  }
}

/* ------------------------------ 发布版本 ------------------------------ */
const releaseLoading = ref(false)
const releases = ref<OpsRelease[]>([])
const releaseDialogVisible = ref(false)
const releaseSubmitting = ref(false)
const releaseFormRef = ref<FormInstance>()
const releaseForm = reactive({
  version: '',
  commit_sha: '',
  image_tag: '',
  image_digest: '',
  status: 'active' as OpsReleaseStatus,
  build_config_summary: '',
  min_agent_protocol: 0,
  center_db_version: '',
  tenant_db_version: '',
  rollback_compatible: false,
  build_status: 'built' as OpsBuildStatus,
  notes: '',
})
const releaseRules: FormRules = {
  version: [
    { required: true, message: '请输入版本号', trigger: 'blur' },
    { max: 64, message: '版本号不能超过 64 个字符', trigger: 'blur' },
  ],
  commit_sha: [
    { required: true, message: '请输入完整 Git commit', trigger: 'blur' },
    {
      pattern: /^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/,
      message: '必须是完整 40 或 64 位十六进制 commit',
      trigger: 'blur',
    },
  ],
}

async function loadReleases(): Promise<void> {
  releaseLoading.value = true
  try {
    releases.value = await opsApi.releases()
  } catch {
    releases.value = []
  } finally {
    releaseLoading.value = false
  }
}

function openReleaseCreate(): void {
  releaseForm.version = ''
  releaseForm.commit_sha = ''
  releaseForm.image_tag = ''
  releaseForm.image_digest = ''
  releaseForm.status = 'active'
  releaseForm.build_config_summary = ''
  releaseForm.min_agent_protocol = 0
  releaseForm.center_db_version = ''
  releaseForm.tenant_db_version = ''
  releaseForm.rollback_compatible = false
  releaseForm.build_status = 'built'
  releaseForm.notes = ''
  releaseDialogVisible.value = true
}

async function submitRelease(): Promise<void> {
  const formInstance = releaseFormRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  const commitSha = releaseForm.commit_sha.trim().toLowerCase()
  if (!isFullCommit(commitSha)) {
    ElMessage.warning('commit_sha 必须是完整 40/64 位十六进制字符串')
    return
  }
  releaseSubmitting.value = true
  try {
    await opsApi.createRelease({
      version: releaseForm.version.trim(),
      commit_sha: commitSha,
      image_tag: releaseForm.image_tag.trim(),
      image_digest: releaseForm.image_digest.trim(),
      status: releaseForm.status,
      build_config_summary: releaseForm.build_config_summary.trim(),
      min_agent_protocol: Number(releaseForm.min_agent_protocol) || 0,
      center_db_version: releaseForm.center_db_version.trim(),
      tenant_db_version: releaseForm.tenant_db_version.trim(),
      rollback_compatible: releaseForm.rollback_compatible,
      build_status: releaseForm.build_status,
      notes: releaseForm.notes.trim(),
    })
    ElMessage.success('发布版本已登记')
    releaseDialogVisible.value = false
    await loadReleases()
  } finally {
    releaseSubmitting.value = false
  }
}

function releaseNeedsBackfill(release: OpsRelease): boolean {
  return (
    !isFullCommit(release.commit_sha) ||
    release.build_status !== 'verified' ||
    !release.build_config_summary ||
    !Number(release.min_agent_protocol ?? 0) ||
    !release.center_db_version ||
    !release.tenant_db_version
  )
}

/* ------------------------------ 回填旧发布版本 ------------------------------ */
const releasePatchVisible = ref(false)
const releasePatchSubmitting = ref(false)
const releasePatchFormRef = ref<FormInstance>()
const releasePatchTarget = ref<OpsRelease | null>(null)
const releasePatchForm = reactive({
  commit_sha: '',
  build_config_summary: '',
  min_agent_protocol: 0,
  center_db_version: '',
  tenant_db_version: '',
  rollback_compatible: false,
  build_status: 'built' as OpsBuildStatus,
  status: 'active' as OpsReleaseStatus,
  notes: '',
})
const releasePatchRules: FormRules = {
  commit_sha: [
    { required: true, message: '请输入完整 Git commit', trigger: 'blur' },
    {
      pattern: /^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$/,
      message: '必须是完整 40 或 64 位十六进制 commit',
      trigger: 'blur',
    },
  ],
}

function openReleasePatch(release: OpsRelease): void {
  releasePatchTarget.value = release
  releasePatchForm.commit_sha = (release.commit_sha ?? '').trim()
  releasePatchForm.build_config_summary = release.build_config_summary ?? ''
  releasePatchForm.min_agent_protocol = Number(release.min_agent_protocol ?? 0) || 0
  releasePatchForm.center_db_version = release.center_db_version ?? ''
  releasePatchForm.tenant_db_version = release.tenant_db_version ?? ''
  releasePatchForm.rollback_compatible = release.rollback_compatible === true
  releasePatchForm.build_status =
    release.build_status === 'verified' || release.build_status === 'failed'
      ? release.build_status
      : 'built'
  releasePatchForm.status =
    release.status === 'draft' || release.status === 'retired' ? release.status : 'active'
  releasePatchForm.notes = release.notes ?? ''
  releasePatchVisible.value = true
}

async function submitReleasePatch(): Promise<void> {
  const target = releasePatchTarget.value
  const formInstance = releasePatchFormRef.value
  if (!target || !formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  const commitSha = releasePatchForm.commit_sha.trim().toLowerCase()
  if (!isFullCommit(commitSha)) {
    ElMessage.warning('commit_sha 必须是完整 40/64 位十六进制字符串')
    return
  }
  releasePatchSubmitting.value = true
  try {
    await opsApi.updateRelease(target.id, {
      commit_sha: commitSha,
      build_config_summary: releasePatchForm.build_config_summary.trim(),
      min_agent_protocol: Number(releasePatchForm.min_agent_protocol) || 0,
      center_db_version: releasePatchForm.center_db_version.trim(),
      tenant_db_version: releasePatchForm.tenant_db_version.trim(),
      rollback_compatible: releasePatchForm.rollback_compatible,
      build_status: releasePatchForm.build_status,
      status: releasePatchForm.status,
      notes: releasePatchForm.notes.trim(),
    })
    ElMessage.success('发布版本已回填')
    releasePatchVisible.value = false
    await loadReleases()
  } finally {
    releasePatchSubmitting.value = false
  }
}

/* ------------------------------ 运维任务 ------------------------------ */
const operationQuery = reactive({
  instance_id: '',
})

const {
  rows: operationRows,
  total: operationTotal,
  page: operationPage,
  pageSize: operationPageSize,
  loading: operationLoading,
  search: searchOperations,
  reload: reloadOperations,
  handlePageChange: handleOperationPageChange,
  handleSizeChange: handleOperationSizeChange,
} = useTableQuery<OpsOperation>((params) =>
  opsApi.operations({
    page: params.page,
    page_size: params.page_size,
    instance_id: stringOrUndefined(operationQuery.instance_id),
  }),
)

const operationDialogVisible = ref(false)
const operationSubmitting = ref(false)
const operationFormRef = ref<FormInstance>()
const operationForm = reactive({
  instance_id: '',
  job_type: 'restart' as OpsJobType,
  target_version: '',
  payload: defaultJobPayload('restart'),
})
const operationRules: FormRules = {
  instance_id: [{ required: true, message: '请选择实例', trigger: 'change' }],
  job_type: [{ required: true, message: '请选择任务类型', trigger: 'change' }],
}
const operationPayloadTip = computed(
  () => JOB_PAYLOAD_TIPS[operationForm.job_type] ?? '请输入 JSON 对象，留空表示无参数。',
)

async function retryOperation(row: OpsOperation): Promise<void> {
  await opsApi.retryOperation(row.id)
  ElMessage.success('已创建重试任务')
  await searchOperations()
}

function openOperationCreate(): void {
  operationForm.instance_id = operationQuery.instance_id
  operationForm.job_type = 'restart'
  operationForm.target_version = ''
  operationForm.payload = defaultJobPayload('restart')
  operationDialogVisible.value = true
  if (!instanceOptions.value.length) void loadInstanceOptions()
}

function handleJobTypeChange(value: string): void {
  operationForm.payload = defaultJobPayload(value)
}

async function submitOperation(): Promise<void> {
  const formInstance = operationFormRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  const payload = parseJsonObject(operationForm.payload)
  if (operationForm.payload.trim() && !payload) {
    ElMessage.warning('参数必须是合法的 JSON 对象')
    return
  }
  operationSubmitting.value = true
  try {
    await opsApi.createOperation(operationForm.instance_id, {
      job_type: operationForm.job_type,
      target_version: operationForm.target_version.trim(),
      payload: payload ?? {},
    })
    ElMessage.success('运维任务已创建')
    operationDialogVisible.value = false
    await Promise.all([reloadOperations(), reloadBackups()])
  } finally {
    operationSubmitting.value = false
  }
}

/* ------------------------------ 备份记录 ------------------------------ */
const backupQuery = reactive({
  instance_id: '',
})

const {
  rows: backupRows,
  total: backupTotal,
  page: backupPage,
  pageSize: backupPageSize,
  loading: backupLoading,
  search: searchBackups,
  reload: reloadBackups,
  handlePageChange: handleBackupPageChange,
  handleSizeChange: handleBackupSizeChange,
} = useTableQuery<OpsBackup>((params) =>
  opsApi.backups({
    page: params.page,
    page_size: params.page_size,
    instance_id: stringOrUndefined(backupQuery.instance_id),
  }),
)

onMounted(() => {
  void loadReleases()
  void loadInstanceOptions()
})
</script>

<template>
  <div class="ops-center-page">
    <div class="ops-center-head">
      <div>
        <h2>运维中心</h2>
        <p>发布版本、远程运维任务与备份记录。</p>
      </div>
      <el-tag type="info" effect="plain">{{ activeTabLabel }}</el-tag>
    </div>

    <el-tabs v-model="activeTab" class="ops-center-tabs">
      <!-- 发布版本 -->
      <el-tab-pane label="发布版本" name="releases">
        <el-card shadow="never" class="page-card">
          <div class="table-toolbar">
            <div class="section-title">
              发布版本
              <span class="text-muted">（共 {{ releases.length }} 个）</span>
            </div>
            <div class="toolbar-actions">
              <el-button :icon="Refresh" :loading="releaseLoading" @click="loadReleases">刷新</el-button>
              <el-button type="primary" :icon="Plus" @click="openReleaseCreate">新建版本</el-button>
            </div>
          </div>

          <el-table v-loading="releaseLoading" :data="releases" border size="small" row-key="id">
            <el-table-column label="ID" width="100">
              <template #default="{ row }"><IdCopy :id="row.id" /></template>
            </el-table-column>
            <el-table-column label="版本号" width="130">
              <template #default="{ row }">
                <span class="mono">{{ textOrDash(row.version) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="发布状态" width="95">
              <template #default="{ row }">
                <el-tag :type="releaseStatusType(row.status)" size="small" disable-transitions>
                  {{ releaseStatusText(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="构建状态" width="100">
              <template #default="{ row }">
                <el-tag
                  v-if="row.build_status"
                  :type="buildStatusType(row.build_status)"
                  size="small"
                  disable-transitions
                >
                  {{ buildStatusText(row.build_status) }}
                </el-tag>
                <span v-else class="text-muted">-</span>
              </template>
            </el-table-column>
            <el-table-column label="完整 commit" min-width="300">
              <template #default="{ row }">
                <span class="commit-full mono">{{ commitShaText(row.commit_sha) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="构建配置摘要" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ textOrDash(row.build_config_summary) }}</template>
            </el-table-column>
            <el-table-column label="最低 Agent 协议" width="125">
              <template #default="{ row }">{{ minAgentProtocolText(row.min_agent_protocol) }}</template>
            </el-table-column>
            <el-table-column label="DB 目标版本" min-width="190">
              <template #default="{ row }">
                <div class="version-line">
                  <span class="text-muted">中心库</span>
                  <span class="mono">{{ textOrDash(row.center_db_version) }}</span>
                </div>
                <div class="version-line">
                  <span class="text-muted">租户库</span>
                  <span class="mono">{{ textOrDash(row.tenant_db_version) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="回退兼容" width="115">
              <template #default="{ row }">
                <el-tag
                  v-if="typeof row.rollback_compatible === 'boolean'"
                  :type="rollbackType(row.rollback_compatible)"
                  size="small"
                  disable-transitions
                >
                  {{ rollbackText(row.rollback_compatible) }}
                </el-tag>
                <span v-else class="text-muted">-</span>
              </template>
            </el-table-column>
            <el-table-column label="镜像" min-width="230" show-overflow-tooltip>
              <template #default="{ row }">
                <div class="mono">{{ textOrDash(row.image_tag) }}</div>
                <div class="version-line text-muted mono">{{ textOrDash(row.image_digest) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="发布说明" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ textOrDash(row.notes) }}</template>
            </el-table-column>
            <el-table-column label="创建时间" width="170">
              <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="releaseNeedsBackfill(row as OpsRelease)"
                  text
                  type="primary"
                  size="small"
                  @click="openReleasePatch(row as OpsRelease)"
                >
                  回填
                </el-button>
                <span v-else class="text-muted">-</span>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无发布版本，请先登记版本" :image-size="72" />
            </template>
          </el-table>
        </el-card>
      </el-tab-pane>

      <!-- 运维任务 -->
      <el-tab-pane label="运维任务" name="operations" lazy>
        <el-card shadow="never" class="page-card">
          <div class="filter-bar">
            <el-select
              v-model="operationQuery.instance_id"
              placeholder="全部实例"
              clearable
              filterable
              :loading="instanceOptionsLoading"
              style="width: 260px"
              @change="searchOperations()"
            >
              <el-option
                v-for="instance in instanceOptions"
                :key="instance.id"
                :label="instanceLabel(instance)"
                :value="instance.id"
              />
            </el-select>
            <el-button type="primary" :icon="Search" @click="searchOperations()">查询</el-button>
            <div class="filter-spacer" />
            <span class="text-muted">共 {{ operationTotal }} 条</span>
            <el-button :icon="Refresh" @click="reloadOperations()">刷新</el-button>
            <el-button type="primary" :icon="Plus" @click="openOperationCreate">新建任务</el-button>
          </div>

          <el-table v-loading="operationLoading" :data="operationRows" border size="small" row-key="id">
            <el-table-column label="ID" width="110">
              <template #default="{ row }"><IdCopy :id="row.id" /></template>
            </el-table-column>
            <el-table-column label="实例" min-width="160">
              <template #default="{ row }">
                <div>{{ instanceName(row.instance_id) || '-' }}</div>
                <IdCopy :id="row.instance_id" />
              </template>
            </el-table-column>
            <el-table-column label="类型" width="110">
              <template #default="{ row }">{{ jobTypeText(row.job_type) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="95">
              <template #default="{ row }">
                <el-tag :type="operationStatusType(row.status)" size="small" disable-transitions>
                  {{ operationStatusText(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="目标版本" width="120">
              <template #default="{ row }">
                <span class="mono">{{ textOrDash(row.target_version) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="尝试" width="70">
              <template #default="{ row }">{{ row.attempts ?? 0 }}</template>
            </el-table-column>
            <el-table-column label="错误" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ textOrDash(row.error) }}</template>
            </el-table-column>
            <el-table-column label="创建时间" width="170">
              <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="执行时间" width="190">
              <template #default="{ row }">
                <div>开始 {{ formatDateTime(row.started_at) }}</div>
                <div class="text-muted">完成 {{ formatDateTime(row.finished_at) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" fixed="right">
              <template #default="{ row }">
                <el-button
                  v-if="row.status === 'failed' || row.status === 'unknown'"
                  text
                  type="primary"
                  size="small"
                  @click="retryOperation(row as OpsOperation)"
                >重试</el-button>
                <span v-else class="text-muted">-</span>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无运维任务，请新建任务" :image-size="72" />
            </template>
          </el-table>

          <PaginationBar
            :total="operationTotal"
            :page="operationPage"
            :page-size="operationPageSize"
            @page-change="handleOperationPageChange"
            @size-change="handleOperationSizeChange"
          />
        </el-card>
      </el-tab-pane>

      <!-- 备份记录 -->
      <el-tab-pane label="备份记录" name="backups" lazy>
        <el-card shadow="never" class="page-card">
          <div class="filter-bar">
            <el-select
              v-model="backupQuery.instance_id"
              placeholder="全部实例"
              clearable
              filterable
              :loading="instanceOptionsLoading"
              style="width: 260px"
              @change="searchBackups()"
            >
              <el-option
                v-for="instance in instanceOptions"
                :key="instance.id"
                :label="instanceLabel(instance)"
                :value="instance.id"
              />
            </el-select>
            <el-button type="primary" :icon="Search" @click="searchBackups()">查询</el-button>
            <div class="filter-spacer" />
            <span class="text-muted">共 {{ backupTotal }} 条</span>
            <el-button :icon="Refresh" @click="reloadBackups()">刷新</el-button>
          </div>

          <el-table v-loading="backupLoading" :data="backupRows" border size="small" row-key="id">
            <el-table-column label="ID" width="110">
              <template #default="{ row }"><IdCopy :id="row.id" /></template>
            </el-table-column>
            <el-table-column label="实例" min-width="160">
              <template #default="{ row }">
                <div>{{ instanceName(row.instance_id) || '-' }}</div>
                <IdCopy :id="row.instance_id" />
              </template>
            </el-table-column>
            <el-table-column label="状态" width="95">
              <template #default="{ row }">
                <el-tag :type="backupStatusType(row.status)" size="small" disable-transitions>
                  {{ backupStatusText(row.status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="存储键" min-width="240" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="mono">{{ textOrDash(row.storage_key) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="大小" width="100">
              <template #default="{ row }">{{ formatSize(row.size_bytes) }}</template>
            </el-table-column>
            <el-table-column label="开始时间" width="170">
              <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
            </el-table-column>
            <el-table-column label="完成时间" width="170">
              <template #default="{ row }">{{ formatDateTime(row.finished_at) }}</template>
            </el-table-column>
            <el-table-column label="错误" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ textOrDash(row.error) }}</template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无备份记录，可新建 backup 运维任务" :image-size="72" />
            </template>
          </el-table>

          <PaginationBar
            :total="backupTotal"
            :page="backupPage"
            :page-size="backupPageSize"
            @page-change="handleBackupPageChange"
            @size-change="handleBackupSizeChange"
          />
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>

  <!-- 新建发布版本 -->
  <el-dialog v-model="releaseDialogVisible" title="新建发布版本" width="680px" destroy-on-close>
    <el-form ref="releaseFormRef" :model="releaseForm" :rules="releaseRules" label-width="110px">
      <el-form-item label="版本号" prop="version">
        <el-input v-model="releaseForm.version" maxlength="64" placeholder="如 v1.2.3" />
      </el-form-item>
      <el-form-item label="完整 commit" prop="commit_sha">
        <el-input
          v-model="releaseForm.commit_sha"
          maxlength="64"
          class="mono-input"
          placeholder="完整 40 或 64 位 Git commit，不允许缩写"
        />
        <div class="form-tip">升级仅对完整 commit + active + verified 的发布版本开放。</div>
      </el-form-item>
      <el-form-item label="发布状态">
        <el-select v-model="releaseForm.status" style="width: 100%">
          <el-option label="草稿" value="draft" />
          <el-option label="启用" value="active" />
          <el-option label="已下线" value="retired" />
        </el-select>
      </el-form-item>
      <el-form-item label="构建状态">
        <el-select v-model="releaseForm.build_status" style="width: 100%">
          <el-option label="已构建（不可升级）" value="built" />
          <el-option label="已验证（可升级）" value="verified" />
          <el-option label="构建失败" value="failed" />
        </el-select>
      </el-form-item>
      <el-form-item label="镜像标签">
        <el-input
          v-model="releaseForm.image_tag"
          maxlength="255"
          placeholder="如 registry.example.com/erp:v1.2.3"
        />
      </el-form-item>
      <el-form-item label="镜像摘要">
        <el-input v-model="releaseForm.image_digest" maxlength="255" placeholder="如 sha256:..." />
      </el-form-item>
      <el-form-item label="构建配置摘要">
        <el-input
          v-model="releaseForm.build_config_summary"
          type="textarea"
          :rows="2"
          maxlength="500"
          show-word-limit
          placeholder="如 build profile、镜像构建参数、依赖锁定摘要"
        />
      </el-form-item>
      <el-form-item label="最低 Agent 协议">
        <el-input-number
          v-model="releaseForm.min_agent_protocol"
          :min="0"
          :max="10000"
          :step="1"
          :precision="0"
          controls-position="right"
          style="width: 180px"
        />
        <span class="form-inline-tip">0 表示未声明</span>
      </el-form-item>
      <el-form-item label="中心库目标版本">
        <el-input
          v-model="releaseForm.center_db_version"
          maxlength="64"
          placeholder="如 center_0016"
        />
      </el-form-item>
      <el-form-item label="租户库目标版本">
        <el-input
          v-model="releaseForm.tenant_db_version"
          maxlength="64"
          placeholder="如 tenant_0014"
        />
      </el-form-item>
      <el-form-item label="回退兼容">
        <el-switch v-model="releaseForm.rollback_compatible" />
        <span class="form-inline-tip">允许旧应用继续兼容新库时开启</span>
      </el-form-item>
      <el-form-item label="发布说明">
        <el-input v-model="releaseForm.notes" type="textarea" :rows="3" maxlength="500" show-word-limit />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="releaseDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="releaseSubmitting" @click="submitRelease">保存</el-button>
    </template>
  </el-dialog>

  <!-- 回填旧发布版本 -->
  <el-dialog v-model="releasePatchVisible" title="回填发布版本" width="680px" destroy-on-close>
    <el-descriptions v-if="releasePatchTarget" :column="1" border size="small" class="release-patch-meta">
      <el-descriptions-item label="版本号">
        <span class="mono">{{ releasePatchTarget.version }}</span>
      </el-descriptions-item>
      <el-descriptions-item label="镜像">
        <span class="mono">{{ textOrDash(releasePatchTarget.image_tag) }}</span>
        <span v-if="releasePatchTarget.image_digest" class="text-muted">
          · {{ releasePatchTarget.image_digest }}
        </span>
      </el-descriptions-item>
    </el-descriptions>
    <el-form
      ref="releasePatchFormRef"
      :model="releasePatchForm"
      :rules="releasePatchRules"
      label-width="110px"
    >
      <el-form-item label="完整 commit" prop="commit_sha">
        <el-input
          v-model="releasePatchForm.commit_sha"
          maxlength="64"
          class="mono-input"
          placeholder="回填完整 40 或 64 位 Git commit"
        />
        <div class="form-tip">版本号和镜像不可修改；回填后需 active + verified 才可用于升级。</div>
      </el-form-item>
      <el-form-item label="发布状态">
        <el-select v-model="releasePatchForm.status" style="width: 100%">
          <el-option label="草稿" value="draft" />
          <el-option label="启用" value="active" />
          <el-option label="已下线" value="retired" />
        </el-select>
      </el-form-item>
      <el-form-item label="构建状态">
        <el-select v-model="releasePatchForm.build_status" style="width: 100%">
          <el-option label="已构建（不可升级）" value="built" />
          <el-option label="已验证（可升级）" value="verified" />
          <el-option label="构建失败" value="failed" />
        </el-select>
      </el-form-item>
      <el-form-item label="构建配置摘要">
        <el-input
          v-model="releasePatchForm.build_config_summary"
          type="textarea"
          :rows="2"
          maxlength="500"
          show-word-limit
        />
      </el-form-item>
      <el-form-item label="最低 Agent 协议">
        <el-input-number
          v-model="releasePatchForm.min_agent_protocol"
          :min="0"
          :max="10000"
          :step="1"
          :precision="0"
          controls-position="right"
          style="width: 180px"
        />
      </el-form-item>
      <el-form-item label="中心库目标版本">
        <el-input
          v-model="releasePatchForm.center_db_version"
          maxlength="64"
          placeholder="如 center_0016"
        />
      </el-form-item>
      <el-form-item label="租户库目标版本">
        <el-input
          v-model="releasePatchForm.tenant_db_version"
          maxlength="64"
          placeholder="如 tenant_0016"
        />
      </el-form-item>
      <el-form-item label="回退兼容">
        <el-switch v-model="releasePatchForm.rollback_compatible" />
      </el-form-item>
      <el-form-item label="发布说明">
        <el-input
          v-model="releasePatchForm.notes"
          type="textarea"
          :rows="2"
          maxlength="500"
          show-word-limit
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="releasePatchVisible = false">取消</el-button>
      <el-button type="primary" :loading="releasePatchSubmitting" @click="submitReleasePatch">
        保存回填
      </el-button>
    </template>
  </el-dialog>

  <!-- 新建运维任务 -->
  <el-dialog v-model="operationDialogVisible" title="新建运维任务" width="640px" destroy-on-close>
    <el-form ref="operationFormRef" :model="operationForm" :rules="operationRules" label-width="90px">
      <el-form-item label="实例" prop="instance_id">
        <el-select
          v-model="operationForm.instance_id"
          filterable
          :loading="instanceOptionsLoading"
          placeholder="请选择实例"
          no-data-text="暂无可选实例，请先在「服务器与实例」登记"
          style="width: 100%"
        >
          <el-option
            v-for="instance in instanceOptions"
            :key="instance.id"
            :label="instanceLabel(instance)"
            :value="instance.id"
          />
        </el-select>
        <div class="form-tip">同一实例同时只允许一个未完成运维任务，重复提交会提示冲突。</div>
      </el-form-item>
      <el-form-item label="任务类型" prop="job_type">
        <el-select v-model="operationForm.job_type" style="width: 100%" @change="handleJobTypeChange">
          <el-option
            v-for="jobType in JOB_TYPE_OPTIONS"
            :key="jobType.value"
            :label="jobType.label"
            :value="jobType.value"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="目标版本">
        <el-input
          v-model="operationForm.target_version"
          maxlength="64"
          placeholder="如 v1.2.3，部署 / 升级时填写"
        />
      </el-form-item>
      <el-form-item label="参数 JSON">
        <el-input
          v-model="operationForm.payload"
          type="textarea"
          :rows="5"
          class="payload-input"
          placeholder='如 {"service":"api"}'
        />
        <div class="form-tip">{{ operationPayloadTip }}</div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="operationDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="operationSubmitting" @click="submitOperation">创建</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.ops-center-page {
  min-width: 0;
}

.ops-center-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 8px;
}

.ops-center-head h2 {
  margin: 0;
  font-size: 20px;
  line-height: 1.4;
}

.ops-center-head p {
  margin: 4px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.ops-center-tabs :deep(.el-tabs__header) {
  margin-bottom: 12px;
}

.filter-spacer {
  flex: 1;
}

.section-title {
  font-size: 15px;
  font-weight: 600;
}

.section-title .text-muted {
  font-weight: 400;
}

.version-line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  line-height: 1.6;
  font-size: 12px;
}

.commit-full {
  display: inline-block;
  max-width: 100%;
  line-height: 1.5;
  white-space: normal;
  word-break: break-all;
  font-size: 11px;
}

.mono-input :deep(.el-input__inner) {
  font-family: Consolas, 'SFMono-Regular', Menlo, monospace;
}

.form-inline-tip {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.release-patch-meta {
  margin-bottom: 12px;
}

.payload-input :deep(.el-textarea__inner) {
  font-family: Consolas, 'SFMono-Regular', Menlo, monospace;
  font-size: 12px;
}

@media (max-width: 900px) {
  .ops-center-head {
    flex-direction: column;
  }
}
</style>
