<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh, Search } from '@element-plus/icons-vue'
import { salesApi } from '@/api/sales'
import { channelsApi } from '@/api/channels'
import { useTableQuery } from '@/composables/useTableQuery'
import { useTenantTask } from '@/composables/useTenantTask'
import { useTenantStore } from '@/stores/tenant'
import PaginationBar from '@/components/PaginationBar.vue'
import { formatDateTime, formatMoney, stringOrUndefined, textOrDash } from '@/utils/format'
import { ORDER_STATUS_OPTIONS, orderStatusLabel, orderStatusType } from '@/utils/orderStatus'
import type { Order, Store } from '@/types'

const tenant = useTenantStore()
const router = useRouter()
const tid = (): string => tenant.requireTenantId()
const { submitAndPoll } = useTenantTask()

const canSync = computed(() => tenant.has('order.sync'))

const statusOptions = ORDER_STATUS_OPTIONS

const query = reactive({
  status: '',
  store_id: '',
  q: '',
})
const dateRange = ref<[string, string] | null>(null)

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
} = useTableQuery<Order>((params) =>
  salesApi.orders(tid(), {
    page: params.page,
    page_size: params.page_size,
    status: stringOrUndefined(query.status),
    store_id: stringOrUndefined(query.store_id),
    q: stringOrUndefined(query.q),
    date_from: dateRange.value?.[0] ?? undefined,
    date_to: dateRange.value?.[1] ?? undefined,
  }),
)

const stores = ref<Store[]>([])

async function loadStores(): Promise<void> {
  if (!tenant.has('store.read')) return
  try {
    stores.value = await channelsApi.stores(tid())
  } catch {
    stores.value = []
  }
}

/* ------------------------------ 手动同步 ------------------------------ */
const syncVisible = ref(false)
const syncStoreId = ref('')
const syncing = ref(false)

function openSync(): void {
  syncStoreId.value = ''
  syncVisible.value = true
}

async function submitSync(): Promise<void> {
  syncing.value = true
  try {
    await submitAndPoll(
      (tenantId) => salesApi.syncOrders(tenantId, syncStoreId.value || undefined),
      reload,
    )
    syncVisible.value = false
  } finally {
    syncing.value = false
  }
}

function openDetail(order: Order): void {
  void router.push(`/orders/${order.id}`)
}

onMounted(loadStores)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-select v-model="query.status" placeholder="订单状态" clearable filterable allow-create style="width: 170px" @change="search()">
        <el-option v-for="status in statusOptions" :key="status.value" :label="status.label" :value="status.value" />
      </el-select>
      <el-select v-model="query.store_id" placeholder="店铺" clearable style="width: 180px" @change="search()">
        <el-option v-for="store in stores" :key="store.id" :label="store.name" :value="store.id" />
      </el-select>
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        range-separator="至"
        start-placeholder="下单开始"
        end-placeholder="下单结束"
        style="width: 260px"
        @change="search()"
      />
      <el-input v-model="query.q" placeholder="平台订单号/买家" clearable style="width: 200px" @keyup.enter="search()" />
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
      <div style="flex: 1" />
      <el-button v-if="canSync" type="primary" @click="openSync">手动同步</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="店铺" width="150" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.store_name) }}</template>
      </el-table-column>
      <el-table-column label="平台订单号" min-width="190">
        <template #default="{ row }"><span class="mono">{{ row.external_order_no }}</span></template>
      </el-table-column>
      <el-table-column label="状态" width="130">
        <template #default="{ row }">
          <el-tag :type="orderStatusType(row.status)" size="small">
            {{ orderStatusLabel(row.status) }}
          </el-tag>
          <el-tag v-if="row.abnormal_reason" size="small" type="danger" style="margin-left: 4px">异常</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="平台状态" width="150">
        <template #default="{ row }">{{ textOrDash(row.platform_status) }}</template>
      </el-table-column>
      <el-table-column label="金额" width="130" align="right">
        <template #default="{ row }">{{ formatMoney(row.total_amount, row.currency) }}</template>
      </el-table-column>
      <el-table-column label="买家" width="130" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.buyer_name) }}</template>
      </el-table-column>
      <el-table-column label="下单时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.placed_at) }}</template>
      </el-table-column>
      <el-table-column label="异常原因" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.abnormal_reason) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button text type="primary" size="small" @click="openDetail(row as Order)">详情</el-button>
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

  <el-dialog v-model="syncVisible" title="手动同步订单" width="440px" destroy-on-close>
    <el-form label-width="80px">
      <el-form-item label="店铺">
        <el-select v-model="syncStoreId" clearable placeholder="不选则同步全部店铺" style="width: 100%">
          <el-option v-for="store in stores" :key="store.id" :label="store.name" :value="store.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <el-alert
      title="同步任务按店铺与平台订单号防重，重复执行不会重复创建订单或重复扣减库存。"
      type="info"
      :closable="false"
      show-icon
    />
    <template #footer>
      <el-button @click="syncVisible = false">取消</el-button>
      <el-button type="primary" :loading="syncing" @click="submitSync">开始同步</el-button>
    </template>
  </el-dialog>
</template>
