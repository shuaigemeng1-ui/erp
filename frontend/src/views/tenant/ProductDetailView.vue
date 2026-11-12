<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { catalogApi } from '@/api/catalog'
import { useTenantStore } from '@/stores/tenant'
import SpuBasicFields from '@/components/SpuBasicFields.vue'
import SkuEditor from '@/components/SkuEditor.vue'
import { catalogDateTime, decimalValue, emptyOptions, salesStatuses, spuForm, spuPayload, statusLabel, summaryText, typeLabel } from '@/utils/catalog'
import { formatDecimal } from '@/utils/format'
import type { Product, ProductSku, ProductSkuPayload, ProductLog, ProductChange } from '@/types/catalog'
const route = useRoute()
const router = useRouter()
const tenant = useTenantStore()
const tid = () => tenant.requireTenantId()
const productId = computed(() => String(route.params.productId ?? ''))
const canUpdate = computed(() => tenant.has('catalog.product.update'))
const canCost = computed(() => options.value.can_read_cost === true)
const product = ref<Product>()
const options = ref(emptyOptions())
const form = ref(spuForm())
const busy = ref(false)
const saving = ref(false)
const activeTab = ref('basic')
let loadSequence = 0
async function loadOptions(): Promise<void> { options.value = await catalogApi.options(tid()) }
async function load(): Promise<void> {
  const sequence = ++loadSequence
  busy.value = true
  try {
    const data = await catalogApi.product(tid(), productId.value)
    if (sequence !== loadSequence) return
    product.value = data; form.value = spuForm(data); selectedSkus.value = []
  } finally { if (sequence === loadSequence) busy.value = false }
}
async function saveBasic(): Promise<void> {
  if (!product.value) return
  let payload
  try {
    const edited = spuPayload(form.value)
    const before = spuPayload(spuForm(product.value))
    payload = { ...Object.fromEntries(Object.entries(edited).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(before[key as keyof typeof before]))), version: product.value.version }
    if (Object.keys(payload).length === 1) { ElMessage.info('没有需要保存的修改'); return }
  } catch (error) { ElMessage.warning(error instanceof Error ? error.message : '请检查输入'); return }
  saving.value = true
  try { product.value = await catalogApi.updateProduct(tid(), productId.value, payload); form.value = spuForm(product.value); ElMessage.success('基本信息已保存'); await loadLogs(1) } finally { saving.value = false }
}
const skuVisible = ref(false)
const editingSku = ref<ProductSku | null>(null)
function editSku(sku?: ProductSku): void { editingSku.value = sku ?? null; skuVisible.value = true }
async function saveSku(payload: ProductSkuPayload): Promise<void> {
  if (!product.value) return
  saving.value = true
  try { product.value = await catalogApi.updateProduct(tid(), productId.value, { version: product.value.version, skus: [payload] }); skuVisible.value = false; selectedSkus.value = []; ElMessage.success('SKU 已保存'); await loadLogs(1) } finally { saving.value = false }
}
async function archiveSku(sku: ProductSku): Promise<void> {
  const restoring = sku.status === 'archived'
  try { await ElMessageBox.confirm(`确认${restoring ? '恢复' : '归档'} SKU ${sku.sku_code}？`, 'SKU 归档', { type: 'warning' }) } catch { return }
  if (!product.value) return
  saving.value = true
  try { product.value = await catalogApi.updateProduct(tid(), productId.value, { version: product.value.version, skus: [{ id: sku.id, sku_code: sku.sku_code, version: sku.version, status: restoring ? 'active' : 'archived' }] }); selectedSkus.value = []; await loadLogs(1) } finally { saving.value = false }
}
async function restoreProduct(): Promise<void> {
  if (!product.value) return
  product.value = await catalogApi.updateProduct(tid(), productId.value, { version: product.value.version, status: 'active' }); form.value = spuForm(product.value); await loadLogs(1)
}
const selectedSkus = ref<ProductSku[]>([])
const batchVisible = ref(false)
const batchField = ref('sales_status')
const batchValue = ref('on_sale')
const clearBatch = ref(false)
const batchPrepared = ref<ProductSkuPayload[]>([])
const batchFields = computed(() => [
  { value: 'sales_status', label: '状态' }, { value: 'weight_g', label: '重量 g' },
  ...(canCost.value ? [{ value: 'cost_amount', label: '当前成本' }, { value: 'guidance_cost', label: '指导成本' }, { value: 'average_cost', label: '平均成本' }] : []),
])
const batchLabel = computed(() => batchFields.value.find((f) => f.value === batchField.value)?.label ?? '')
watch([batchField, batchValue, clearBatch], () => { batchPrepared.value = [] })
watch(batchField, (field) => { batchValue.value = field === 'sales_status' ? 'on_sale' : ''; clearBatch.value = false })
function openBatch(): void { batchPrepared.value = []; batchVisible.value = true }
function previewBatch(): void {
  try {
    if (!selectedSkus.value.length) throw new Error('请选择 SKU')
    if (!clearBatch.value && batchField.value !== 'sales_status' && !batchValue.value.trim()) throw new Error('请输入数值或勾选清空字段')
    const value = batchField.value === 'sales_status' ? batchValue.value : clearBatch.value ? null : decimalValue(batchValue.value, batchLabel.value)
    batchPrepared.value = selectedSkus.value.map((sku) => ({ id: sku.id, sku_code: sku.sku_code, version: sku.version, [batchField.value]: value }))
  } catch (error) { ElMessage.warning(error instanceof Error ? error.message : '请检查输入') }
}
function batchOld(skuId?: string): string {
  const sku = selectedSkus.value.find((s) => s.id === skuId)
  if (!sku) return '—'
  return batchField.value === 'sales_status' ? statusLabel(sku.sales_status) : displayValue(sku[batchField.value as keyof ProductSku])
}
function batchNew(payload: ProductSkuPayload): string {
  const value = payload[batchField.value as keyof ProductSkuPayload]
  return batchField.value === 'sales_status' ? statusLabel(String(value)) : displayValue(value)
}
async function commitBatch(): Promise<void> {
  if (!product.value || !batchPrepared.value.length) return
  saving.value = true
  try { product.value = await catalogApi.updateProduct(tid(), productId.value, { version: product.value.version, skus: batchPrepared.value }); batchVisible.value = false; selectedSkus.value = []; ElMessage.success('批量修改已保存'); await loadLogs(1) } finally { saving.value = false }
}
const logs = ref<ProductLog[]>([])
const logTotal = ref(0)
const logPage = ref(1)
const logsBusy = ref(false)
async function loadLogs(page = logPage.value): Promise<void> {
  logsBusy.value = true
  try { const result = await catalogApi.logs(tid(), productId.value, page); logs.value = result.items; logTotal.value = result.total; logPage.value = result.page } finally { logsBusy.value = false }
}
function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未设置'
  if (value && typeof value === 'object' && !Array.isArray(value) && 'name' in value) return String(value.name)
  if (typeof value === 'object') return Array.isArray(value) ? value.map((v) => typeof v === 'object' ? JSON.stringify(v) : String(v)).join('、') : JSON.stringify(value)
  if (typeof value === 'boolean') return value ? '是' : '否'
  return String(value)
}
function imageUrls(value: unknown): string[] {
  return (Array.isArray(value) ? value : [value]).flatMap((item) => {
    const url = typeof item === 'string' ? item : item && typeof item === 'object' && 'url' in item ? String(item.url) : ''
    return url && (url.startsWith('/') || /^https?:\/\//i.test(url)) ? [url] : []
  })
}
function isImage(change: ProductChange): boolean { return change.field.includes('image') || change.field === 'images' }
watch(activeTab, (tab) => { if (tab === 'logs') void loadLogs(1) })
watch(productId, () => { product.value = undefined; activeTab.value = 'basic'; logs.value = []; void load(); void loadOptions() }, { immediate: true })
</script>
<template>
  <div v-loading="busy">
    <el-card shadow="never" class="page-card">
      <div class="detail-heading"><div><el-button text @click="router.push('/products')">← 商品列表</el-button><strong>{{ product?.code }} {{ product?.name }}</strong><el-tag v-if="product?.status === 'archived'" type="info">已归档</el-tag></div><el-button @click="load">刷新</el-button></div>
      <template v-if="product">
        <div class="metadata"><span>{{ typeLabel(product.sales_type) }}</span><span>状态：{{ statusLabel(product.sales_status) }}</span><span>创建：{{ catalogDateTime(product.created_at) }}</span><span>更新：{{ catalogDateTime(product.updated_at) }}</span><el-button v-if="canUpdate && product.status === 'archived'" text type="primary" @click="restoreProduct">恢复商品</el-button></div>
        <el-tabs v-model="activeTab">
          <el-tab-pane label="基本信息" name="basic">
            <div class="summary-row"><span v-if="canCost">指导成本：{{ summaryText(product.cost_summary?.guidance) }}</span><span v-if="canCost">平均成本：{{ summaryText(product.cost_summary?.average) }}</span><span v-if="canCost">当前成本：{{ summaryText(product.cost_summary?.current) }} {{ product.cost_currency ?? 'CNY' }}</span><span>重量：{{ summaryText(product.weight_summary, true) }}</span></div>
            <SpuBasicFields :model="form" :options="options" :disabled="!canUpdate" />
            <div v-if="canUpdate" class="save-row"><el-button @click="form = spuForm(product)">还原输入</el-button><el-button type="primary" :loading="saving" @click="saveBasic">保存基本信息</el-button></div>
          </el-tab-pane>
          <el-tab-pane label="销售信息" name="skus">
            <div class="sku-toolbar"><span>共 {{ product.skus?.length ?? 0 }} 个 SKU</span><div v-if="canUpdate"><el-button :disabled="!selectedSkus.length" @click="openBatch">批量修改（{{ selectedSkus.length }}）</el-button><el-button type="primary" @click="editSku()">新增 SKU</el-button></div></div>
            <el-table :data="product.skus ?? []" border row-key="id" @selection-change="(selection: ProductSku[]) => selectedSkus = selection">
              <el-table-column v-if="canUpdate" type="selection" width="46" :selectable="(row: ProductSku) => row.status !== 'archived'" />
              <el-table-column label="图片" width="80"><template #default="{ row }"><el-image v-if="row.image_url || product.primary_image" :src="row.image_url || product.primary_image" fit="cover" style="width:48px;height:48px" /></template></el-table-column>
              <el-table-column prop="sku_code" label="SKU" min-width="160" /><el-table-column label="别名" min-width="200"><template #default="{ row }"><el-tag v-for="alias in row.aliases?.filter((a: { is_active: boolean }) => a.is_active)" :key="alias.id" size="small" class="alias-tag">{{ alias.identifier }}</el-tag><span v-if="!row.aliases?.some((a: { is_active: boolean }) => a.is_active)">—</span></template></el-table-column>
              <el-table-column label="规格" min-width="160"><template #default="{ row }">{{ Object.entries(row.specs || {}).map(([k, v]) => `${k}：${v}`).join(' / ') || '—' }}</template></el-table-column>
              <el-table-column v-if="canCost" label="当前成本" width="110"><template #default="{ row }">{{ row.cost_amount == null ? '未设置' : formatDecimal(row.cost_amount) }}</template></el-table-column>
              <el-table-column v-if="canCost" label="指导成本" width="110"><template #default="{ row }">{{ row.guidance_cost == null ? '未设置' : formatDecimal(row.guidance_cost) }}</template></el-table-column>
              <el-table-column v-if="canCost" label="平均成本" width="110"><template #default="{ row }">{{ row.average_cost == null ? '未设置' : formatDecimal(row.average_cost) }}</template></el-table-column>
              <el-table-column label="重量 g" width="110"><template #default="{ row }">{{ row.weight_g == null ? '未设置' : formatDecimal(row.weight_g) }}</template></el-table-column>
              <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.status === 'archived' ? 'info' : row.sales_status === 'discontinued' ? 'danger' : 'success'">{{ row.status === 'archived' ? '已归档' : statusLabel(row.sales_status) }}</el-tag></template></el-table-column>
              <el-table-column v-if="canUpdate" label="操作" width="130" fixed="right"><template #default="{ row }"><el-button text type="primary" @click="editSku(row as ProductSku)">编辑</el-button><el-button text :type="row.status === 'archived' ? 'primary' : 'danger'" @click="archiveSku(row as ProductSku)">{{ row.status === 'archived' ? '恢复' : '归档' }}</el-button></template></el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane label="操作日志" name="logs">
            <el-table v-loading="logsBusy" :data="logs" border row-key="id">
              <el-table-column type="expand"><template #default="{ row }"><div class="log-changes"><div v-for="(change, index) in row.changes" :key="index" class="log-change"><strong>{{ change.sku_code ? `SKU ${change.sku_code} · ` : '' }}{{ change.label || change.field }}</strong><div v-if="isImage(change)" class="image-change"><div><el-image v-for="url in imageUrls(change.old)" :key="url" :src="url" fit="contain" class="log-image" :preview-src-list="imageUrls(change.old)" preview-teleported /><span v-if="!imageUrls(change.old).length">未设置</span></div><span>→</span><div><el-image v-for="url in imageUrls(change.new)" :key="url" :src="url" fit="contain" class="log-image" :preview-src-list="imageUrls(change.new)" preview-teleported /><span v-if="!imageUrls(change.new).length">未设置</span></div></div><div v-else class="text-change"><span>{{ displayValue(change.old) }}</span><span>→</span><span>{{ displayValue(change.new) }}</span></div></div><span v-if="!row.changes?.length">{{ row.summary }}</span></div></template></el-table-column>
              <el-table-column label="日志内容" min-width="320"><template #default="{ row }"><div v-for="(change, index) in (row.changes || []).slice(0, 3)" :key="index" class="log-summary">{{ change.sku_code ? `SKU ${change.sku_code}：` : 'SPU：' }}{{ change.label }}{{ isImage(change) ? '已修改' : `由“${displayValue(change.old)}”修改为“${displayValue(change.new)}”` }}</div><span v-if="!row.changes?.length">{{ row.summary }}</span><span v-else-if="row.changes.length > 3" class="text-muted">另有 {{ row.changes.length - 3 }} 项，展开查看</span></template></el-table-column><el-table-column label="操作日期" width="180"><template #default="{ row }">{{ catalogDateTime(row.created_at) }}</template></el-table-column><el-table-column prop="actor_name" label="操作人" width="150" />
            </el-table>
            <div class="pagination-bar"><el-pagination :total="logTotal" :current-page="logPage" :page-size="20" layout="total, prev, pager, next, jumper" background @current-change="loadLogs" /></div>
          </el-tab-pane>
        </el-tabs>
      </template>
    </el-card>
  </div>
  <SkuEditor v-model="skuVisible" :sku="editingSku" :can-read-cost="canCost" :busy="saving" @save="saveSku" />
  <el-dialog v-model="batchVisible" title="批量修改 SKU" width="min(720px, 95vw)" :close-on-click-modal="false">
    <el-form label-width="90px"><el-form-item label="修改字段"><el-select v-model="batchField"><el-option v-for="field in batchFields" :key="field.value" :label="field.label" :value="field.value" /></el-select></el-form-item><el-form-item :label="batchLabel"><el-select v-if="batchField === 'sales_status'" v-model="batchValue"><el-option v-for="status in salesStatuses" :key="status.value" :label="status.label" :value="status.value" /></el-select><template v-else><el-input v-model="batchValue" inputmode="decimal" :disabled="clearBatch" style="width:180px" /><el-checkbox v-model="clearBatch" style="margin-left:12px">清空字段</el-checkbox></template></el-form-item></el-form>
    <el-table v-if="batchPrepared.length" :data="batchPrepared" border><el-table-column prop="sku_code" label="SKU" /><el-table-column label="修改前"><template #default="{ row }">{{ batchOld(row.id) }}</template></el-table-column><el-table-column label="修改后"><template #default="{ row }">{{ batchNew(row as ProductSkuPayload) }}</template></el-table-column></el-table>
    <template #footer><el-button @click="batchVisible = false">取消</el-button><el-button @click="previewBatch">预览修改</el-button><el-button type="primary" :loading="saving" :disabled="!batchPrepared.length" @click="commitBatch">确认保存 {{ batchPrepared.length }} 个 SKU</el-button></template>
  </el-dialog>
</template>
<style scoped>
.detail-heading,.detail-heading > div,.sku-toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; }.detail-heading > div { justify-content:flex-start; }
.metadata,.summary-row { display:flex; gap:24px; align-items:center; flex-wrap:wrap; margin:16px 0; color:var(--el-text-color-secondary); font-size:13px; }
.summary-row { background:var(--el-fill-color-light); padding:14px; border-radius:5px; }.save-row { display:flex; justify-content:flex-end; gap:10px; padding-top:12px; }
.log-summary { white-space:normal; overflow-wrap:anywhere; margin:3px 0; }.sku-toolbar { margin:12px 0; }.alias-tag { margin:2px; }.log-changes { padding:8px 24px; }.log-change { padding:12px 0; border-bottom:1px solid var(--el-border-color-lighter); }
.text-change,.image-change { display:flex; align-items:center; gap:16px; margin-top:8px; overflow-wrap:anywhere; }.text-change > span:first-child { color:var(--el-text-color-secondary); }.log-image { width:72px; height:64px; margin-right:8px; }
</style>
