<script setup lang="ts">
import { computed } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { scopeTypeLabels } from '@/utils/dict'
import type { Role, RoleBindingInput, ScopeBinding, ScopeType, StockPlace, Store } from '@/types'

const props = defineProps<{
  modelValue: RoleBindingInput[]
  roles: Role[]
  stores?: Store[]
  stockPlaces?: StockPlace[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: RoleBindingInput[]]
}>()

const bindings = computed(() => props.modelValue)
const storeOptions = computed(() => props.stores ?? [])
const placeOptions = computed(() => props.stockPlaces ?? [])

function addBinding(): void {
  emit('update:modelValue', [...props.modelValue, { role_id: '', scopes: [{ scope_type: 'all', scope_ids: [] }] }])
}

function removeBinding(index: number): void {
  emit(
    'update:modelValue',
    props.modelValue.filter((_, current) => current !== index),
  )
}

function addScope(bindingIndex: number): void {
  const binding = props.modelValue[bindingIndex]
  if (!binding) return
  binding.scopes.push({ scope_type: 'all', scope_ids: [] })
}

function removeScope(bindingIndex: number, scopeIndex: number): void {
  const binding = props.modelValue[bindingIndex]
  if (!binding) return
  binding.scopes.splice(scopeIndex, 1)
}

function onScopeTypeChange(scope: ScopeBinding): void {
  // 切换范围维度时清空旧的 ID，避免混用店铺/库存地点
  scope.scope_ids = []
}

function scopeOptions(type: ScopeType): Array<{ id: string; name: string }> {
  if (type === 'stores') {
    return storeOptions.value.map((store) => ({ id: store.id, name: store.name }))
  }
  if (type === 'stock_places') {
    return placeOptions.value.map((place) => ({ id: place.id, name: place.name }))
  }
  return []
}
</script>

<template>
  <div>
    <div v-for="(binding, bindingIndex) in bindings" :key="bindingIndex" class="scope-editor">
      <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap">
        <el-select v-model="binding.role_id" placeholder="选择角色" filterable style="width: 220px">
          <el-option v-for="role in roles" :key="role.id" :label="`${role.name}（${role.code}）`" :value="role.id" />
        </el-select>
        <el-button text type="primary" @click="addScope(bindingIndex)">添加数据范围</el-button>
        <el-button text type="danger" @click="removeBinding(bindingIndex)">移除角色</el-button>
      </div>
      <div
        v-for="(scope, scopeIndex) in binding.scopes"
        :key="scopeIndex"
        style="display: flex; gap: 8px; align-items: center; margin-bottom: 6px"
      >
        <el-select v-model="scope.scope_type" style="width: 180px" @change="onScopeTypeChange(scope)">
          <el-option v-for="(label, value) in scopeTypeLabels" :key="value" :label="label" :value="value" />
        </el-select>
        <el-select
          v-if="scope.scope_type !== 'all'"
          v-model="scope.scope_ids"
          multiple
          filterable
          collapse-tags
          :placeholder="scope.scope_type === 'stores' ? '选择店铺' : '选择库存地点'"
          style="flex: 1"
        >
          <el-option
            v-for="option in scopeOptions(scope.scope_type)"
            :key="option.id"
            :label="option.name"
            :value="option.id"
          />
        </el-select>
        <span v-else class="form-tip" style="flex: 1">覆盖当前租户全部数据</span>
        <el-button text type="danger" @click="removeScope(bindingIndex, scopeIndex)">删除</el-button>
      </div>
    </div>
    <el-button text type="primary" :icon="Plus" @click="addBinding">添加角色绑定</el-button>
    <div class="form-tip">空范围表示无授权；授权结果按绑定取并集，不同范围维度共同生效。</div>
  </div>
</template>
