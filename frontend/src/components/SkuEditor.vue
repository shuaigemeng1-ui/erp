<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import CatalogImageInput from './CatalogImageInput.vue'
import { decimalValue, salesStatuses } from '@/utils/catalog'
import type { ProductSku, ProductSkuPayload, SalesStatus } from '@/types/catalog'
const props = defineProps<{ modelValue: boolean; sku?: ProductSku | ProductSkuPayload | null; canReadCost: boolean; busy?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; save: [value: ProductSkuPayload] }>()
const visible = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })
const form = reactive({ sku_code: '', name: '', images: [] as string[], aliases: [] as string[], specs: [] as { key: string; value: string }[], cost_amount: '', guidance_cost: '', average_cost: '', weight_g: '', sales_status: 'on_sale' as SalesStatus })
const historicalAliases = computed(() => (props.sku?.aliases ?? []).filter((a): a is { id: string; identifier: string; is_active: boolean } => typeof a !== 'string' && !a.is_active))
watch(() => props.modelValue, (open) => {
  if (!open) return
  const sku = props.sku
  form.sku_code = sku?.sku_code ?? ''
  form.name = sku?.name ?? ''
  form.images = sku?.image_url ? [sku.image_url] : []
  form.aliases = (sku?.aliases ?? []).filter((a) => typeof a === 'string' || a.is_active).map((a) => typeof a === 'string' ? a : a.identifier)
  form.specs = Object.entries(sku?.specs ?? {}).map(([key, value]) => ({ key, value: String(value) }))
  form.cost_amount = sku?.cost_amount == null ? '' : String(sku.cost_amount)
  form.guidance_cost = sku?.guidance_cost == null ? '' : String(sku.guidance_cost)
  form.average_cost = sku?.average_cost == null ? '' : String(sku.average_cost)
  form.weight_g = sku?.weight_g == null ? '' : String(sku.weight_g)
  form.sales_status = sku?.sales_status ?? 'on_sale'
}, { immediate: true })
function save(): void {
  try {
    if (!form.sku_code.trim()) throw new Error('请填写 SKU 编号')
    const aliases = form.aliases.map((a) => a.trim()).filter(Boolean)
    if (aliases.length > 100 || aliases.some((alias) => alias.length > 128)) throw new Error('别名最多 100 个，每个不超过 128 个字符')
    if (new Set(aliases.map((a) => a.toLocaleUpperCase())).size !== aliases.length) throw new Error('别名不能重复')
    if (form.specs.length > 30) throw new Error('规格最多 30 项')
    const specs: Record<string, string> = {}
    for (const spec of form.specs) {
      const key = spec.key.trim()
      const value = spec.value.trim()
      if (!key || !value) throw new Error('规格名称和值均不能为空')
      if (key.length > 100 || value.length > 200) throw new Error('规格名称不能超过 100 字，规格值不能超过 200 字')
      if (key in specs) throw new Error('规格名称不能重复')
      specs[key] = value
    }
    const payload: ProductSkuPayload = {
      ...(props.sku?.id ? { id: props.sku.id } : {}),
      ...(props.sku?.version != null ? { version: props.sku.version } : {}),
      sku_code: form.sku_code.trim(), name: form.name || Object.values(specs).join(' / ') || form.sku_code.trim(),
      price: props.sku?.price ?? '0', aliases, specs,
      image_url: form.images[0] ?? null, weight_g: decimalValue(form.weight_g, '重量'), sales_status: form.sales_status,
      ...(props.canReadCost ? { cost_amount: decimalValue(form.cost_amount, '当前成本'), guidance_cost: decimalValue(form.guidance_cost, '指导成本'), average_cost: decimalValue(form.average_cost, '平均成本') } : {}),
    }
    emit('save', payload)
  } catch (error) { ElMessage.warning(error instanceof Error ? error.message : '请检查输入') }
}
</script>
<template>
  <el-dialog v-model="visible" :title="sku?.id ? '编辑 SKU' : '新增 SKU'" width="min(760px, 95vw)" append-to-body :close-on-click-modal="false">
    <el-form label-width="90px" :disabled="busy">
      <el-form-item label="图片"><CatalogImageInput v-model="form.images" /></el-form-item>
      <el-row :gutter="16">
        <el-col :span="14"><el-form-item label="SKU 编号" required><el-input v-model="form.sku_code" maxlength="64" /></el-form-item></el-col>
        <el-col :span="10"><el-form-item label="状态"><el-select v-model="form.sales_status"><el-option v-for="item in salesStatuses" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item></el-col>
      </el-row>
      <el-form-item label="别名"><el-select v-model="form.aliases" multiple filterable allow-create default-first-option placeholder="输入别名后回车，可添加多个" style="width:100%"><el-option v-for="alias in form.aliases" :key="alias" :label="alias" :value="alias" /></el-select></el-form-item>
      <el-form-item v-if="historicalAliases.length" label="停用别名"><el-button v-for="alias in historicalAliases" :key="alias.id" text size="small" :disabled="form.aliases.includes(alias.identifier)" @click="form.aliases.push(alias.identifier)">{{ alias.identifier }} · 恢复</el-button></el-form-item>
      <el-form-item label="规格">
        <div class="specs">
          <div v-for="(spec, index) in form.specs" :key="index" class="spec-row"><el-input v-model="spec.key" placeholder="规格名称，如颜色" /><el-input v-model="spec.value" placeholder="规格值，如黑色" /><el-button text type="danger" @click="form.specs.splice(index, 1)">移除</el-button></div>
          <el-button size="small" @click="form.specs.push({ key: '', value: '' })">添加规格</el-button>
        </div>
      </el-form-item>
      <el-row v-if="canReadCost" :gutter="16">
        <el-col :xs="24" :sm="8"><el-form-item label="当前成本"><el-input v-model="form.cost_amount" inputmode="decimal" clearable /></el-form-item></el-col>
        <el-col :xs="24" :sm="8"><el-form-item label="指导成本"><el-input v-model="form.guidance_cost" inputmode="decimal" clearable /></el-form-item></el-col>
        <el-col :xs="24" :sm="8"><el-form-item label="平均成本"><el-input v-model="form.average_cost" inputmode="decimal" clearable /></el-form-item></el-col>
      </el-row>
      <el-form-item label="重量 g"><el-input v-model="form.weight_g" inputmode="decimal" clearable style="width:180px" /></el-form-item>
    </el-form>
    <template #footer><el-button @click="visible = false">取消</el-button><el-button type="primary" :loading="busy" @click="save">保存</el-button></template>
  </el-dialog>
</template>
<style scoped>
.specs { width:100%; }.spec-row { display:flex; gap:8px; margin-bottom:8px; }
</style>
