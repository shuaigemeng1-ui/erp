<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { inventoryApi } from '@/api/inventory'
import { catalogApi } from '@/api/catalog'
import { useTableQuery } from '@/composables/useTableQuery'
import { useTenantStore } from '@/stores/tenant'
import PaginationBar from '@/components/PaginationBar.vue'
import IdCopy from '@/components/IdCopy.vue'
import { formatDateTime, formatDecimal, stringOrUndefined, textOrDash } from '@/utils/format'
import { labelOf, movementTypeLabels } from '@/utils/dict'
import type { InventoryBalance, Product, StockMovement, StockPlace } from '@/types'

const tenant = useTenantStore()
const tid = (): string => tenant.requireTenantId()

const canAdjust = computed(() => tenant.has('inventory.adjust'))
const canReadProducts = computed(() => tenant.has('catalog.product.read'))

const activeTab = ref('balances')
const stockPlaces = ref<StockPlace[]>([])

const balanceQuery = reactive({
  q: '',
  stock_place_id: '',
})

const balances = useTableQuery<InventoryBalance>((params) =>
  inventoryApi.balances(tid(), {
    page: params.page,
    page_size: params.page_size,
    q: stringOrUndefined(balanceQuery.q),
    stock_place_id: stringOrUndefined(balanceQuery.stock_place_id),
  }),
)

const movementQuery = reactive({
  sku_id: '',
  stock_place_id: '',
})

const movements = useTableQuery<StockMovement>((params) =>
  inventoryApi.movements(tid(), {
    page: params.page,
    page_size: params.page_size,
    sku_id: stringOrUndefined(movementQuery.sku_id),
    stock_place_id: stringOrUndefined(movementQuery.stock_place_id),
  }),
)

async function loadStockPlaces(): Promise<void> {
  try {
    stockPlaces.value = await inventoryApi.stockPlaces(tid())
  } catch {
    stockPlaces.value = []
  }
}

/* ------------------------------ 库存调整 ------------------------------ */
const adjustVisible = ref(false)
const adjustSubmitting = ref(false)
const adjustFormRef = ref<FormInstance>()
const productOptions = ref<Product[]>([])
const skuOptions = ref<Array<{ id: string; sku_code: string; name: string }>>([])
const skuLoading = ref(false)
const adjustForm = reactive({
  product_id: '',
  sku_id: '',
  stock_place_id: '',
  quantity_change: 0,
  note: '',
})
const adjustRules: FormRules = {
  sku_id: [{ required: true, message: '请选择 SKU', trigger: 'change' }],
  stock_place_id: [{ required: true, message: '请选择库存地点', trigger: 'change' }],
  quantity_change: [{ required: true, message: '请输入调整数量', trigger: 'blur' }],
  note: [{ required: true, message: '请填写调整原因', trigger: 'blur' }],
}

async function openAdjust(balance?: InventoryBalance): Promise<void> {
  adjustForm.product_id = balance?.product_id ?? ''
  adjustForm.sku_id = balance?.sku_id ?? ''
  adjustForm.stock_place_id = balance?.stock_place_id ?? stockPlaces.value.find((place) => place.is_default)?.id ?? ''
  adjustForm.quantity_change = 0
  adjustForm.note = ''
  skuOptions.value = []
  adjustVisible.value = true

  if (canReadProducts.value) {
    try {
      productOptions.value = await catalogApi.productOptions(tid())
    } catch {
      productOptions.value = []
    }
    if (balance?.product_id) await loadSkus(balance.product_id)
  }
}

async function loadSkus(productId: string): Promise<void> {
  if (!productId) {
    skuOptions.value = []
    return
  }
  skuLoading.value = true
  try {
    const product = await catalogApi.product(tid(), productId)
    skuOptions.value = (product.skus ?? []).map((sku) => ({ id: sku.id, sku_code: sku.sku_code, name: sku.name }))
  } catch {
    skuOptions.value = []
  } finally {
    skuLoading.value = false
  }
}

async function onProductChange(productId: string): Promise<void> {
  adjustForm.sku_id = ''
  await loadSkus(productId)
}

async function submitAdjust(): Promise<void> {
  const formInstance = adjustFormRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  if (!adjustForm.quantity_change || Number(adjustForm.quantity_change) === 0) {
    ElMessage.warning('调整数量不能为 0')
    return
  }
  adjustSubmitting.value = true
  try {
    const result = await inventoryApi.adjust(tid(), {
      sku_id: adjustForm.sku_id,
      stock_place_id: adjustForm.stock_place_id,
      quantity_change: Number(adjustForm.quantity_change),
      note: adjustForm.note.trim(),
    })
    ElMessage.success(
      result.available_quantity !== undefined
        ? `调整成功，可用数量 ${formatDecimal(result.available_quantity)}`
        : '库存调整成功',
    )
    adjustVisible.value = false
    await Promise.all([balances.reload(), movements.reload()])
  } finally {
    adjustSubmitting.value = false
  }
}

onMounted(() => {
  void loadStockPlaces()
})
</script>

<template>
  <el-card shadow="never" class="page-card">
    <el-tabs v-model="activeTab">
      <el-tab-pane label="库存余额" name="balances">
        <div class="filter-bar">
          <el-input v-model="balanceQuery.q" placeholder="SKU 编码/商品名称" clearable style="width: 220px" @keyup.enter="balances.search()" />
          <el-select v-model="balanceQuery.stock_place_id" placeholder="库存地点" clearable style="width: 180px" @change="balances.search()">
            <el-option v-for="place in stockPlaces" :key="place.id" :label="place.name" :value="place.id" />
          </el-select>
          <el-button type="primary" :icon="Search" @click="balances.search()">查询</el-button>
          <el-button :icon="Refresh" @click="balances.reload()">刷新</el-button>
          <div style="flex: 1" />
          <el-button v-if="canAdjust" type="primary" @click="openAdjust()">库存调整</el-button>
        </div>

        <el-table v-loading="balances.loading.value" :data="balances.rows.value" border size="small" row-key="id">
          <el-table-column label="SKU" min-width="160">
            <template #default="{ row }">
              <div>{{ textOrDash(row.sku_code) }}</div>
              <div class="text-muted">{{ textOrDash(row.product_name) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="SKU ID" width="110">
            <template #default="{ row }"><IdCopy :id="row.sku_id" /></template>
          </el-table-column>
          <el-table-column label="库存地点" min-width="150">
            <template #default="{ row }">{{ textOrDash(row.stock_place_name) }}</template>
          </el-table-column>
          <el-table-column label="可用数量" width="120" align="right">
            <template #default="{ row }">
              <span :style="{ color: Number(row.available_quantity) < 0 ? '#f56c6c' : undefined }">
                {{ formatDecimal(row.available_quantity) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="占用数量" width="120" align="right">
            <template #default="{ row }">{{ formatDecimal(row.reserved_quantity) }}</template>
          </el-table-column>
          <el-table-column label="版本" width="80" align="center">
            <template #default="{ row }">{{ row.version ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button v-if="canAdjust" text type="primary" size="small" @click="openAdjust(row as InventoryBalance)">调整</el-button>
            </template>
          </el-table-column>
        </el-table>

        <PaginationBar
          :total="balances.total.value"
          :page="balances.page.value"
          :page-size="balances.pageSize.value"
          @page-change="balances.handlePageChange"
          @size-change="balances.handleSizeChange"
        />
      </el-tab-pane>

      <el-tab-pane label="库存流水" name="movements">
        <div class="filter-bar">
          <el-input v-model="movementQuery.sku_id" placeholder="SKU ID" clearable style="width: 180px" @keyup.enter="movements.search()" />
          <el-select v-model="movementQuery.stock_place_id" placeholder="库存地点" clearable style="width: 180px" @change="movements.search()">
            <el-option v-for="place in stockPlaces" :key="place.id" :label="place.name" :value="place.id" />
          </el-select>
          <el-button type="primary" :icon="Search" @click="movements.search()">查询</el-button>
          <el-button :icon="Refresh" @click="movements.reload()">刷新</el-button>
        </div>

        <el-table v-loading="movements.loading.value" :data="movements.rows.value" border size="small" row-key="id">
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="SKU" min-width="150">
            <template #default="{ row }">
              <div>{{ textOrDash(row.sku_code) }}</div>
              <div class="text-muted mono">{{ row.sku_id }}</div>
            </template>
          </el-table-column>
          <el-table-column label="库存地点" min-width="140">
            <template #default="{ row }">{{ textOrDash(row.stock_place_name) }}</template>
          </el-table-column>
          <el-table-column label="变动" width="110" align="right">
            <template #default="{ row }">
              <span :style="{ color: Number(row.quantity_change) < 0 ? '#f56c6c' : '#67c23a' }">
                {{ formatDecimal(row.quantity_change) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="变动后" width="110" align="right">
            <template #default="{ row }">{{ formatDecimal(row.balance_after) }}</template>
          </el-table-column>
          <el-table-column label="类型" width="130">
            <template #default="{ row }">{{ labelOf(movementTypeLabels, row.movement_type) }}</template>
          </el-table-column>
          <el-table-column label="引用" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">
              {{ textOrDash(row.reference_type) }} / {{ textOrDash(row.reference_id) }}
            </template>
          </el-table-column>
          <el-table-column label="操作人" width="110">
            <template #default="{ row }"><IdCopy :id="row.operator_id" /></template>
          </el-table-column>
          <el-table-column label="备注" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ textOrDash(row.note) }}</template>
          </el-table-column>
        </el-table>

        <PaginationBar
          :total="movements.total.value"
          :page="movements.page.value"
          :page-size="movements.pageSize.value"
          @page-change="movements.handlePageChange"
          @size-change="movements.handleSizeChange"
        />
      </el-tab-pane>

      <el-tab-pane label="库存地点" name="places">
        <div class="filter-bar">
          <el-button :icon="Refresh" @click="loadStockPlaces">刷新</el-button>
        </div>
        <el-table :data="stockPlaces" border size="small" row-key="id">
          <el-table-column label="ID" width="110">
            <template #default="{ row }"><IdCopy :id="row.id" /></template>
          </el-table-column>
          <el-table-column prop="code" label="编码" width="160" />
          <el-table-column prop="name" label="名称" min-width="180" />
          <el-table-column label="默认" width="100">
            <template #default="{ row }">
              <el-tag :type="row.is_default ? 'success' : 'info'" size="small">{{ row.is_default ? '默认' : '否' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">{{ row.status }}</template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </el-card>

  <!-- 库存调整 -->
  <el-dialog v-model="adjustVisible" title="库存调整" width="560px" destroy-on-close>
    <el-form ref="adjustFormRef" :model="adjustForm" :rules="adjustRules" label-width="110px">
      <el-form-item v-if="canReadProducts" label="商品">
        <el-select
          v-model="adjustForm.product_id"
          filterable
          clearable
          placeholder="选择商品后加载 SKU"
          style="width: 100%"
          @change="onProductChange"
        >
          <el-option
            v-for="product in productOptions"
            :key="product.id"
            :label="`${product.name}（${product.code}）`"
            :value="product.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="SKU" prop="sku_id">
        <el-select
          v-if="canReadProducts"
          v-model="adjustForm.sku_id"
          filterable
          :loading="skuLoading"
          placeholder="选择 SKU"
          style="width: 100%"
        >
          <el-option
            v-for="sku in skuOptions"
            :key="sku.id"
            :label="`${sku.sku_code} ${sku.name}`"
            :value="sku.id"
          />
        </el-select>
        <el-input v-else v-model="adjustForm.sku_id" placeholder="SKU ID" />
      </el-form-item>
      <el-form-item label="库存地点" prop="stock_place_id">
        <el-select v-model="adjustForm.stock_place_id" placeholder="选择库存地点" style="width: 100%">
          <el-option v-for="place in stockPlaces" :key="place.id" :label="place.name" :value="place.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="调整数量" prop="quantity_change">
        <el-input-number v-model="adjustForm.quantity_change" :precision="0" :controls="false" style="width: 100%" />
        <span class="form-tip">正数入库，负数出库；人工调整禁止产生负库存。</span>
      </el-form-item>
      <el-form-item label="调整原因" prop="note">
        <el-input v-model="adjustForm.note" type="textarea" :rows="2" placeholder="必填，写入库存流水与审计" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="adjustVisible = false">取消</el-button>
      <el-button type="primary" :loading="adjustSubmitting" @click="submitAdjust">确认调整</el-button>
    </template>
  </el-dialog>
</template>
