<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { tenantApi } from '@/api/tenant'
import { channelsApi } from '@/api/channels'
import { inventoryApi } from '@/api/inventory'
import { useTableQuery } from '@/composables/useTableQuery'
import { useTenantStore } from '@/stores/tenant'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusTag from '@/components/StatusTag.vue'
import IdCopy from '@/components/IdCopy.vue'
import ScopeBindingEditor from '@/components/ScopeBindingEditor.vue'
import { formatDateTime, stringOrUndefined, textOrDash } from '@/utils/format'
import { scopeTypeLabels } from '@/utils/dict'
import type { Member, Role, RoleBinding, RoleBindingInput, StockPlace, Store } from '@/types'

const tenant = useTenantStore()
const tid = (): string => tenant.requireTenantId()

const query = reactive({
  q: '',
  status: '',
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
} = useTableQuery<Member>((params) =>
  tenantApi.members(tid(), {
    page: params.page,
    page_size: params.page_size,
    q: stringOrUndefined(query.q),
    status: stringOrUndefined(query.status),
  }),
)

const roles = ref<Role[]>([])
const stores = ref<Store[]>([])
const stockPlaces = ref<StockPlace[]>([])

const canAssign = computed(() => tenant.has('role.assign'))
const canInvite = computed(() => tenant.has('member.invite'))

/* ------------------------------ 邀请成员 ------------------------------ */
const inviteVisible = ref(false)
const inviteSubmitting = ref(false)
const inviteFormRef = ref<FormInstance>()
const inviteForm = reactive({
  email: '',
  role_bindings: [] as RoleBindingInput[],
})
const inviteRules: FormRules = {
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
}

function openInvite(): void {
  inviteForm.email = ''
  inviteForm.role_bindings = []
  inviteVisible.value = true
}

async function submitInvite(): Promise<void> {
  const formInstance = inviteFormRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  const validBindings = inviteForm.role_bindings.filter((binding) => binding.role_id)
  if (validBindings.length === 0) {
    ElMessage.warning('请至少选择一个角色')
    return
  }
  inviteSubmitting.value = true
  try {
    const result = await tenantApi.inviteMember(tid(), {
      email: inviteForm.email.trim(),
      role_bindings: validBindings.map((binding) => ({ role_id: binding.role_id, scopes: binding.scopes })),
    })
    ElMessage.success(`邀请已发出，有效期至 ${formatDateTime(result.expires_at)}`)
    inviteVisible.value = false
  } finally {
    inviteSubmitting.value = false
  }
}

/* ------------------------------ 角色与范围 ------------------------------ */
const roleVisible = ref(false)
const roleSubmitting = ref(false)
const roleMember = ref<Member | null>(null)
const roleBindings = ref<RoleBindingInput[]>([])

function toBindingInput(bindings: RoleBinding[] | undefined): RoleBindingInput[] {
  if (!bindings || bindings.length === 0) return []
  return bindings.map((binding) => ({
    role_id: binding.role_id,
    scopes:
      binding.scopes && binding.scopes.length > 0
        ? binding.scopes.map((scope) => ({ scope_type: scope.scope_type, scope_ids: [...scope.scope_ids] }))
        : [{ scope_type: 'all', scope_ids: [] }],
  }))
}

function openRoles(member: Member): void {
  roleMember.value = member
  roleBindings.value = toBindingInput(member.role_bindings)
  roleVisible.value = true
}

async function submitRoles(): Promise<void> {
  const member = roleMember.value
  if (!member) return
  const validBindings = roleBindings.value.filter((binding) => binding.role_id)
  roleSubmitting.value = true
  try {
    await tenantApi.updateMemberRoles(tid(), member.id, {
      role_bindings: validBindings.map((binding) => ({ role_id: binding.role_id, scopes: binding.scopes })),
    })
    ElMessage.success('角色与数据范围已更新')
    roleVisible.value = false
    await reload()
  } finally {
    roleSubmitting.value = false
  }
}

/* ------------------------------ 停用/启用 ------------------------------ */
const statusLoadingId = ref('')

async function toggleStatus(member: Member): Promise<void> {
  const nextStatus = member.status === 'disabled' ? 'active' : 'disabled'
  if (nextStatus === 'disabled') {
    try {
      await ElMessageBox.confirm('停用后该成员会话立即失效，未执行的手动任务也不再允许。', '停用成员', {
        type: 'warning',
        confirmButtonText: '确认停用',
        cancelButtonText: '取消',
      })
    } catch {
      return
    }
  }
  statusLoadingId.value = member.id
  try {
    await tenantApi.updateMemberStatus(tid(), member.id, { status: nextStatus })
    ElMessage.success(nextStatus === 'disabled' ? '成员已停用' : '成员已启用')
    await reload()
  } finally {
    statusLoadingId.value = ''
  }
}

function scopeSummary(member: Member): string {
  const bindings = member.role_bindings ?? []
  if (bindings.length === 0) return '-'
  return bindings
    .map((binding) => {
      const roleName = binding.role_name ?? binding.role_code ?? binding.role_id
      const scopes = binding.scopes ?? []
      if (scopes.length === 0) return `${roleName}：无范围`
      const parts = scopes.map((scope) => {
        const label = scopeTypeLabels[scope.scope_type] ?? scope.scope_type
        return scope.scope_type === 'all' ? label : `${label}(${scope.scope_ids.length})`
      })
      return `${roleName}：${parts.join('、')}`
    })
    .join('、')
}

async function loadReferences(): Promise<void> {
  if (tenant.has('role.manage') || tenant.has('role.assign')) {
    try {
      roles.value = await tenantApi.roles(tid())
    } catch {
      roles.value = []
    }
  }
  if (tenant.has('store.read')) {
    try {
      stores.value = await channelsApi.stores(tid())
    } catch {
      stores.value = []
    }
  }
  if (tenant.has('inventory.read')) {
    try {
      stockPlaces.value = await inventoryApi.stockPlaces(tid())
    } catch {
      stockPlaces.value = []
    }
  }
}

onMounted(loadReferences)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.q" placeholder="姓名/邮箱" clearable style="width: 220px" @keyup.enter="search()" />
      <el-select v-model="query.status" placeholder="状态" clearable style="width: 140px" @change="search()">
        <el-option label="正常" value="active" />
        <el-option label="已邀请" value="invited" />
        <el-option label="已停用" value="disabled" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
      <div style="flex: 1" />
      <el-button v-permission="'member.invite'" type="primary" :icon="Plus" @click="openInvite">邀请成员</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="成员 ID" width="110">
        <template #default="{ row }"><IdCopy :id="row.member_no" /></template>
      </el-table-column>
      <el-table-column prop="display_name" label="姓名" width="140" />
      <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip />
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="角色" min-width="160">
        <template #default="{ row }">
          <template v-if="row.roles && row.roles.length">
            <el-tag v-for="role in row.roles" :key="role" size="small" type="info" style="margin-right: 4px">
              {{ role }}
            </el-tag>
          </template>
          <span v-else class="text-muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="数据范围" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">{{ scopeSummary(row as Member) }}</template>
      </el-table-column>
      <el-table-column label="加入时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.joined_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="canAssign"
            text
            type="primary"
            size="small"
            @click="openRoles(row as Member)"
          >
            角色与范围
          </el-button>
          <el-button
            v-if="canInvite && row.status !== 'invited'"
            text
            :type="row.status === 'disabled' ? 'success' : 'danger'"
            size="small"
            :loading="statusLoadingId === row.id"
            @click="toggleStatus(row as Member)"
          >
            {{ row.status === 'disabled' ? '启用' : '停用' }}
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

  <!-- 邀请成员 -->
  <el-dialog v-model="inviteVisible" title="邀请成员" width="640px" destroy-on-close>
    <el-form ref="inviteFormRef" :model="inviteForm" :rules="inviteRules" label-width="96px">
      <el-form-item label="邮箱" prop="email">
        <el-input v-model="inviteForm.email" placeholder="被邀请人邮箱" />
      </el-form-item>
      <el-form-item label="角色与范围">
        <ScopeBindingEditor
          v-model="inviteForm.role_bindings"
          :roles="roles"
          :stores="stores"
          :stock-places="stockPlaces"
        />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="inviteVisible = false">取消</el-button>
      <el-button type="primary" :loading="inviteSubmitting" @click="submitInvite">发送邀请</el-button>
    </template>
  </el-dialog>

  <!-- 角色与数据范围 -->
  <el-dialog v-model="roleVisible" title="角色与数据范围" width="720px" destroy-on-close>
    <el-alert
      v-if="roles.length === 0"
      title="当前账号缺少角色管理权限，无法加载角色列表或调整绑定。"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 12px"
    />
    <div v-else>
      <el-descriptions :column="2" border size="small" class="detail-descriptions">
        <el-descriptions-item label="成员">{{ roleMember?.display_name }}</el-descriptions-item>
        <el-descriptions-item label="邮箱">{{ textOrDash(roleMember?.email) }}</el-descriptions-item>
      </el-descriptions>
      <ScopeBindingEditor
        v-model="roleBindings"
        :roles="roles"
        :stores="stores"
        :stock-places="stockPlaces"
      />
    </div>
    <template #footer>
      <el-button @click="roleVisible = false">取消</el-button>
      <el-button type="primary" :loading="roleSubmitting" :disabled="roles.length === 0" @click="submitRoles">
        保存
      </el-button>
    </template>
  </el-dialog>
</template>
