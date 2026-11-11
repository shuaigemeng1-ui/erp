<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft } from '@element-plus/icons-vue'
import { salesApi } from '@/api/sales'
import { useTenantStore } from '@/stores/tenant'
import StatusTag from '@/components/StatusTag.vue'
import IdCopy from '@/components/IdCopy.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { formatDateTime, formatDecimal, formatMoney, textOrDash } from '@/utils/format'
import type { OrderDetail } from '@/types'

const route = useRoute()
const router = useRouter()
const tenant = useTenantStore()

const orderId = String(route.params.orderId ?? '')
const loading = ref(true)
const detail = ref<OrderDetail | null>(null)
const activeTab = ref('items')

onMounted(async () => {
  loading.value = true
  try {
    detail.value = await salesApi.order(tenant.requireTenantId(), orderId)
  } catch {
    detail.value = null
  } finally {
    loading.value = false
  }
})

function back(): void {
  void router.push('/orders')
}
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="table-toolbar">
      <div class="toolbar-actions">
        <el-button :icon="ArrowLeft" @click="back">返回列表</el-button>
      </div>
      <el-tag v-if="detail" size="small" type="info">订单 {{ detail.external_order_no }}</el-tag>
    </div>

    <el-skeleton v-if="loading" :rows="6" animated />
    <el-empty v-else-if="!detail" description="订单不存在或不可见" />

    <template v-else>
      <el-descriptions :column="3" border size="small" class="detail-descriptions">
        <el-descriptions-item label="订单 ID"><IdCopy :id="detail.id" /></el-descriptions-item>
        <el-descriptions-item label="平台订单号">
          <span class="mono">{{ detail.external_order_no }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="店铺">{{ textOrDash(detail.store_name) }}</el-descriptions-item>
        <el-descriptions-item label="渠道">{{ detail.channel }}</el-descriptions-item>
        <el-descriptions-item label="状态"><StatusTag :status="detail.status" /></el-descriptions-item>
        <el-descriptions-item label="平台状态">{{ textOrDash(detail.platform_status) }}</el-descriptions-item>
        <el-descriptions-item label="金额">{{ formatMoney(detail.total_amount, detail.currency) }}</el-descriptions-item>
        <el-descriptions-item label="买家">{{ textOrDash(detail.buyer_name) }}</el-descriptions-item>
        <el-descriptions-item label="下单时间">{{ formatDateTime(detail.placed_at) }}</el-descriptions-item>
        <el-descriptions-item label="同步时间">{{ formatDateTime(detail.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="异常原因" :span="2">
          {{ textOrDash(detail.abnormal_reason) }}
        </el-descriptions-item>
      </el-descriptions>

      <el-tabs v-model="activeTab">
        <el-tab-pane label="订单明细" name="items">
          <el-table :data="detail.items" border size="small" empty-text="无明细">
            <el-table-column label="明细 ID" width="110">
              <template #default="{ row }"><IdCopy :id="row.id" /></template>
            </el-table-column>
            <el-table-column label="平台商品/SKU" min-width="200">
              <template #default="{ row }">
                <div class="mono">{{ textOrDash(row.external_item_id) }}</div>
                <div class="mono text-muted">{{ textOrDash(row.external_sku_id) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="本地 SKU" min-width="160">
              <template #default="{ row }">
                <div>{{ textOrDash(row.product_name) }}</div>
                <div class="mono text-muted">{{ textOrDash(row.sku_code) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="数量" width="90" align="right">
              <template #default="{ row }">{{ formatDecimal(row.quantity) }}</template>
            </el-table-column>
            <el-table-column label="单价" width="110" align="right">
              <template #default="{ row }">{{ formatDecimal(row.unit_price) }}</template>
            </el-table-column>
            <el-table-column label="优惠" width="100" align="right">
              <template #default="{ row }">{{ formatDecimal(row.discount_amount) }}</template>
            </el-table-column>
            <el-table-column label="明细状态" width="120">
              <template #default="{ row }">{{ textOrDash(row.item_status) }}</template>
            </el-table-column>
            <el-table-column label="映射状态" width="120">
              <template #default="{ row }"><StatusTag :status="row.mapping_status" /></template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="状态历史" name="history">
          <el-table :data="detail.status_history" border size="small" empty-text="无状态历史">
            <el-table-column label="变更时间" width="180">
              <template #default="{ row }">{{ formatDateTime(row.changed_at) }}</template>
            </el-table-column>
            <el-table-column label="原状态" width="140">
              <template #default="{ row }">{{ textOrDash(row.from_status) }}</template>
            </el-table-column>
            <el-table-column label="新状态" width="140">
              <template #default="{ row }"><StatusTag :status="row.to_status" /></template>
            </el-table-column>
            <el-table-column label="平台状态" width="140">
              <template #default="{ row }">{{ textOrDash(row.platform_status) }}</template>
            </el-table-column>
            <el-table-column label="来源" min-width="120">
              <template #default="{ row }">{{ textOrDash(row.source) }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="库存影响" name="stock">
          <el-table :data="detail.stock_effects" border size="small" empty-text="无库存影响记录">
            <el-table-column label="明细 ID" width="110">
              <template #default="{ row }"><IdCopy :id="row.order_item_id" /></template>
            </el-table-column>
            <el-table-column label="SKU" min-width="150">
              <template #default="{ row }">
                <div class="mono">{{ textOrDash(row.sku_code) }}</div>
                <div class="mono text-muted">{{ textOrDash(row.sku_id) }}</div>
              </template>
            </el-table-column>
            <el-table-column label="库存地点" width="130">
              <template #default="{ row }"><IdCopy :id="row.stock_place_id" /></template>
            </el-table-column>
            <el-table-column label="已扣数量" width="110" align="right">
              <template #default="{ row }">{{ formatDecimal(row.applied_quantity) }}</template>
            </el-table-column>
            <el-table-column label="应扣数量" width="110" align="right">
              <template #default="{ row }">{{ formatDecimal(row.target_quantity) }}</template>
            </el-table-column>
            <el-table-column label="状态" width="110">
              <template #default="{ row }"><StatusTag :status="row.status" /></template>
            </el-table-column>
            <el-table-column label="最近任务" width="120">
              <template #default="{ row }"><IdCopy :id="row.last_task_id" /></template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="原始数据" name="raw">
          <JsonBlock :value="detail.raw" empty-text="接口未返回原始数据" />
        </el-tab-pane>
      </el-tabs>
    </template>
  </el-card>
</template>
