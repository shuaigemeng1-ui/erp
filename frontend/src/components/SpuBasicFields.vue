<script setup lang="ts">
import CatalogImageInput from './CatalogImageInput.vue'
import { salesTypes, type SpuForm } from '@/utils/catalog'
import type { CatalogOptions } from '@/types/catalog'
defineProps<{ model: SpuForm; options: CatalogOptions; disabled?: boolean }>()
const personnel = [{ field: 'purchaser_id', label: '采购员' }, { field: 'developer_id', label: '业务开发员' }, { field: 'inquiry_buyer_id', label: '采购询价员' }] as const
</script>
<template>
  <el-form :disabled="disabled" label-width="110px" class="spu-basic-form">
    <div class="section-title">商品标识</div>
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
      <el-col :xs="24" :md="12"><el-form-item label="品牌"><el-select v-model="model.brand" filterable clearable><el-option v-for="item in options.brand" :key="item.id" :label="item.name" :value="item.name" :disabled="item.status !== 'active'" /></el-select></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="分类"><el-select v-model="model.category" filterable clearable><el-option v-for="item in options.category" :key="item.id" :label="item.name" :value="item.name" :disabled="item.status !== 'active'" /></el-select></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="特性标签"><el-select v-model="model.feature_tags" filterable multiple clearable><el-option v-for="item in options.tag" :key="item.id" :label="item.name" :value="item.name" :disabled="item.status !== 'active'" /></el-select></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="包材名称"><el-select v-model="model.packaging_name" filterable clearable><el-option v-for="item in options.packaging" :key="item.id" :label="item.name" :value="item.name" :disabled="item.status !== 'active'" /></el-select></el-form-item></el-col>
    </el-row>
    <div class="section-title">尺寸与补充信息</div>
    <el-row :gutter="20">
      <el-col :xs="24" :md="12"><el-form-item label="商品尺寸 cm"><div class="dimensions"><el-input v-model="model.length_cm" placeholder="长" inputmode="decimal" /><el-input v-model="model.width_cm" placeholder="宽" inputmode="decimal" /><el-input v-model="model.height_cm" placeholder="高" inputmode="decimal" /></div></el-form-item></el-col>
      <el-col :xs="24" :md="12"><el-form-item label="包装后尺寸 cm"><div class="dimensions"><el-input v-model="model.package_length_cm" placeholder="长" inputmode="decimal" /><el-input v-model="model.package_width_cm" placeholder="宽" inputmode="decimal" /><el-input v-model="model.package_height_cm" placeholder="高" inputmode="decimal" /></div></el-form-item></el-col>
      <el-col :span="24"><el-form-item label="产品特点"><el-input v-model="model.features" type="textarea" :rows="3" /></el-form-item></el-col>
      <el-col :span="24"><el-form-item label="备注"><el-input v-model="model.remark" type="textarea" :rows="2" /></el-form-item></el-col>
    </el-row>
  </el-form>

</template>
<style scoped>
.section-title { font-weight:600; margin:8px 0 18px; padding-bottom:10px; border-bottom:1px solid var(--el-border-color-lighter); display:flex; justify-content:space-between; align-items:center; }
.dimensions { display:flex; gap:8px; width:100%; }
.spu-basic-form :deep(.el-select) { width:100%; }
</style>
