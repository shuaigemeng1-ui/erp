<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { catalogApi } from '@/api/catalog'
import { isAbortError } from '@/api/http'
import { useTenantStore } from '@/stores/tenant'
import type { CatalogOption, CatalogOptionKind, CatalogOptions, SupplierOption } from '@/types/catalog'

type ResourceKind = 'supplier' | CatalogOptionKind
type ResourceRow = CatalogOption & Partial<Pick<SupplierOption, 'code' | 'contact_name' | 'contact_details' | 'remark'>>
const tenant = useTenantStore()
const sections: { value: ResourceKind; label: string }[] = [
  { value: 'supplier', label: '供应商' }, { value: 'brand', label: '品牌' },
  { value: 'category', label: '分类' }, { value: 'tag', label: '特性标签' }, { value: 'packaging', label: '包材' },
]
const activeKind = ref<ResourceKind>('supplier')
const options = ref<CatalogOptions>({ suppliers: [], members: [], brand: [], category: [], tag: [], packaging: [] })
const loading = ref(false)
const loadFailed = ref(false)
const query = ref('')
const page = ref(1)
const pageSize = 20
const canManage = computed(() => options.value.can_manage_options === true)
const activeLabel = computed(() => sections.find((item) => item.value === activeKind.value)?.label ?? '')
const rows = computed<ResourceRow[]>(() => activeKind.value === 'supplier' ? options.value.suppliers : options.value[activeKind.value])
const filteredRows = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase()
  return rows.value.filter((item) => !keyword || [item.name, item.code, item.contact_name, item.contact_details].some((value) => value?.toLocaleLowerCase().includes(keyword)))
})
const pagedRows = computed(() => filteredRows.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const categoryNames = computed(() => new Map(options.value.category.map((item) => [item.id, item.name])))
watch([activeKind, query], () => { page.value = 1 })
watch(() => filteredRows.value.length, (count) => { page.value = Math.min(page.value, Math.max(1, Math.ceil(count / pageSize))) })

let disposed = false
let loadVersion = 0
onBeforeUnmount(() => { disposed = true })
function current(tenantId: string, epoch: number): boolean {
  return !disposed && tenant.activeTenantId === tenantId && tenant.epoch === epoch
}
async function load(): Promise<void> {
  const tenantId = tenant.requireTenantId()
  const epoch = tenant.epoch
  const version = ++loadVersion
  loading.value = true
  try {
    const data = await catalogApi.options(tenantId)
    if (!current(tenantId, epoch) || version !== loadVersion) return
    options.value = data
    loadFailed.value = false
  } catch (cause) {
    if (current(tenantId, epoch) && version === loadVersion && !isAbortError(cause)) loadFailed.value = true
  } finally {
    if (current(tenantId, epoch) && version === loadVersion) loading.value = false
  }
}
onMounted(load)

const dialogVisible = ref(false)
const saving = ref(false)
const togglingId = ref('')
const editingId = ref('')
const formRef = ref<FormInstance>()
const form = reactive({ kind: 'supplier' as ResourceKind, code: '', name: '', contact_name: '', contact_details: '', remark: '', parent_id: '' })
const dialogLabel = computed(() => sections.find((item) => item.value === form.kind)?.label ?? '')
const rules: FormRules = {
  code: [{ required: true, whitespace: true, message: '请输入供应商编号', trigger: 'blur' }],
  name: [{ required: true, whitespace: true, message: '请输入名称', trigger: 'blur' }],
}
const parentCandidates = computed(() => {
  const excluded = new Set(editingId.value ? [editingId.value] : [])
  let changed = true
  while (changed) {
    changed = false
    for (const item of options.value.category) {
      if (item.parent_id && excluded.has(item.parent_id) && !excluded.has(item.id)) { excluded.add(item.id); changed = true }
    }
  }
  return options.value.category.filter((item) => !excluded.has(item.id))
})
function openEditor(row?: ResourceRow): void {
  editingId.value = row?.id ?? ''
  Object.assign(form, {
    kind: activeKind.value, code: row?.code ?? '', name: row?.name ?? '',
    contact_name: row?.contact_name ?? '', contact_details: row?.contact_details ?? '', remark: row?.remark ?? '', parent_id: row?.parent_id ?? '',
  })
  dialogVisible.value = true
}
async function save(): Promise<void> {
  if (!canManage.value || saving.value) return
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid || saving.value || disposed) return
  const tenantId = tenant.requireTenantId()
  const epoch = tenant.epoch
  saving.value = true
  try {
    if (form.kind === 'supplier') {
      const payload = { code: form.code.trim(), name: form.name.trim(), contact_name: form.contact_name.trim() || null, contact_details: form.contact_details.trim() || null, remark: form.remark.trim() || null }
      if (editingId.value) await catalogApi.updateSupplier(tenantId, editingId.value, payload)
      else await catalogApi.createSupplier(tenantId, payload)
    } else if (editingId.value) {
      await catalogApi.updateOption(tenantId, editingId.value, { name: form.name.trim(), ...(form.kind === 'category' ? { parent_id: form.parent_id || null } : {}) })
    } else {
      await catalogApi.createOption(tenantId, { kind: form.kind, name: form.name.trim(), ...(form.kind === 'category' ? { parent_id: form.parent_id || undefined } : {}) })
    }
    if (!current(tenantId, epoch)) return
    ElMessage.success('保存成功')
    dialogVisible.value = false
    await load()
  } catch {
    // 接口统一显示具体错误，保留表单以便修改。
  } finally {
    if (current(tenantId, epoch)) saving.value = false
  }
}
async function toggleStatus(row: ResourceRow): Promise<void> {
  if (!canManage.value || togglingId.value) return
  const tenantId = tenant.requireTenantId()
  const epoch = tenant.epoch
  const kind = activeKind.value
  const status = row.status === 'active' ? 'disabled' : 'active'
  const action = status === 'active' ? '启用' : '停用'
  togglingId.value = row.id
  try {
    await ElMessageBox.confirm(`确认${action}「${row.name}」？`, `${action}${activeLabel.value}`, { type: 'warning', confirmButtonText: action, cancelButtonText: '取消' })
    if (!current(tenantId, epoch)) return
    if (kind === 'supplier') await catalogApi.updateSupplier(tenantId, row.id, { status })
    else await catalogApi.updateOption(tenantId, row.id, { status })
    if (!current(tenantId, epoch)) return
    ElMessage.success(`已${action}`)
    await load()
  } catch {
    // 取消确认无需提示；接口错误由统一拦截器显示。
  } finally {
    if (current(tenantId, epoch)) togglingId.value = ''
  }
}
</script>

<template>
  <el-card shadow="never">
    <el-tabs v-model="activeKind">
      <el-tab-pane v-for="section in sections" :key="section.value" :label="section.label" :name="section.value" />
    </el-tabs>
    <div class="settings-toolbar">
      <el-input v-model="query" :prefix-icon="Search" clearable :placeholder="activeKind === 'supplier' ? '搜索编号、名称、联系人或联系方式' : '搜索名称'" class="settings-search" />
      <div class="settings-actions">
        <el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button>
        <el-button v-if="canManage" type="primary" :icon="Plus" :disabled="loading || !!togglingId" @click="openEditor()">新增{{ activeLabel }}</el-button>
      </div>
    </div>
    <el-alert v-if="loadFailed" title="资料加载失败，请刷新重试" type="error" :closable="false" class="load-error" />
    <el-table v-loading="loading" :data="pagedRows" row-key="id" empty-text="暂无资料">
      <el-table-column v-if="activeKind === 'supplier'" prop="code" label="供应商编号" min-width="150" show-overflow-tooltip />
      <el-table-column prop="name" :label="`${activeLabel}名称`" min-width="180" show-overflow-tooltip />
      <template v-if="activeKind === 'supplier'">
        <el-table-column prop="contact_name" label="联系人" min-width="120"><template #default="{ row }">{{ row.contact_name || '—' }}</template></el-table-column>
        <el-table-column prop="contact_details" label="联系方式" min-width="180" show-overflow-tooltip><template #default="{ row }">{{ row.contact_details || '—' }}</template></el-table-column>
        <el-table-column prop="remark" label="备注" min-width="180" show-overflow-tooltip><template #default="{ row }">{{ row.remark || '—' }}</template></el-table-column>
      </template>
      <el-table-column v-if="activeKind === 'category'" label="上级分类" min-width="160"><template #default="{ row }">{{ categoryNames.get(row.parent_id) || '—' }}</template></el-table-column>
      <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : 'info'">{{ row.status === 'active' ? '启用' : '停用' }}</el-tag></template></el-table-column>
      <el-table-column v-if="canManage" label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="!!togglingId || loading" @click="openEditor(row as ResourceRow)">编辑</el-button>
          <el-button link :type="row.status === 'active' ? 'danger' : 'success'" :loading="togglingId === row.id" :disabled="!!togglingId || loading" @click="toggleStatus(row as ResourceRow)">{{ row.status === 'active' ? '停用' : '启用' }}</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination v-if="filteredRows.length > pageSize" v-model:current-page="page" :page-size="pageSize" :total="filteredRows.length" layout="total, prev, pager, next" class="settings-pagination" />
  </el-card>

  <el-dialog v-model="dialogVisible" :title="`${editingId ? '编辑' : '新增'}${dialogLabel}`" width="520px" destroy-on-close :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="100px" :disabled="saving" @submit.prevent="save">
      <el-form-item v-if="form.kind === 'supplier'" label="供应商编号" prop="code"><el-input v-model="form.code" maxlength="64" /></el-form-item>
      <el-form-item :label="`${dialogLabel}名称`" prop="name"><el-input v-model="form.name" :maxlength="form.kind === 'supplier' ? 200 : 100" /></el-form-item>
      <template v-if="form.kind === 'supplier'">
        <el-form-item label="联系人"><el-input v-model="form.contact_name" maxlength="100" /></el-form-item>
        <el-form-item label="联系方式"><el-input v-model="form.contact_details" maxlength="255" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="form.remark" type="textarea" :rows="3" maxlength="2000" show-word-limit /></el-form-item>
      </template>
      <el-form-item v-if="form.kind === 'category'" label="上级分类"><el-select v-model="form.parent_id" filterable clearable placeholder="无上级分类" class="full-width"><el-option v-for="item in parentCandidates" :key="item.id" :label="item.name" :value="item.id" :disabled="item.status !== 'active'" /></el-select></el-form-item>
    </el-form>
    <template #footer><el-button :disabled="saving" @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存</el-button></template>
  </el-dialog>
</template>

<style scoped>
.settings-toolbar { display:flex; flex-wrap:wrap; justify-content:space-between; gap:12px; margin:4px 0 16px; }
.settings-search { width:360px; max-width:100%; }
.settings-actions { display:flex; }
.settings-pagination { justify-content:flex-end; margin-top:16px; }
.load-error { margin-bottom:12px; }
.full-width { width:100%; }
</style>
