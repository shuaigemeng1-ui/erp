<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules, type InputInstance } from 'element-plus'
import { Refresh, Search } from '@element-plus/icons-vue'
import { platformApi } from '@/api/platform'
import { useTableQuery } from '@/composables/useTableQuery'
import PaginationBar from '@/components/PaginationBar.vue'
import StatusTag from '@/components/StatusTag.vue'
import IdCopy from '@/components/IdCopy.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { formatDateTime, stringOrUndefined, textOrDash } from '@/utils/format'
import type { StatusMeta } from '@/utils/dict'
import type { Plan, PlatformTenant, PlatformTenantDetail, ProvisioningJob } from '@/types'

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
} = useTableQuery<PlatformTenant>((params) =>
  platformApi.tenants({
    page: params.page,
    page_size: params.page_size,
    q: stringOrUndefined(query.q),
    status: stringOrUndefined(query.status),
  }),
)

const plans = ref<Plan[]>([])
const timezoneOptions = ['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Singapore', 'Asia/Tokyo', 'UTC']

/* ------------------------------ 创建租户 ------------------------------ */
const createVisible = ref(false)
const createSubmitting = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({
  code: '',
  name: '',
  timezone: 'Asia/Shanghai',
  plan_id: '',
  owner_email: '',
})
const createRules: FormRules = {
  code: [
    { required: true, message: '请输入租户编码', trigger: 'blur' },
    { pattern: /^[a-z0-9][a-z0-9_-]*$/, message: '仅小写字母、数字、下划线和中划线', trigger: 'blur' },
  ],
  name: [{ required: true, message: '请输入租户名称', trigger: 'blur' }],
  timezone: [{ required: true, message: '请选择时区', trigger: 'change' }],
  owner_email: [
    { required: true, message: '请输入负责人邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
}

function openCreate(): void {
  createForm.code = ''
  createForm.name = ''
  createForm.timezone = 'Asia/Shanghai'
  createForm.plan_id = plans.value[0]?.id ?? ''
  createForm.owner_email = ''
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  const formInstance = createFormRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  createSubmitting.value = true
  try {
    const result = await platformApi.createTenant({
      code: createForm.code.trim(),
      name: createForm.name.trim(),
      timezone: createForm.timezone,
      plan_id: createForm.plan_id || undefined,
      owner_email: createForm.owner_email.trim(),
    })
    ElMessage.success(`已提交开通任务，task_id=${result.task_id}`)
    createVisible.value = false
    await reload()
  } finally {
    createSubmitting.value = false
  }
}

/* ------------------------------ 详情 ------------------------------ */
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref<PlatformTenantDetail | null>(null)

async function openDetail(row: PlatformTenant): Promise<void> {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await platformApi.tenant(row.id)
  } finally {
    detailLoading.value = false
  }
}

function jobStatusType(job: ProvisioningJob): 'success' | 'warning' | 'danger' | 'info' {
  if (job.status === 'succeeded') return 'success'
  if (job.status === 'failed') return 'danger'
  if (job.status === 'running') return 'warning'
  return 'info'
}

/* ------------------------------ 负责人邀请 ------------------------------ */
/** 服务器无邮件通道：平台管理员生成一次性邀请链接，复制后线下发给负责人。 */
const invitationStatusMeta: Record<string, StatusMeta> = {
  pending: { label: '待接受', type: 'warning' },
  invited: { label: '待接受', type: 'warning' },
  accepted: { label: '已接受', type: 'success' },
  revoked: { label: '已撤销', type: 'info' },
  expired: { label: '已过期', type: 'danger' },
}

const ownerInvitation = computed(
  () => detail.value?.owner_invitation ?? detail.value?.ownerInvitation ?? null,
)

const ownerInvitationExpired = computed((): boolean => {
  const invitation = ownerInvitation.value
  if (!invitation) return false
  if (invitation.status === 'expired') return true
  if (invitation.status === 'accepted' || invitation.status === 'revoked') return false
  if (!invitation.expires_at) return false
  const expiresAt = Date.parse(invitation.expires_at)
  return Number.isFinite(expiresAt) && expiresAt <= Date.now()
})

/** 无邀请或已过期/已撤销时允许生成 */
const canIssueInvitation = computed(
  () => !ownerInvitation.value || ownerInvitationExpired.value || ownerInvitation.value.status === 'revoked',
)

/** 邀请仍有效时允许重新生成（旧的链接会立即失效，需要二次确认） */
const canRegenerateInvitation = computed(
  () => Boolean(ownerInvitation.value) && !canIssueInvitation.value && ownerInvitation.value?.status !== 'accepted',
)

const invitationCreating = ref(false)
const invitationDialogVisible = ref(false)
const invitationLink = ref('')
const invitationEmail = ref('')
const invitationExpiresAt = ref<string | null>(null)
const invitationInputRef = ref<InputInstance>()

function buildInvitationUrl(invitationUrl: string): string {
  const path = (invitationUrl ?? '').trim()
  if (!path) return ''
  if (/^https?:\/\//i.test(path)) return path
  return `${window.location.origin}${path.startsWith('/') ? path : `/${path}`}`
}

async function issueOwnerInvitation(): Promise<void> {
  const current = detail.value
  if (!current) return
  invitationCreating.value = true
  try {
    const result = await platformApi.issueOwnerInvitation(current.id)
    invitationEmail.value = result.email || current.owner_email || ''
    invitationExpiresAt.value = result.expires_at
    invitationLink.value = buildInvitationUrl(result.invitation_url)
    invitationDialogVisible.value = true
    // 刷新详情，回显最新的邀请状态（刷新失败不影响已生成的链接）
    try {
      detail.value = await platformApi.tenant(current.id)
    } catch {
      // 忽略：保持当前详情
    }
  } finally {
    invitationCreating.value = false
  }
}

async function regenerateOwnerInvitation(): Promise<void> {
  try {
    await ElMessageBox.confirm('重新生成后，之前发出的激活链接将立即失效。确认继续？', '重新生成邀请链接', {
      type: 'warning',
      confirmButtonText: '确认重新生成',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await issueOwnerInvitation()
}

function selectInvitationLink(): void {
  invitationInputRef.value?.focus()
  invitationInputRef.value?.select()
}

async function copyInvitationLink(): Promise<void> {
  const link = invitationLink.value
  if (!link) return

  // 1) 安全上下文（https）优先使用 Clipboard API
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(link)
      ElMessage.success('邀请链接已复制')
      return
    }
  } catch {
    // 继续走降级方案
  }

  // 2) 非安全上下文（http 部署）降级：选中输入框内容后尝试 execCommand
  await nextTick()
  selectInvitationLink()
  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  }
  if (copied) {
    ElMessage.success('邀请链接已复制')
    return
  }

  // 3) 最后降级：提示手动复制（链接已全选）
  ElMessage.warning('自动复制失败，链接已选中，请按 Ctrl+C 手动复制')
}

/* ------------------------------ 生命周期操作 ------------------------------ */
const actionLoadingId = ref('')

async function retryProvision(row: PlatformTenant): Promise<void> {
  actionLoadingId.value = row.id
  try {
    const result = await platformApi.retryProvision(row.id)
    ElMessage.success(`已提交重试任务，task_id=${result.task_id}`)
    await reload()
  } finally {
    actionLoadingId.value = ''
  }
}

async function suspendTenant(row: PlatformTenant): Promise<void> {
  let reason = ''
  try {
    const result = await ElMessageBox.prompt('暂停后租户业务访问与主动同步将被禁止，请填写原因。', '暂停租户', {
      confirmButtonText: '确认暂停',
      cancelButtonText: '取消',
      type: 'warning',
      inputPlaceholder: '暂停原因',
      inputValidator: (value: string) => (value && value.trim().length > 0 ? true : '请填写暂停原因'),
    })
    reason = result.value
  } catch {
    return
  }
  actionLoadingId.value = row.id
  try {
    await platformApi.suspendTenant(row.id, reason.trim())
    ElMessage.success('租户已暂停')
    await reload()
  } finally {
    actionLoadingId.value = ''
  }
}

async function activateTenant(row: PlatformTenant): Promise<void> {
  try {
    await ElMessageBox.confirm('重新启用前请确认平台侧状态已核对。', '启用租户', {
      confirmButtonText: '确认启用',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  actionLoadingId.value = row.id
  try {
    await platformApi.activateTenant(row.id)
    ElMessage.success('租户已启用')
    await reload()
  } finally {
    actionLoadingId.value = ''
  }
}

onMounted(async () => {
  try {
    plans.value = await platformApi.plans()
  } catch {
    // 套餐列表失败不阻断租户列表
  }
})
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-input v-model="query.q" placeholder="租户名称/编码" clearable style="width: 220px" @keyup.enter="search()" />
      <el-select v-model="query.status" placeholder="状态" clearable style="width: 160px" @change="search()">
        <el-option label="开通中" value="provisioning" />
        <el-option label="已启用" value="active" />
        <el-option label="已暂停" value="suspended" />
        <el-option label="开通失败" value="provision_failed" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="search()">查询</el-button>
      <el-button :icon="Refresh" @click="reload()">刷新</el-button>
      <div style="flex: 1" />
      <el-button type="primary" @click="openCreate">创建租户</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" border size="small" row-key="id">
      <el-table-column label="ID" width="110">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column prop="code" label="编码" width="140" />
      <el-table-column prop="name" label="名称" min-width="160" show-overflow-tooltip />
      <el-table-column label="状态" width="110">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column prop="timezone" label="时区" width="140" />
      <el-table-column label="套餐" width="120">
        <template #default="{ row }">{{ textOrDash(row.plan_name) }}</template>
      </el-table-column>
      <el-table-column label="负责人" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ textOrDash(row.owner_email) }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="250" fixed="right">
        <template #default="{ row }">
          <el-button text type="primary" size="small" @click="openDetail(row as PlatformTenant)">详情</el-button>
          <el-button
            v-if="row.status === 'provision_failed'"
            text
            type="warning"
            size="small"
            :loading="actionLoadingId === row.id"
            @click="retryProvision(row as PlatformTenant)"
          >
            重试开通
          </el-button>
          <el-button
            v-if="row.status === 'active'"
            text
            type="danger"
            size="small"
            :loading="actionLoadingId === row.id"
            @click="suspendTenant(row as PlatformTenant)"
          >
            暂停
          </el-button>
          <el-button
            v-if="row.status === 'suspended'"
            text
            type="success"
            size="small"
            :loading="actionLoadingId === row.id"
            @click="activateTenant(row as PlatformTenant)"
          >
            启用
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

  <!-- 创建租户 -->
  <el-dialog v-model="createVisible" title="创建租户" width="520px" destroy-on-close>
    <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-width="110px">
      <el-form-item label="租户编码" prop="code">
        <el-input v-model="createForm.code" placeholder="如 demo_shop" />
      </el-form-item>
      <el-form-item label="租户名称" prop="name">
        <el-input v-model="createForm.name" placeholder="经营主体名称" />
      </el-form-item>
      <el-form-item label="时区" prop="timezone">
        <el-select v-model="createForm.timezone" filterable allow-create style="width: 100%">
          <el-option v-for="zone in timezoneOptions" :key="zone" :label="zone" :value="zone" />
        </el-select>
      </el-form-item>
      <el-form-item label="套餐" prop="plan_id">
        <el-select v-model="createForm.plan_id" clearable placeholder="可选" style="width: 100%">
          <el-option v-for="plan in plans" :key="plan.id" :label="`${plan.name}（${plan.code}）`" :value="plan.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="负责人邮箱" prop="owner_email">
        <el-input v-model="createForm.owner_email" placeholder="租户负责人登录邮箱" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="createVisible = false">取消</el-button>
      <el-button type="primary" :loading="createSubmitting" @click="submitCreate">提交开通</el-button>
    </template>
  </el-dialog>

  <!-- 租户详情 -->
  <el-drawer v-model="detailVisible" title="租户详情" size="720px" destroy-on-close>
    <el-skeleton v-if="detailLoading" :rows="6" animated />
    <template v-else-if="detail">
      <el-descriptions :column="2" border size="small" class="detail-descriptions">
        <el-descriptions-item label="ID"><IdCopy :id="detail.id" /></el-descriptions-item>
        <el-descriptions-item label="编码">{{ detail.code }}</el-descriptions-item>
        <el-descriptions-item label="名称">{{ detail.name }}</el-descriptions-item>
        <el-descriptions-item label="状态"><StatusTag :status="detail.status" /></el-descriptions-item>
        <el-descriptions-item label="时区">{{ detail.timezone }}</el-descriptions-item>
        <el-descriptions-item label="套餐">{{ textOrDash(detail.plan_name) }}</el-descriptions-item>
        <el-descriptions-item label="负责人邮箱">{{ textOrDash(detail.owner_email) }}</el-descriptions-item>
        <el-descriptions-item label="暂停时间">{{ formatDateTime(detail.suspended_at) }}</el-descriptions-item>
        <el-descriptions-item label="数据库">
          {{ detail.database?.database_name || '-' }}
          <el-tag v-if="detail.database?.status" size="small" style="margin-left: 4px">
            {{ detail.database.status }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Schema 版本">{{ textOrDash(detail.database?.schema_version) }}</el-descriptions-item>
        <el-descriptions-item label="创建时间">{{ formatDateTime(detail.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="更新时间">{{ formatDateTime(detail.updated_at) }}</el-descriptions-item>
      </el-descriptions>

      <div style="margin: 12px 0 4px">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px">
          <span style="font-weight: 600">负责人邀请</span>
          <div>
            <el-button
              v-if="canRegenerateInvitation"
              text
              type="warning"
              size="small"
              :loading="invitationCreating"
              @click="regenerateOwnerInvitation"
            >
              重新生成
            </el-button>
            <el-button
              v-if="canIssueInvitation"
              type="primary"
              size="small"
              :loading="invitationCreating"
              @click="issueOwnerInvitation"
            >
              {{ ownerInvitation ? '重新生成邀请链接' : '生成邀请链接' }}
            </el-button>
          </div>
        </div>
        <el-descriptions v-if="ownerInvitation" :column="2" border size="small" class="detail-descriptions">
          <el-descriptions-item label="邀请邮箱">{{ textOrDash(ownerInvitation.email) }}</el-descriptions-item>
          <el-descriptions-item label="邀请状态">
            <StatusTag :status="ownerInvitation.status" :map="invitationStatusMeta" />
          </el-descriptions-item>
          <el-descriptions-item label="邀请时间">{{ formatDateTime(ownerInvitation.invited_at) }}</el-descriptions-item>
          <el-descriptions-item label="过期时间">{{ formatDateTime(ownerInvitation.expires_at) }}</el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-else
          title="尚未生成负责人邀请链接，负责人无法激活账号。"
          type="warning"
          :closable="false"
          show-icon
        />
      </div>

      <div style="font-weight: 600; margin: 12px 0 8px">开通步骤</div>
      <el-table :data="detail.provisioning_jobs ?? []" border size="small" empty-text="暂无开通任务">
        <el-table-column label="步骤" prop="step" min-width="140" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag :type="jobStatusType(row as ProvisioningJob)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="尝试次数" prop="attempts" width="90" />
        <el-table-column label="开始时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="结束时间" width="160">
          <template #default="{ row }">{{ formatDateTime(row.finished_at) }}</template>
        </el-table-column>
        <el-table-column label="错误" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ textOrDash(row.error_message) }}</template>
        </el-table-column>
      </el-table>

      <div v-if="detail.database" style="margin-top: 16px">
        <div style="font-weight: 600; margin-bottom: 8px">数据库路由</div>
        <JsonBlock :value="detail.database" />
      </div>
    </template>
  </el-drawer>

  <!-- 负责人邀请链接 -->
  <el-dialog v-model="invitationDialogVisible" title="负责人邀请链接" width="640px" destroy-on-close>
    <el-form label-width="88px">
      <el-form-item label="邀请邮箱">{{ textOrDash(invitationEmail) }}</el-form-item>
      <el-form-item label="过期时间">{{ formatDateTime(invitationExpiresAt) }}</el-form-item>
      <el-form-item label="邀请链接">
        <el-input
          ref="invitationInputRef"
          :model-value="invitationLink"
          readonly
          @focus="selectInvitationLink"
        />
      </el-form-item>
    </el-form>
    <el-alert
      title="服务器无邮件通道：请复制链接后通过其他渠道发给负责人，链接一次性有效。"
      type="warning"
      :closable="false"
      show-icon
    />
    <template #footer>
      <el-button @click="invitationDialogVisible = false">关闭</el-button>
      <el-button type="primary" @click="copyInvitationLink">复制链接</el-button>
    </template>
  </el-dialog>
</template>
