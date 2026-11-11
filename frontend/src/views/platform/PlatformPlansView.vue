<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { platformApi } from '@/api/platform'
import IdCopy from '@/components/IdCopy.vue'
import StatusTag from '@/components/StatusTag.vue'
import { textOrDash } from '@/utils/format'
import type { Plan, PlanFeature } from '@/types'

const loading = ref(false)
const plans = ref<Plan[]>([])

const dialogVisible = ref(false)
const submitting = ref(false)
const editingId = ref('')
const formRef = ref<FormInstance>()
const form = reactive({
  code: '',
  name: '',
  status: 'active',
  description: '',
  features: [] as PlanFeature[],
})
const rules: FormRules = {
  code: [
    { required: true, message: '请输入套餐编码', trigger: 'blur' },
    { pattern: /^[a-z0-9][a-z0-9_-]*$/, message: '仅小写字母、数字、下划线和中划线', trigger: 'blur' },
  ],
  name: [{ required: true, message: '请输入套餐名称', trigger: 'blur' }],
}

async function load(): Promise<void> {
  loading.value = true
  try {
    plans.value = await platformApi.plans()
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  editingId.value = ''
  form.code = ''
  form.name = ''
  form.status = 'active'
  form.description = ''
  form.features = []
  dialogVisible.value = true
}

function openEdit(plan: Plan): void {
  editingId.value = plan.id
  form.code = plan.code
  form.name = plan.name
  form.status = plan.status
  form.description = plan.description ?? ''
  form.features = (plan.features ?? []).map((feature) => ({ ...feature }))
  dialogVisible.value = true
}

function addFeature(): void {
  form.features.push({ feature_code: '', limit_value: null })
}

function removeFeature(index: number): void {
  form.features.splice(index, 1)
}

async function submit(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  const features = form.features
    .filter((feature) => feature.feature_code.trim())
    .map((feature) => ({
      feature_code: feature.feature_code.trim(),
      limit_value: feature.limit_value === null || feature.limit_value === undefined ? null : Number(feature.limit_value),
    }))
  submitting.value = true
  try {
    if (editingId.value) {
      await platformApi.updatePlan(editingId.value, {
        name: form.name.trim(),
        status: form.status,
        description: form.description.trim(),
        features,
      })
      ElMessage.success('套餐已更新')
    } else {
      await platformApi.createPlan({
        code: form.code.trim(),
        name: form.name.trim(),
        description: form.description.trim(),
        features,
      })
      ElMessage.success('套餐已创建')
    }
    dialogVisible.value = false
    await load()
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="table-toolbar">
      <el-button :icon="Refresh" @click="load">刷新</el-button>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建套餐</el-button>
    </div>

    <el-table v-loading="loading" :data="plans" border size="small" row-key="id">
      <el-table-column label="ID" width="110">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column prop="code" label="编码" width="160" />
      <el-table-column prop="name" label="名称" min-width="160" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="描述" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.description) }}</template>
      </el-table-column>
      <el-table-column label="额度配置" min-width="240">
        <template #default="{ row }">
          <template v-if="row.features && row.features.length">
            <el-tag v-for="feature in row.features" :key="feature.feature_code" size="small" type="info" style="margin-right: 4px">
              {{ feature.feature_code }}={{ feature.limit_value ?? '不限额' }}
            </el-tag>
          </template>
          <span v-else class="text-muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button text type="primary" size="small" @click="openEdit(row as Plan)">编辑</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="dialogVisible" :title="editingId ? '编辑套餐' : '新建套餐'" width="600px" destroy-on-close>
    <el-form ref="formRef" :model="form" :rules="rules" label-width="96px">
      <el-form-item label="套餐编码" prop="code">
        <el-input v-model="form.code" :disabled="Boolean(editingId)" placeholder="如 basic" />
      </el-form-item>
      <el-form-item label="套餐名称" prop="name">
        <el-input v-model="form.name" placeholder="如 基础版" />
      </el-form-item>
      <el-form-item v-if="editingId" label="状态">
        <el-select v-model="form.status" style="width: 100%">
          <el-option label="启用" value="active" />
          <el-option label="停用" value="inactive" />
        </el-select>
      </el-form-item>
      <el-form-item label="描述">
        <el-input v-model="form.description" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="功能额度">
        <div style="width: 100%">
          <div v-for="(feature, index) in form.features" :key="index" style="display: flex; gap: 8px; margin-bottom: 8px">
            <el-input v-model="feature.feature_code" placeholder="功能编码，如 max_skus" style="flex: 1" />
            <el-input-number
              v-model="feature.limit_value"
              :min="0"
              :controls="false"
              placeholder="额度"
              style="width: 140px"
            />
            <el-button text type="danger" @click="removeFeature(index)">删除</el-button>
          </div>
          <el-button text type="primary" :icon="Plus" @click="addFeature">添加功能额度</el-button>
          <div class="form-tip">留空额度表示不限制；额度降低不会删除已有数据。</div>
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>
