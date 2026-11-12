<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import type { SyncSettings } from '@/types/tasks'
import { ElMessage } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { tasksApi } from '@/api/tasks'
import { useTableQuery } from '@/composables/useTableQuery'
import { useTenantTask } from '@/composables/useTenantTask'
import { useTenantStore } from '@/stores/tenant'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusTag from '@/components/StatusTag.vue'
import IdCopy from '@/components/IdCopy.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { formatDateTime, stringOrUndefined, textOrDash } from '@/utils/format'
import { jobTypeLabels, labelOf, triggeredByLabels } from '@/utils/dict'
import type { SyncRun, SyncRunDetail, SyncSchedule } from '@/types'

const tenant = useTenantStore()
const tid = (): string => tenant.requireTenantId()
const { submitAndPoll } = useTenantTask()

const query = reactive({
  job_type: '',
  status: '',
})

const {
  rows,
  total,
  page,
  pageSize,
  loading,
  search,
  reload,
  handlePageChange,
  handleSizeChange,
} = useTableQuery<SyncRun>((params) =>
  tasksApi.syncRuns(tid(), {
    page: params.page,
    page_size: params.page_size,
    job_type: stringOrUndefined(query.job_type),
    status: stringOrUndefined(query.status),
  }),
)

/* ------------------------------ 任务详情 ------------------------------ */
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<SyncRunDetail | null>(null)

async function openDetail(run: SyncRun): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await tasksApi.syncRun(tid(), run.id)
  } finally {
    detailLoading.value = false
  }
}

/* ------------------------------ 重试 ------------------------------ */
const retryingId = ref('')
const jobPermissions: Record<string, string> = {
  order_sync: 'order.sync', stock_push: 'stock.push',
  listing_sync: 'channel.listing.map', token_refresh: 'store.connect',
}
function canRetry(run: SyncRun): boolean {
  return Boolean(jobPermissions[run.job_type] && tenant.has(jobPermissions[run.job_type]!))
}


async function retry(run: SyncRun): Promise<void> {
  retryingId.value = run.id
  try {
    await submitAndPoll((tenantId) => tasksApi.retrySyncRun(tenantId, run.id), reload)
  } finally {
    retryingId.value = ''
  }
}

/* ------------------------------ 同步总开关 ------------------------------ */
const syncSettings = ref<SyncSettings | null>(null)
const settingsSaving = ref(false)
async function loadSyncSettings(): Promise<void> {
  if (!tenant.has('tenant.setting.manage')) return
  syncSettings.value = await tasksApi.syncSettings(tid())
}
async function toggleSyncSetting(key: keyof SyncSettings, value: string | number | boolean): Promise<void> {
  settingsSaving.value = true
  try {
    syncSettings.value = await tasksApi.updateSyncSettings(tid(), { [key]: Boolean(value) })
    await tenant.reloadBootstrap()
    ElMessage.success('同步开关已更新')
  } finally {
    settingsSaving.value = false
  }
}

/* ------------------------------ 定时同步 ------------------------------ */
const schedules = ref<SyncSchedule[]>([])
const scheduleLoading = ref(false)
const scheduleSavingId = ref('')

async function loadSchedules(): Promise<void> {
  scheduleLoading.value = true
  try {
    schedules.value = await tasksApi.syncSchedules(tid())
  } catch {
    schedules.value = []
  } finally {
    scheduleLoading.value = false
  }
}

async function updateSchedule(row: SyncSchedule, payload: { enabled: boolean; interval_minutes: number }): Promise<void> {
  scheduleSavingId.value = row.id
  try {
    await tasksApi.updateSyncSchedule(tid(), row.id, payload)
    ElMessage.success('定时同步配置已更新')
    await loadSchedules()
  } finally {
    scheduleSavingId.value = ''
  }
}

function onToggleSchedule(row: SyncSchedule, value: string | number | boolean): void {
  void updateSchedule(row, { enabled: Boolean(value), interval_minutes: row.interval_minutes })
}

const intervalVisible = ref(false)
const intervalSaving = ref(false)
const intervalTarget = ref<SyncSchedule | null>(null)
const intervalValue = ref(15)

function openInterval(row: SyncSchedule): void {
  intervalTarget.value = row
  intervalValue.value = row.interval_minutes
  intervalVisible.value = true
}

async function saveInterval(): Promise<void> {
  const target = intervalTarget.value
  if (!target) return
  intervalSaving.value = true
  try {
    await updateSchedule(target, { enabled: target.enabled, interval_minutes: Number(intervalValue.value) })
    intervalVisible.value = false
  } finally {
    intervalSaving.value = false
  }
}

onMounted(() => {
  void loadSchedules()
  void loadSyncSettings()
})
</script>

<template>
  <el-card v-if="syncSettings && tenant.has('tenant.setting.manage')" shadow="never" class="page-card">
    <el-space :size="24">
      <span>订单同步 <el-switch :model-value="syncSettings.order_sync_enabled" :loading="settingsSaving" @change="(value: string | number | boolean) => toggleSyncSetting('order_sync_enabled', value)" /></span>
      <span>平台库存推送 <el-switch :model-value="syncSettings.stock_push_enabled" :loading="settingsSaving" @change="(value: string | number | boolean) => toggleSyncSetting('stock_push_enabled', value)" /></span>
    </el-space>
  </el-card>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-select v-model="query.job_type" placeholder="任务类型" clearable style="width: 180px" @change="search()">
        <el-option v-for="(label, value) in jobTypeLabels" :key="value" :label="label" :value="value" />
      </el-select>
      <el-select v-model="query.status" placeholder="状态" clearable style="width: 150px" @change="search()">
        <el-option label="等待中" value="pending" />
        <el-option label="执行中" value="running" />
        <el-option label="成功" value="succeeded" />
        <el-option label="失败" value="failed" />
        <el-option label="部分成功" value="partial" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="任务 ID" width="110">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column label="类型" width="150">
        <template #default="{ row }">{{ labelOf(jobTypeLabels, row.job_type) }}</template>
      </el-table-column>
      <el-table-column label="店铺" width="150" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.store_name) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="触发方式" width="110">
        <template #default="{ row }">{{ labelOf(triggeredByLabels, row.triggered_by) }}</template>
      </el-table-column>
      <el-table-column label="开始时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
      </el-table-column>
      <el-table-column label="结束时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.finished_at) }}</template>
      </el-table-column>
      <el-table-column label="处理条数" width="100" align="right">
        <template #default="{ row }">{{ row.total_processed ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="错误" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.error_message) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button text type="primary" size="small" @click="openDetail(row as SyncRun)">详情</el-button>
          <el-button
            v-if="(row.status === 'failed' || row.status === 'partial') && canRetry(row as SyncRun)"
            text
            type="warning"
            size="small"
            :loading="retryingId === row.id"
            @click="retry(row as SyncRun)"
          >
            重试
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <PaginationBar
      :total="total"
      :page="page"
      :page-size="pageSize"
      @page-change="handlePageChange"
      @size-change="handleSizeChange"
    />
  </el-card>

  <el-card shadow="never" class="page-card">
    <div class="table-toolbar">
      <span style="font-weight: 600">定时同步配置</span>
      <el-button :icon="Refresh" @click="loadSchedules">刷新</el-button>
    </div>
    <el-table v-loading="scheduleLoading" :data="schedules" border size="small" row-key="id" empty-text="暂无定时配置">
      <el-table-column label="ID" width="110">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column label="任务类型" width="170">
        <template #default="{ row }">{{ labelOf(jobTypeLabels, row.job_type) }}</template>
      </el-table-column>
      <el-table-column label="店铺" width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.store_name) }}</template>
      </el-table-column>
      <el-table-column label="间隔（分钟）" width="130" align="right">
        <template #default="{ row }">
          <el-button text type="primary" size="small" :disabled="!tenant.has('task.schedule.manage')" @click="openInterval(row as SyncSchedule)">{{ row.interval_minutes }}</el-button>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="100">
        <template #default="{ row }">
          <el-switch
            :model-value="row.enabled"
            :disabled="!tenant.has('task.schedule.manage')"
            :loading="scheduleSavingId === row.id"
            @change="(value: string | number | boolean) => onToggleSchedule(row as SyncSchedule, value)"
          />
        </template>
      </el-table-column>
      <el-table-column label="上次执行" width="170">
        <template #default="{ row }">{{ formatDateTime(row.last_run_at) }}</template>
      </el-table-column>
      <el-table-column label="下次执行" width="170">
        <template #default="{ row }">{{ formatDateTime(row.next_run_at) }}</template>
      </el-table-column>
    </el-table>
  </el-card>

  <!-- 任务详情 -->
  <el-drawer v-model="detailVisible" title="任务详情" size="640px" destroy-on-close>
    <el-skeleton v-if="detailLoading" :rows="6" animated />
    <template v-else-if="detail">
      <el-descriptions :column="2" border size="small" class="detail-descriptions">
        <el-descriptions-item label="任务 ID"><IdCopy :id="detail.id" /></el-descriptions-item>
        <el-descriptions-item label="类型">{{ labelOf(jobTypeLabels, detail.job_type) }}</el-descriptions-item>
        <el-descriptions-item label="状态"><StatusTag :status="detail.status" /></el-descriptions-item>
        <el-descriptions-item label="触发方式">{{ labelOf(triggeredByLabels, detail.triggered_by) }}</el-descriptions-item>
        <el-descriptions-item label="店铺">{{ textOrDash(detail.store_name) }}</el-descriptions-item>
        <el-descriptions-item label="处理条数">{{ detail.total_processed ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="开始时间">{{ formatDateTime(detail.started_at) }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ formatDateTime(detail.finished_at) }}</el-descriptions-item>
        <el-descriptions-item label="游标/断点" :span="2">{{ textOrDash(detail.cursor) }}</el-descriptions-item>
        <el-descriptions-item label="错误信息" :span="2">{{ textOrDash(detail.error_message) }}</el-descriptions-item>
      </el-descriptions>

      <div v-if="detail.items && detail.items.length" style="margin-bottom: 12px">
        <div style="font-weight: 600; margin-bottom: 8px">处理明细</div>
        <el-table :data="detail.items" border size="small">
          <el-table-column label="对象类型" width="140">
            <template #default="{ row }">{{ textOrDash(row.object_type) }}</template>
          </el-table-column>
          <el-table-column label="对象 ID" min-width="140">
            <template #default="{ row }"><span class="mono">{{ textOrDash(row.object_id) }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }"><StatusTag :status="row.status" /></template>
          </el-table-column>
          <el-table-column label="说明" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ textOrDash(row.message) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <div style="font-weight: 600; margin: 12px 0 8px">结果数据</div>
      <JsonBlock :value="detail.result ?? detail.logs" empty-text="无结果数据" />
    </template>
  </el-drawer>

  <!-- 修改间隔 -->
  <el-dialog v-model="intervalVisible" title="修改同步间隔" width="380px">
    <el-form label-width="90px">
      <el-form-item label="间隔（分钟）">
        <el-input-number v-model="intervalValue" :min="1" :max="1440" :controls="false" style="width: 100%" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="intervalVisible = false">取消</el-button>
      <el-button type="primary" :loading="intervalSaving" @click="saveInterval">保存</el-button>
    </template>
  </el-dialog>
</template>
