<script setup lang="ts">
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import CatalogImageInput from './CatalogImageInput.vue'
import { catalogApi } from '@/api/catalog'
import { useTenantStore } from '@/stores/tenant'
import { salesTypes, type SpuForm } from '@/utils/catalog'
import type { CatalogOptions } from '@/types/catalog'
const props = defineProps<{ model: SpuForm; options: CatalogOptions; disabled?: boolean }>()
const emit = defineEmits<{ refreshOptions: [] }>()
const tenant = useTenantStore()
const referenceVisible = ref(false)
const referenceBusy = ref(false)
const reference = reactive({ kind: 'supplier', code: '', name: '', parent_id: '' })
const referenceKinds = [{ value: 'supplier', label: '供应商' }, { value: 'brand', label: '品牌' }, { value: 'category', label: '分类' }, { value: 'tag', label: '特性标签' }, { value: 'packaging', label: '包材' }]
const personnel = [{ field: 'purchaser_id', label: '采购员' }, { field: 'developer_id', label: '业务开发员' }, { field: 'inquiry_buyer_id', label: '采购询价员' }] as const
async function addReference(): Promise<void> {
  if (!reference.name.trim() || (reference.kind === 'supplier' && !reference.code.trim())) { ElMessage.warning('请填写名称和供应商编号'); return }
  referenceBusy.value = true
  try {
    if (reference.kind === 'supplier') {
      const supplier = await catalogApi.createSupplier(tenant.requireTenantId(), { code: reference.code.trim(), name: reference.name.trim() })
      props.model.preferred_supplier_id = supplier.id
    } else {
      const option = await catalogApi.createOption(tenant.requireTenantId(), { kind: reference.kind, name: reference.name.trim(), parent_id: reference.kind === 'category' ? reference.parent_id || undefined : undefined })
      if (reference.kind === 'brand') props.model.brand = option.name
      if (reference.kind === 'category') props.model.category = option.name
      if (reference.kind === 'packaging') props.model.packaging_name = option.name
      if (reference.kind === 'tag') props.model.feature_tags = [...new Set([...props.model.feature_tags, option.name])]
    }
    emit('refreshOptions'); referenceVisible.value = false; reference.name = ''; reference.code = ''; reference.parent_id = ''
    ElMessage.success('基础资料已新增')
  } finally { referenceBusy.value = false }
}
</script>
<template>
  <el-form :disabled="disabled" label-width="110px" class="spu-basic-form">
    <div class="section-title"><span>商品标识</span><el-button v-if="!disabled && tenant.has('catalog.product.update')" text type="primary" @click="referenceVisible = true">新增基础资料</el-button></div>
    <el-row :gutter="20">
      <el-col :xs="24" :md="12"><el-form-item label="SPU 编号" required><el-input v-model="model.code" maxlength="64" /></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="销售类型"><el-select v-model="model.sales_type"><el-option v-for="item in salesTypes" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="商品中文名称" required><el-input v-model="model.name" maxlength="200" /></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="商品英文名称"><el-input v-model="model.name_en" maxlength="200" /></el-form-item></el-col>
      <el-col :span="24"><el-form-item label="商品图片"><CatalogImageInput v-model="model.images" multiple :disabled="disabled" /></el-form-item></el-col>
    </el-row>
    <div class="section-title">采购与人员</div>
    <el-row :gutter="20">
      <el-col :xs="24" :md="12"><el-form-item label="首选供应商"><el-select v-model="model.preferred_supplier_id" filterable clearable><el-option v-for="item in options.suppliers" :key="item.id" :label="`${item.name}（${item.code}）`" :value="item.id" :disabled="item.status !== 'active'" /></el-select></el-form-item></el-col>
      <el-col v-for="item in personnel" :key="item.field" :xs="24" :md="12"><el-form-item :label="item.label"><el-select v-model="model[item.field]" filterable clearable><el-option v-for="person in options.members" :key="person.id" :label="person.name" :value="person.id" :disabled="person.status !== 'active'" /></el-select></el-form-item></el-col>
    </el-row>
    <div class="section-title">商品资料</div>
    <el-row :gutter="20">
      <el-col :xs="24" :md="12"><el-form-item label="海关编码"><el-input v-model="model.hs_code" maxlength="32" /></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="用途"><el-input v-model="model.usage" /></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="中文材质"><el-input v-model="model.material_zh" /></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="英文材质"><el-input v-model="model.material_en" /></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="品牌"><el-select v-model="model.brand" filterable clearable><el-option v-for="item in options.brand" :key="item.id" :label="item.name" :value="item.name" /></el-select></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="分类"><el-select v-model="model.category" filterable clearable><el-option v-for="item in options.category" :key="item.id" :label="item.name" :value="item.name" /></el-select></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="特性标签"><el-select v-model="model.feature_tags" filterable multiple clearable><el-option v-for="item in options.tag" :key="item.id" :label="item.name" :value="item.name" /></el-select></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="包材名称"><el-select v-model="model.packaging_name" filterable clearable><el-option v-for="item in options.packaging" :key="item.id" :label="item.name" :value="item.name" /></el-select></el-form-item></el-col>
    </el-row>
    <div class="section-title">尺寸与补充信息</div>
    <el-row :gutter="20">
      <el-col :xs="24" :md="12"><el-form-item label="商品尺寸 cm"><div class="dimensions"><el-input v-model="model.length_cm" placeholder="长" inputmode="decimal" /><el-input v-model="model.width_cm" placeholder="宽" inputmode="decimal" /><el-input v-model="model.height_cm" placeholder="高" inputmode="decimal" /></div></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="包装后尺寸 cm"><div class="dimensions"><el-input v-model="model.package_length_cm" placeholder="长" inputmode="decimal" /><el-input v-model="model.package_width_cm" placeholder="宽" inputmode="decimal" /><el-input v-model="model.package_height_cm" placeholder="高" inputmode="decimal" /></div></el-form-item></el-col>
      <el-col :span="24"><el-form-item label="产品特点"><el-input v-model="model.features" type="textarea" :rows="3" /></el-form-item></el-col>
      <el-col :span="24"><el-form-item label="备注"><el-input v-model="model.remark" type="textarea" :rows="2" /></el-form-item></el-col>
    </el-row>
  </el-form>
  <el-dialog v-model="referenceVisible" title="新增基础资料" width="440px" append-to-body>
    <el-form label-width="90px">
      <el-form-item label="类型"><el-select v-model="reference.kind"><el-option v-for="item in referenceKinds" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item>
      <el-form-item v-if="reference.kind === 'supplier'" label="供应商编号"><el-input v-model="reference.code" maxlength="64" /></el-form-item>
      <el-form-item label="名称"><el-input v-model="reference.name" maxlength="100" /></el-form-item>
      <el-form-item v-if="reference.kind === 'category'" label="上级分类"><el-select v-model="reference.parent_id" clearable><el-option v-for="item in options.category" :key="item.id" :label="item.name" :value="item.id" /></el-select></el-form-item>
    </el-form>
    <template #footer><el-button @click="referenceVisible = false">取消</el-button><el-button type="primary" :loading="referenceBusy" @click="addReference">新增</el-button></template>
  </el-dialog>
</template>
<style scoped>
.section-title { font-weight:600; margin:8px 0 18px; padding-bottom:10px; border-bottom:1px solid var(--el-border-color-lighter); display:flex; justify-content:space-between; align-items:center; }
.dimensions { display:flex; gap:8px; width:100%; }
.spu-basic-form :deep(.el-select) { width:100%; }
</style>
