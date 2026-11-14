<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { channelsApi } from '@/api/channels'
import { useTenantStore } from '@/stores/tenant'
import StatusTag from '@/components/StatusTag.vue'
import IdCopy from '@/components/IdCopy.vue'
import { formatDateTime, textOrDash } from '@/utils/format'
import type { AuthorizeResult, Store } from '@/types'

const tenant = useTenantStore()
const route = useRoute()
const router = useRouter()
const tid = (): string => tenant.requireTenantId()

const loading = ref(false)
const stores = ref<Store[]>([])
const disconnectLoadingId = ref('')
const authLoadingStoreId = ref('')

const canConnect = computed(() => tenant.has('store.connect'))

const createVisible = ref(false)
const creating = ref(false)
const createForm = reactive<{ channel: 'tiktok' | 'shopee'; name: string }>({
  channel: 'tiktok',
  name: '',
})

const authVisible = ref(false)
const authStore = ref<Store | null>(null)
const authResult = ref<AuthorizeResult | null>(null)
const oauthMessage = ref('')
const oauthError = ref(false)

async function load(): Promise<void> {
  loading.value = true
  try {
    stores.value = await channelsApi.stores(tid())
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  createForm.channel = 'tiktok'
  createForm.name = ''
  createVisible.value = true
}

async function copyText(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch {
    // fall through to execCommand fallback
  }
  try {
    const textarea = document.createElement('textarea')
    textarea.value = value
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    textarea.remove()
    return true
  } catch {
    return false
  }
}

async function submitCreate(): Promise<void> {
  const name = createForm.name.trim()
  if (!name) {
    ElMessage.warning('请填写 ERP 中显示的店铺名称')
    return
  }
  creating.value = true
  try {
    const created = await channelsApi.createStore(tid(), {
      channel: createForm.channel,
      name,
    })
    createVisible.value = false
    ElMessage.success(`店铺「${name}」已创建，状态：待授权`)
    await load()
    await openAuthorizationLink(created)
  } finally {
    creating.value = false
  }
}

async function openAuthorizationLink(store: Store): Promise<void> {
  authStore.value = store
  authResult.value = null
  authVisible.value = true
  authLoadingStoreId.value = String(store.id)
  try {
    const result = await channelsApi.authorizeStore(
      tid(),
      store.id,
      store.channel === 'shopee' ? 'shopee' : 'tiktok',
    )
    authResult.value = result
    const copied = await copyText(result.auth_url)
    if (copied) {
      ElMessage.success('授权链接已生成并复制')
    } else {
      ElMessage.warning('授权链接已生成，请点击「复制授权链接」手动复制')
    }
  } catch {
    authVisible.value = false
  } finally {
    authLoadingStoreId.value = ''
  }
}

async function copyAuthLink(): Promise<void> {
  if (!authResult.value) return
  const copied = await copyText(authResult.value.auth_url)
  if (copied) {
    ElMessage.success('授权链接已复制')
  } else {
    ElMessage.error('复制失败，请手动选中链接复制')
  }
}

function openAuthTab(): void {
  if (!authResult.value) return
  const win = window.open(authResult.value.auth_url, '_blank')
  if (win) {
    win.opener = null
  } else {
    ElMessage.warning('浏览器拦截了新窗口，请复制授权链接后手动打开')
  }
}

function channelLabel(channel: string): string {
  if (channel === 'tiktok') return 'TikTok Shop'
  if (channel === 'shopee') return 'Shopee'
  return channel
}

const authStoreLabel = computed(() => authStore.value?.name ?? '')

async function revokeAuthLink(store: Store): Promise<void> {
  try {
    await ElMessageBox.confirm(`撤销店铺「${store.name}」已生成的授权链接？`, '撤销授权链接', { type: 'warning' })
    await channelsApi.revokeAuthorizationLink(tid(), store.id)
    authResult.value = null
    authVisible.value = false
    ElMessage.success('授权链接已撤销')
  } catch (cause) {
    if (cause !== 'cancel' && cause !== 'close') console.error(cause)
  }
}

async function disconnect(store: Store): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认断开店铺「${store.name}」的授权？断开后依赖该凭据的同步任务将暂停。`,
      '断开授权',
      { type: 'warning', confirmButtonText: '确认断开', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  disconnectLoadingId.value = String(store.id)
  try {
    await channelsApi.disconnectStore(tid(), store.id)
    ElMessage.success('已断开授权')
    await load()
  } finally {
    disconnectLoadingId.value = ''
  }
}

function credentialStatus(store: Store): string {
  return store.credential?.status ?? store.credential_status ?? store.status
}

function platformStoreId(store: Store): string {
  return store.status === 'pending_authorization' ? '' : store.external_store_id
}

async function handleOAuthResult(): Promise<void> {
  const status = String(route.query.tiktok ?? '')
  if (status === 'connected') {
    oauthMessage.value = 'TikTok 授权成功，店铺已连接'
    oauthError.value = false
    ElMessage.success(oauthMessage.value)
    await load()
  } else if (status === 'error') {
    const reason = String(route.query.reason ?? '授权失败')
    oauthMessage.value = `TikTok 授权失败：${reason}`
    oauthError.value = true
    ElMessage.error(oauthMessage.value)
  }
  if (status || route.query.reason) {
    const query = { ...route.query }
    delete query.tiktok
    delete query.reason
    await router.replace({ query })
  }
}

onMounted(async () => {
  await load()
  await handleOAuthResult()
})
</script>

<template>
  <el-card shadow="never" class="page-card">
    <el-alert
      v-if="oauthMessage"
      :type="oauthError ? 'error' : 'success'"
      :title="oauthMessage"
      :closable="true"
      show-icon
      class="oauth-result-alert"
    />
    <div class="table-toolbar">
      <div class="toolbar-actions">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-button v-if="canConnect" type="primary" @click="openCreate">新建店铺</el-button>
    </div>

    <el-table v-loading="loading" :data="stores" border size="small" row-key="id">
      <el-table-column label="ID" width="100">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column prop="name" label="显示名称" min-width="160" />
      <el-table-column label="平台店铺名称" min-width="180">
        <template #default="{ row }">{{ textOrDash(row.external_name) }}</template>
      </el-table-column>
      <el-table-column label="区域" width="90">
        <template #default="{ row }">{{ textOrDash(row.region) }}</template>
      </el-table-column>
      <el-table-column label="店铺编码" width="130">
        <template #default="{ row }"><span class="mono">{{ textOrDash(row.store_code) }}</span></template>
      </el-table-column>
      <el-table-column label="渠道" width="110">
        <template #default="{ row }">{{ channelLabel(row.channel) }}</template>
      </el-table-column>
      <el-table-column label="平台店铺 ID" width="160">
        <template #default="{ row }">
          <span class="mono">{{ textOrDash(platformStoreId(row as Store)) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="110">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="凭据状态" width="120">
        <template #default="{ row }"><StatusTag :status="credentialStatus(row as Store)" /></template>
      </el-table-column>
      <el-table-column label="授权时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.authorized_at) }}</template>
      </el-table-column>
      <el-table-column label="最近同步" width="170">
        <template #default="{ row }">{{ formatDateTime(row.last_synced_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <template v-if="canConnect">
            <el-button
              v-if="row.status === 'pending_authorization' || row.status === 'reauth_required'"
              text
              type="primary"
              size="small"
              :loading="authLoadingStoreId === String(row.id)"
              @click="openAuthorizationLink(row as Store)"
            >
              复制授权链接
            </el-button>
            <el-button v-permission="'store.connect'" link type="warning" @click="revokeAuthLink(row as Store)">撤销链接</el-button>
            <el-button
              v-if="row.status === 'connected'"
              text
              type="danger"
              size="small"
              :loading="disconnectLoadingId === String(row.id)"
              @click="disconnect(row as Store)"
            >
              断开
            </el-button>
          </template>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="createVisible" title="新建店铺" width="500px" destroy-on-close>
    <el-form label-width="90px">
      <el-form-item label="渠道" required>
        <el-radio-group v-model="createForm.channel">
          <el-radio-button label="tiktok">TikTok Shop</el-radio-button>
          <el-radio-button label="shopee">Shopee</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="店铺名称" required>
        <el-input
          v-model="createForm.name"
          maxlength="128"
          show-word-limit
          placeholder="例如：英国店 / aligo UK"
        />
      </el-form-item>
    </el-form>
    <el-alert type="info" :closable="false" show-icon>
      <p>创建后店铺状态为「待授权」，不会参与订单/库存同步。</p>
      <p>系统会立即生成授权链接并自动复制；把它发给该店铺的主账号打开完成授权。</p>
    </el-alert>
    <template #footer>
      <el-button @click="createVisible = false">取消</el-button>
      <el-button type="primary" :loading="creating" @click="submitCreate">创建店铺</el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="authVisible"
    :title="`${authStore ? channelLabel(authStore.channel) : ''} 授权链接`"
    width="600px"
    destroy-on-close
  >
    <div v-if="authResult" class="auth-link-dialog">
      <el-descriptions :column="2" border size="small">
        <el-descriptions-item label="ERP 店铺">{{ authStoreLabel }}</el-descriptions-item>
        <el-descriptions-item label="店铺编码">
          <span class="mono">{{ textOrDash(authStore?.store_code) }}</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="authStore?.channel === 'tiktok'" label="Service ID">
          <span class="mono">{{ textOrDash(authResult.service_id) }}</span>
        </el-descriptions-item>
        <el-descriptions-item v-if="authStore?.channel === 'tiktok'" label="授权市场">
          {{ authResult.market || 'ROW' }}
        </el-descriptions-item>
        <el-descriptions-item v-else label="授权环境">
          {{ authResult.environment || '-' }}
        </el-descriptions-item>
      </el-descriptions>
      <div class="auth-link-label">完整授权链接</div>
      <el-input
        :model-value="authResult.auth_url"
        readonly
        type="textarea"
        :rows="5"
        resize="none"
      />
      <el-alert type="warning" :closable="false" show-icon class="auth-link-alert">
        <p>请复制链接，用该店铺的 TikTok 主账号在任意浏览器打开并同意授权。</p>
        <p>链接可长期使用；重新生成会使旧链接失效，也可以随时撤销。授权后刷新店铺状态。</p>
      </el-alert>
      <div class="auth-link-actions">
        <el-button @click="authVisible = false">关闭</el-button>
        <el-button @click="copyAuthLink">复制授权链接</el-button>
        <el-button type="primary" @click="openAuthTab">打开授权页</el-button>
      </div>
    </div>
    <el-skeleton v-else :rows="4" animated />
  </el-dialog>
</template>

<style scoped>
.auth-link-dialog {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.auth-link-label {
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.auth-link-alert {
  margin-top: 2px;
}

.auth-link-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
