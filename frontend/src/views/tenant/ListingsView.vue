<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { channelsApi } from '@/api/channels'
import { catalogApi } from '@/api/catalog'
import { useTableQuery } from '@/composables/useTableQuery'
import { useTenantTask } from '@/composables/useTenantTask'
import { useTenantStore } from '@/stores/tenant'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusTag from '@/components/StatusTag.vue'
import { formatDateTime, formatDecimal, stringOrUndefined, textOrDash } from '@/utils/format'
import type { Listing, Product, Store } from '@/types'

const tenant = useTenantStore()
const tid = (): string => tenant.requireTenantId()
const { submitAndPoll } = useTenantTask()

const canMap = computed(() => tenant.has('channel.listing.map'))

const query = reactive({
  store_id: '',
  mapping_status: '',
  q: '',
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
} = useTableQuery<Listing>((params) =>
  channelsApi.listings(tid(), {
    page: params.page,
    page_size: params.page_size,
    store_id: stringOrUndefined(query.store_id),
    mapping_status: stringOrUndefined(query.mapping_status),
    q: stringOrUndefined(query.q),
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

/* ------------------------------ 同步平台商品 ------------------------------ */
const syncVisible = ref(false)
const syncStoreId = ref('')
const syncing = ref(false)

function openSync(): void {
  syncStoreId.value = stores.value[0]?.id ?? ''
  syncVisible.value = true
}

async function submitSync(): Promise<void> {
  if (!syncStoreId.value) {
    ElMessage.warning('请选择店铺')
    return
  }
  syncing.value = true
  try {
    await submitAndPoll((tenantId) => channelsApi.syncListings(tenantId, syncStoreId.value), reload)
    syncVisible.value = false
  } finally {
    syncing.value = false
  }
}

/* ------------------------------ 手动映射 ------------------------------ */
const mapVisible = ref(false)
const mapSubmitting = ref(false)
const mappingListing = ref<Listing | null>(null)
const products = ref<Product[]>([])
const skuOptions = ref<Array<{ id: string; sku_code: string; name: string }>>([])
const mapForm = reactive({
  product_id: '',
  sku_id: '',
})

async function openMap(listing: Listing): Promise<void> {
  mappingListing.value = listing
  mapForm.product_id = listing.product_id ?? ''
  mapForm.sku_id = listing.sku_id ?? ''
  skuOptions.value = []
  mapVisible.value = true
  try {
    products.value = await catalogApi.productOptions(tid())
  } catch {
    products.value = []
  }
  if (mapForm.product_id) await loadSkus(mapForm.product_id)
}

async function loadSkus(productId: string): Promise<void> {
  if (!productId) {
    skuOptions.value = []
    return
  }
  try {
    const product = await catalogApi.product(tid(), productId)
    skuOptions.value = (product.skus ?? []).map((sku) => ({ id: sku.id, sku_code: sku.sku_code, name: sku.name }))
  } catch {
    skuOptions.value = []
  }
}

async function onProductChange(productId: string): Promise<void> {
  mapForm.sku_id = ''
  await loadSkus(productId)
}

async function submitMap(): Promise<void> {
  const listing = mappingListing.value
  if (!listing) return
  if (!mapForm.product_id || !mapForm.sku_id) {
    ElMessage.warning('请选择商品与 SKU')
    return
  }
  if (skuOptions.value.length === 0) {
    ElMessage.warning('该商品没有可用 SKU')
    return
  }
  mapSubmitting.value = true
  try {
    await channelsApi.linkListing(tid(), listing.id, {
      product_id: mapForm.product_id,
      sku_id: mapForm.sku_id,
    })
    ElMessage.success('映射已保存')
    mapVisible.value = false
    await reload()
  } finally {
    mapSubmitting.value = false
  }
}

async function unlink(listing: Listing): Promise<void> {
  try {
    await ElMessageBox.confirm('确认解除该平台商品与本地 SKU 的映射？', '解除映射', {
      type: 'warning',
      confirmButtonText: '确认解除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await channelsApi.unlinkListing(tid(), listing.id)
  ElMessage.success('映射已解除')
  await reload()
}

function mappingStatus(listing: Listing): string {
  return listing.link_status ?? listing.mapping_status ?? 'missing'
}

onMounted(loadStores)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-select v-model="query.store_id" placeholder="店铺" clearable style="width: 200px" @change="search()">
        <el-option v-for="store in stores" :key="store.id" :label="store.name" :value="store.id" />
      </el-select>
      <el-select v-model="query.mapping_status" placeholder="映射状态" clearable style="width: 160px" @change="search()">
        <el-option label="已映射" value="mapped" />
        <el-option label="待处理" value="pending" />
        <el-option label="待人工确认" value="ambiguous" />
        <el-option label="未映射" value="missing" />
      </el-select>
      <el-input v-model="query.q" placeholder="平台商品标题" clearable style="width: 200px" @keyup.enter="search()" />
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
      <div style="flex: 1" />
      <el-button v-if="canMap" type="primary" @click="openSync">同步平台商品</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="店铺" width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.store_name) }}</template>
      </el-table-column>
      <el-table-column label="平台商品 ID" width="160">
        <template #default="{ row }"><span class="mono">{{ textOrDash(row.external_item_id) }}</span></template>
      </el-table-column>
      <el-table-column label="平台 SKU" width="150">
        <template #default="{ row }"><span class="mono">{{ textOrDash(row.external_sku_id) }}</span></template>
      </el-table-column>
      <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
      <el-table-column label="价格" width="130" align="right">
        <template #default="{ row }">{{ formatDecimal(row.price) }} {{ row.currency ?? '' }}</template>
      </el-table-column>
      <el-table-column label="远端库存" width="100" align="right">
        <template #default="{ row }">{{ formatDecimal(row.remote_stock) }}</template>
      </el-table-column>
      <el-table-column label="映射状态" width="130">
        <template #default="{ row }">
          <StatusTag :status="mappingStatus(row as Listing)" />
          <el-tag v-if="row.mapping_source" size="small" type="info" style="margin-left: 4px">
            {{ row.mapping_source === 'auto' ? '自动' : '手动' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="本地商品 / SKU" min-width="190" show-overflow-tooltip>
        <template #default="{ row }">
          <div>{{ textOrDash(row.product_name) }}</div>
          <div class="text-muted mono">{{ textOrDash(row.sku_code) }}</div>
        </template>
      </el-table-column>
      <el-table-column label="同步时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.synced_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button v-if="canMap" text type="primary" size="small" @click="openMap(row as Listing)">映射</el-button>
          <el-button v-if="canMap && mappingStatus(row as Listing) === 'mapped'" text type="danger" size="small" @click="unlink(row as Listing)">
            解除
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

  <!-- 同步平台商品 -->
  <el-dialog v-model="syncVisible" title="同步平台商品" width="460px" destroy-on-close>
    <el-form label-width="80px">
      <el-form-item label="店铺">
        <el-select v-model="syncStoreId" placeholder="选择店铺" style="width: 100%">
          <el-option v-for="store in stores" :key="store.id" :label="store.name" :value="store.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <el-alert
      title="后台任务会读取该店铺的平台商品与规格；自动匹配只接受唯一明确结果。"
      type="info"
      :closable="false"
      show-icon
    />
    <template #footer>
      <el-button @click="syncVisible = false">取消</el-button>
      <el-button type="primary" :loading="syncing" @click="submitSync">开始同步</el-button>
    </template>
  </el-dialog>

  <!-- 手动映射 -->
  <el-dialog v-model="mapVisible" title="手动映射" width="560px" destroy-on-close>
    <el-descriptions :column="1" border size="small" class="detail-descriptions">
      <el-descriptions-item label="平台商品">{{ mappingListing?.title }}</el-descriptions-item>
      <el-descriptions-item label="平台商品 ID">{{ mappingListing?.external_item_id }}</el-descriptions-item>
      <el-descriptions-item label="平台 SKU">{{ textOrDash(mappingListing?.external_sku_id) }}</el-descriptions-item>
    </el-descriptions>
    <el-form label-width="88px">
      <el-form-item label="本地商品">
        <el-select v-model="mapForm.product_id" filterable clearable placeholder="选择商品" style="width: 100%" @change="onProductChange">
          <el-option
            v-for="product in products"
            :key="product.id"
            :label="`${product.name}（${product.code}）`"
            :value="product.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="本地 SKU">
        <el-select v-model="mapForm.sku_id" filterable placeholder="选择 SKU" style="width: 100%">
          <el-option v-for="sku in skuOptions" :key="sku.id" :label="`${sku.sku_code} ${sku.name}`" :value="sku.id" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="mapVisible = false">取消</el-button>
      <el-button type="primary" :loading="mapSubmitting" @click="submitMap">保存映射</el-button>
    </template>
  </el-dialog>
</template>
