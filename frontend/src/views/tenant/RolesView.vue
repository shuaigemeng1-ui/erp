<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { tenantApi } from '@/api/tenant'
import { useTenantStore } from '@/stores/tenant'
import IdCopy from '@/components/IdCopy.vue'
import { textOrDash } from '@/utils/format'
import type { PermissionItem, Role } from '@/types'

const tenant = useTenantStore()
const tid = (): string => tenant.requireTenantId()

const loading = ref(false)
const roles = ref<Role[]>([])
const permissions = ref<PermissionItem[]>([])

const groupLabels: Record<string, string> = {
  catalog: '商品',
  inventory: '库存',
  store: '店铺',
  stores: '店铺',
  channel: '渠道商品',
  order: '订单',
  orders: '订单',
  stock: '库存推送',
  member: '成员',
  members: '成员',
  role: '角色权限',
  roles: '角色权限',
  audit: '审计',
  task: '任务',
  tasks: '任务',
  tenant: '租户设置',
}

const permissionGroups = computed(() => {
  const grouped = new Map<string, PermissionItem[]>()
  for (const item of permissions.value) {
    const key = item.group ?? item.code.split('.')[0] ?? '其他'
    const list = grouped.get(key) ?? []
    list.push(item)
    grouped.set(key, list)
  }
  return [...grouped.entries()].map(([group, items]) => ({
    group,
    label: groupLabels[group] ?? group,
    items,
  }))
})

const dialogVisible = ref(false)
const submitting = ref(false)
const editingRole = ref<Role | null>(null)
const formRef = ref<FormInstance>()
const form = reactive({
  code: '',
  name: '',
  description: '',
  copy_from_role_id: '',
  permissions: [] as string[],
})
const rules: FormRules = {
  code: [
    { required: true, message: '请输入角色编码', trigger: 'blur' },
    { pattern: /^[a-z0-9][a-z0-9_.-]*$/, message: '仅小写字母、数字、下划线、点和中划线', trigger: 'blur' },
  ],
  name: [{ required: true, message: '请输入角色名称', trigger: 'blur' }],
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [roleList, permissionList] = await Promise.all([tenantApi.roles(tid()), tenantApi.permissions(tid())])
    roles.value = roleList
    permissions.value = permissionList
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  editingRole.value = null
  form.code = ''
  form.name = ''
  form.description = ''
  form.copy_from_role_id = ''
  form.permissions = []
  dialogVisible.value = true
}

function openEdit(role: Role): void {
  editingRole.value = role
  form.code = role.code
  form.name = role.name
  form.description = role.description ?? ''
  form.copy_from_role_id = ''
  form.permissions = [...role.permissions]
  dialogVisible.value = true
}

function onCopyFromChange(roleId: string): void {
  const source = roles.value.find((role) => role.id === roleId)
  if (source) {
    form.permissions = [...source.permissions]
    if (!form.name) form.name = `${source.name}副本`
  }
}

async function submit(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    if (editingRole.value) {
      await tenantApi.updateRole(tid(), editingRole.value.id, {
        name: form.name.trim(),
        permissions: form.permissions,
        description: form.description.trim(),
      })
      ElMessage.success('角色已更新，权限版本已递增')
    } else {
      await tenantApi.createRole(tid(), {
        code: form.code.trim(),
        name: form.name.trim(),
        permissions: form.permissions,
        description: form.description.trim() || undefined,
        copy_from_role_id: form.copy_from_role_id || undefined,
      })
      ElMessage.success('角色已创建')
    }
    dialogVisible.value = false
    await load()
  } finally {
    submitting.value = false
  }
}

async function removeRole(role: Role): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除角色「${role.name}」？系统角色或仍有成员绑定时无法删除。`, '删除角色', {
      type: 'warning',
      confirmButtonText: '确认删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await tenantApi.deleteRole(tid(), role.id)
  ElMessage.success('角色已删除')
  await load()
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="table-toolbar">
      <div class="toolbar-actions">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建角色</el-button>
    </div>

    <el-table v-loading="loading" :data="roles" border size="small" row-key="id">
      <el-table-column label="ID" width="110">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column prop="code" label="编码" width="180" />
      <el-table-column prop="name" label="名称" width="160" />
      <el-table-column label="系统角色" width="100">
        <template #default="{ row }">
          <el-tag :type="row.is_system ? 'warning' : 'info'" size="small">{{ row.is_system ? '是' : '否' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="权限" min-width="320">
        <template #default="{ row }">
          <el-tag v-for="code in row.permissions" :key="code" size="small" type="info" style="margin: 2px">
            {{ code }}
          </el-tag>
          <span v-if="!row.permissions || row.permissions.length === 0" class="text-muted">无权限</span>
        </template>
      </el-table-column>
      <el-table-column label="描述" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.description) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button text type="primary" size="small" @click="openEdit(row as Role)">编辑</el-button>
          <el-button
            v-if="!row.is_system"
            text
            type="danger"
            size="small"
            @click="removeRole(row as Role)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog
    v-model="dialogVisible"
    :title="editingRole ? '编辑角色' : '新建角色'"
    width="760px"
    destroy-on-close
    top="6vh"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="96px">
      <el-form-item label="角色编码" prop="code">
        <el-input v-model="form.code" :disabled="Boolean(editingRole)" placeholder="如 order_operator" />
      </el-form-item>
      <el-form-item label="角色名称" prop="name">
        <el-input v-model="form.name" placeholder="如 订单运营" />
      </el-form-item>
      <el-form-item v-if="!editingRole" label="复制自">
        <el-select v-model="form.copy_from_role_id" clearable placeholder="可选：从现有角色复制权限" style="width: 100%" @change="onCopyFromChange">
          :label="`${role.name}（${role.code}）`"
        </el-select>
      </el-form-item>
      <el-form-item label="描述">
        <el-input v-model="form.description" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="权限">
        <div style="max-height: 46vh; overflow: auto; width: 100%">
          <div v-for="group in permissionGroups" :key="group.group" style="margin-bottom: 10px">
            <div style="font-weight: 600; margin-bottom: 4px">{{ group.label }}</div>
            <el-checkbox-group v-model="form.permissions">
              <el-checkbox
                v-for="item in group.items"
                :key="item.code"
                :value="item.code"
                style="width: 300px"
              >
                {{ item.name }} <span class="text-muted mono">{{ item.code }}</span>
              </el-checkbox>
            </el-checkbox-group>
          </div>
          <span v-if="permissionGroups.length === 0" class="text-muted">未获取到权限目录</span>
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
    </template>
  </el-dialog>
</template>
