<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { catalogApi } from '@/api/catalog'
import { useTenantStore } from '@/stores/tenant'
import { useTableQuery } from '@/composables/useTableQuery'
import PaginationBar from '@/components/PaginationBar.vue'
import SpuBasicFields from '@/components/SpuBasicFields.vue'
import SkuEditor from '@/components/SkuEditor.vue'
import { formatDecimal, stringOrUndefined } from '@/utils/format'
import { catalogDateTime, emptyOptions, salesTypes, spuStatuses, spuForm, spuPayload, statusLabel, summaryText, typeLabel } from '@/utils/catalog'
import type { Product, ProductCreatePayload, ProductSkuPayload } from '@/types/catalog'
const router = useRouter()
const tenant = useTenantStore()
const tid = () => tenant.requireTenantId()
const canCreate = computed(() => tenant.has('catalog.product.create'))
const canUpdate = computed(() => tenant.has('catalog.product.update'))
const canCost = computed(() => options.value.can_read_cost === true)
const options = ref(emptyOptions())
async function loadOptions(): Promise<void> { options.value = await catalogApi.options(tid()) }
const query = reactive({ q: '', sales_status: '', sales_type: '', status: 'active', brand: '', category: '', preferred_supplier_id: '', purchaser_id: '', developer_id: '', inquiry_buyer_id: '', tag: '' })
const moreFilters = ref(false)
const table = useTableQuery<Product>((params) => catalogApi.products(tid(), {
  page: params.page, page_size: params.page_size,
  ...Object.fromEntries(Object.entries(query).map(([key, value]) => [key, stringOrUndefined(value)])),
}))
const { rows, total, page, pageSize, loading, search, reload, handlePageChange, handleSizeChange } = table
const columns = [
  { key: 'name_en', label: '英文名称', width: 180 }, { key: 'brand', label: '品牌', width: 100 },
  { key: 'category', label: '分类', width: 110 }, { key: 'preferred_supplier_id', label: '首选供应商', width: 140 },
  { key: 'sku_count', label: 'SKU 数量', width: 90 }, { key: 'sales_type', label: '销售类型', width: 110 },
  { key: 'sales_status', label: '状态', width: 110 }, { key: 'current', label: '当前成本', width: 140, cost: true },
  { key: 'guidance', label: '指导成本', width: 140, cost: true }, { key: 'average', label: '平均成本', width: 140, cost: true },
  { key: 'weight_summary', label: '重量 g', width: 140 }, { key: 'purchaser_id', label: '采购员', width: 110 },
  { key: 'developer_id', label: '业务开发员', width: 120 }, { key: 'inquiry_buyer_id', label: '采购询价员', width: 120 },
  { key: 'hs_code', label: '海关编码', width: 130 }, { key: 'material_zh', label: '中文材质', width: 140 },
  { key: 'material_en', label: '英文材质', width: 140 }, { key: 'usage', label: '用途', width: 160 },
  { key: 'feature_tags', label: '特性标签', width: 150 }, { key: 'packaging_name', label: '包材名称', width: 130 },
  { key: 'dimensions', label: '商品尺寸 cm', width: 160 }, { key: 'package_dimensions', label: '包装尺寸 cm', width: 160 },
  { key: 'remark', label: '备注', width: 180 }, { key: 'features', label: '产品特点', width: 180 },
  { key: 'created_at', label: '创建日期', width: 170 }, { key: 'updated_at', label: '更新日期', width: 170 },
]
const availableColumns = computed(() => columns.filter((column) => !column.cost || canCost.value))
const defaults = ['name_en', 'category', 'preferred_supplier_id', 'sku_count', 'sales_type', 'sales_status', 'current', 'weight_summary', 'updated_at']
const preferenceKey = computed(() => `catalog-columns:${tid()}`)
function savedColumns(): string[] { try { const saved = JSON.parse(localStorage.getItem(preferenceKey.value) ?? 'null') as unknown; return Array.isArray(saved) ? saved.filter((x): x is string => typeof x === 'string' && columns.some((c) => c.key === x)) : [...defaults] } catch { return [...defaults] } }
const selectedColumns = ref(savedColumns())
watch(selectedColumns, (value) => localStorage.setItem(preferenceKey.value, JSON.stringify(value)), { deep: true })
function display(row: Product, field: string): string {
  if (field === 'sales_status') return statusLabel(row.sales_status)
  if (field === 'sales_type') return typeLabel(row.sales_type)
  if (field === 'current' || field === 'guidance' || field === 'average') return summaryText(row.cost_summary?.[field])
  if (field === 'weight_summary') return summaryText(row.weight_summary, true)
  if (field === 'preferred_supplier_id') return row.preferred_supplier_name ?? options.value.suppliers.find((s) => s.id === row.preferred_supplier_id)?.name ?? '—'
  if (['purchaser_id', 'developer_id', 'inquiry_buyer_id'].includes(field)) return row[`${field.replace('_id', '')}_name` as 'purchaser_name'] ?? options.value.members.find((m) => m.id === row[field as 'purchaser_id'])?.name ?? '—'
  if (field === 'created_at' || field === 'updated_at') return catalogDateTime(row[field])
  if (field === 'dimensions') return [row.length_cm, row.width_cm, row.height_cm].map((v) => v == null ? '—' : formatDecimal(v)).join(' × ')
  if (field === 'package_dimensions') return [row.package_length_cm, row.package_width_cm, row.package_height_cm].map((v) => v == null ? '—' : formatDecimal(v)).join(' × ')
  const value = row[field as keyof Product]
  return Array.isArray(value) ? value.join('、') : value == null || value === '' ? '—' : String(value)
}
const createVisible = ref(false)
const creating = ref(false)
const form = ref(spuForm())
const skus = ref<ProductSkuPayload[]>([])
const skuVisible = ref(false)
const editingSkuIndex = ref(-1)
const editingSku = computed(() => editingSkuIndex.value < 0 ? null : skus.value[editingSkuIndex.value] ?? null)
function openCreate(): void { form.value = spuForm(); skus.value = []; createVisible.value = true }
function editSku(index = -1): void { editingSkuIndex.value = index; skuVisible.value = true }
function saveSku(payload: ProductSkuPayload): void {
  if (editingSkuIndex.value >= 0) skus.value[editingSkuIndex.value] = payload
  else skus.value.push(payload)
  skuVisible.value = false
}
async function create(): Promise<void> {
  let payload: ProductCreatePayload
  try { if (!skus.value.length) throw new Error('请至少新增一个 SKU'); payload = { ...spuPayload(form.value), code: form.value.code.trim(), name: form.value.name.trim(), skus: skus.value } } catch (error) { ElMessage.warning(error instanceof Error ? error.message : '请检查输入'); return }
  creating.value = true
  try { const product = await catalogApi.createProduct(tid(), payload); createVisible.value = false; ElMessage.success('SPU 已创建'); await router.push(`/products/${product.id}`) } finally { creating.value = false }
}
async function archive(product: Product): Promise<void> {
  try { await ElMessageBox.confirm(`确认归档 ${product.code}？`, '归档商品', { type: 'warning' }) } catch { return }
  await catalogApi.archiveProduct(tid(), product.id); ElMessage.success('商品已归档'); await reload()
}
onMounted(loadOptions)
</script>
<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.q" clearable placeholder="SPU / SKU / 别名 / 中英文名称" style="width:300px" @keyup.enter="search()" />
      <el-select v-model="query.sales_status" clearable placeholder="销售状态" style="width:130px" @change="search()"><el-option v-for="item in spuStatuses" :key="item.value" :label="item.label" :value="item.value" /></el-select>
      <el-select v-model="query.sales_type" clearable placeholder="销售类型" style="width:130px" @change="search()"><el-option v-for="item in salesTypes" :key="item.value" :label="item.label" :value="item.value" /></el-select>
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button><el-button :icon="Refresh" @click="reload()">刷新</el-button><el-button text @click="moreFilters = !moreFilters">更多筛选</el-button>
      <div style="flex:1" />
      <el-popover trigger="click" width="230"><template #reference><el-button>显示列</el-button></template><el-checkbox-group v-model="selectedColumns" class="column-options"><el-checkbox v-for="column in availableColumns" :key="column.key" :label="column.label" :value="column.key" /></el-checkbox-group></el-popover>
      <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openCreate">新建 SPU</el-button>
    </div>
    <div v-if="moreFilters" class="filter-bar secondary-filters">
      <el-select v-model="query.brand" clearable filterable placeholder="品牌" @change="search()"><el-option v-for="item in options.brand" :key="item.id" :label="item.name" :value="item.name" /></el-select>
      <el-select v-model="query.category" clearable filterable placeholder="分类" @change="search()"><el-option v-for="item in options.category" :key="item.id" :label="item.name" :value="item.name" /></el-select>
      <el-select v-model="query.preferred_supplier_id" clearable filterable placeholder="首选供应商" @change="search()"><el-option v-for="item in options.suppliers" :key="item.id" :label="item.name" :value="item.id" /></el-select>
      <el-select v-for="item in [{ key: 'purchaser_id', label: '采购员' }, { key: 'developer_id', label: '业务开发员' }, { key: 'inquiry_buyer_id', label: '采购询价员' }] as const" :key="item.key" v-model="query[item.key]" clearable filterable :placeholder="item.label" @change="search()"><el-option v-for="person in options.members" :key="person.id" :label="person.name" :value="person.id" /></el-select>
      <el-select v-model="query.tag" clearable filterable placeholder="特性标签" @change="search()"><el-option v-for="item in options.tag" :key="item.id" :label="item.name" :value="item.name" /></el-select>
      <el-select v-model="query.status" clearable placeholder="归档状态" @change="search()"><el-option label="未归档" value="active" /><el-option label="已归档" value="archived" /></el-select>
    </div>
    <el-table v-loading="loading" :data="rows" border row-key="id">
      <el-table-column label="图片" width="80"><template #default="{ row }"><el-image v-if="row.primary_image || row.images?.length" :src="row.primary_image || row.images[0].url" fit="cover" style="width:48px;height:48px" /><span v-else>—</span></template></el-table-column>
      <el-table-column label="SPU 编号" width="155" fixed><template #default="{ row }"><el-link type="primary" @click="router.push(`/products/${row.id}`)">{{ row.code }}</el-link></template></el-table-column>
      <el-table-column label="商品中文名称" min-width="200"><template #default="{ row }"><el-link @click="router.push(`/products/${row.id}`)">{{ row.name }}</el-link><el-tag v-if="row.status === 'archived'" size="small" type="info" style="margin-left:6px">已归档</el-tag></template></el-table-column>
      <el-table-column v-for="column in availableColumns.filter((c) => selectedColumns.includes(c.key))" :key="column.key" :label="column.label" :width="column.width" show-overflow-tooltip><template #default="{ row }">{{ display(row as Product, column.key) }}</template></el-table-column>
      <el-table-column label="操作" width="130" fixed="right"><template #default="{ row }"><el-button text type="primary" @click="router.push(`/products/${row.id}`)">详情</el-button><el-button v-if="canUpdate && row.status !== 'archived'" text type="danger" @click="archive(row as Product)">归档</el-button></template></el-table-column>
    </el-table>
    <PaginationBar :total="total" :page="page" :page-size="pageSize" @page-change="handlePageChange" @size-change="handleSizeChange" />
  </el-card>
  <el-dialog v-model="createVisible" title="新建 SPU" width="min(1120px, 96vw)" :close-on-click-modal="false" destroy-on-close>
    <div class="create-body"><SpuBasicFields :model="form" :options="options" />
      <div class="sku-heading"><strong>销售信息</strong><el-button type="primary" plain @click="editSku()">新增 SKU</el-button></div>
      <el-table :data="skus" border><el-table-column prop="sku_code" label="SKU" /><el-table-column label="别名"><template #default="{ row }">{{ row.aliases?.join('、') || '—' }}</template></el-table-column><el-table-column label="规格"><template #default="{ row }">{{ Object.entries(row.specs || {}).map(([k, v]) => `${k}：${v}`).join(' / ') || '—' }}</template></el-table-column><el-table-column label="状态"><template #default="{ row }">{{ statusLabel(row.sales_status) }}</template></el-table-column><el-table-column label="操作" width="130"><template #default="{ $index }"><el-button text @click="editSku($index)">编辑</el-button><el-button text type="danger" @click="skus.splice($index, 1)">移除</el-button></template></el-table-column></el-table>
    </div><template #footer><el-button @click="createVisible = false">取消</el-button><el-button type="primary" :loading="creating" @click="create">创建</el-button></template>
  </el-dialog>
  <SkuEditor v-model="skuVisible" :sku="editingSku" :can-read-cost="canCost" @save="saveSku" />
</template>
<style scoped>
.column-options { display:flex; flex-direction:column; max-height:60vh; overflow:auto; }.column-options :deep(.el-checkbox) { margin-right:0; }
.secondary-filters .el-select { width:160px; }.create-body { max-height:68vh; overflow:auto; padding-right:12px; }.sku-heading { display:flex; justify-content:space-between; align-items:center; margin:16px 0; }
</style>
