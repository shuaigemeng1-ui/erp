<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { channelsApi } from '@/api/channels'
import { useTenantStore } from '@/stores/tenant'
import StatusTag from '@/components/StatusTag.vue'
import IdCopy from '@/components/IdCopy.vue'
import { formatDateTime, textOrDash } from '@/utils/format'
import type { Store } from '@/types'

const tenant = useTenantStore()
const tid = (): string => tenant.requireTenantId()

const loading = ref(false)
const stores = ref<Store[]>([])
const authorizing = ref(false)
const disconnectLoadingId = ref('')

const canConnect = computed(() => tenant.has('store.connect'))

const authorizeVisible = ref(false)
const authorizeChannel = ref<'shopee' | 'tiktok'>('tiktok')
const generatedAuthUrl = ref('')
const authorizeChannelLabel = computed(() =>
  authorizeChannel.value === 'tiktok' ? 'TikTok Shop' : 'Shopee',
)

async function load(): Promise<void> {
  loading.value = true
  try {
    stores.value = await channelsApi.stores(tid())
  } finally {
    loading.value = false
  }
}

function openAuthorize(): void {
  authorizeChannel.value = 'tiktok'
  generatedAuthUrl.value = ''
  authorizeVisible.value = true
}

function channelLabel(channel: string): string {
  if (channel === 'tiktok') return 'TikTok Shop'
  if (channel === 'shopee') return 'Shopee'
  return channel
}

async function confirmAuthorize(): Promise<void> {
  // 先同步开一个空白窗口，避免 await 之后被浏览器拦截弹窗
  const newWindow = window.open('', '_blank')
  authorizing.value = true
  try {
    const result =
      authorizeChannel.value === 'tiktok'
        ? await channelsApi.authorizeTikTok(tid())
        : await channelsApi.authorizeShopee(tid())
    generatedAuthUrl.value = result.auth_url
    if (newWindow) {
      newWindow.opener = null
      newWindow.location.href = result.auth_url
      ElMessage.success(`已在新标签页打开 ${authorizeChannelLabel.value} 授权页`)
    } else {
      ElMessage.warning('浏览器拦截了新窗口，请点击「复制授权链接」后手动打开')
    }
  } catch {
    newWindow?.close()
  } finally {
    authorizing.value = false
  }
}

function reopenAuthUrl(): void {
  if (!generatedAuthUrl.value) return
  const win = window.open(generatedAuthUrl.value, '_blank')
  if (win) {
    win.opener = null
  } else {
    ElMessage.warning('浏览器拦截了新窗口，请点击「复制授权链接」后手动打开')
  }
}

async function copyAuthUrl(): Promise<void> {
  const url = generatedAuthUrl.value
  if (!url) return
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(url)
    } else {
      throw new Error('clipboard unavailable')
    }
  } catch {
    const textarea = document.createElement('textarea')
    textarea.value = url
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    textarea.remove()
  }
  ElMessage.success('授权链接已复制')
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
  disconnectLoadingId.value = store.id
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

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="table-toolbar">
      <div class="toolbar-actions">
        <el-button :icon="Refresh" @click="load">刷新</el-button>
      </div>
      <el-button v-if="canConnect" type="primary" @click="openAuthorize">授权店铺</el-button>
    </div>

    <el-table v-loading="loading" :data="stores" border size="small" row-key="id">
      <el-table-column label="ID" width="110">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column prop="name" label="店铺名称" min-width="160" />
      <el-table-column label="渠道" width="100">
        <template #default="{ row }">{{ channelLabel(row.channel) }}</template>
      </el-table-column>
      <el-table-column label="店铺 ID" width="150">
        <template #default="{ row }"><span class="mono">{{ textOrDash(row.external_store_id) }}</span></template>
      </el-table-column>
      <el-table-column label="站点" width="90">
        <template #default="{ row }">{{ textOrDash(row.region) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="凭据状态" width="130">
        <template #default="{ row }"><StatusTag :status="credentialStatus(row as Store)" /></template>
      </el-table-column>
      <el-table-column label="Token 过期" width="170">
        <template #default="{ row }">
          {{ formatDateTime(row.credential?.access_token_expires_at ?? row.access_token_expires_at) }}
        </template>
      </el-table-column>
      <el-table-column label="授权时间" width="170">
        <template #default="{ row }">{{ formatDateTime(row.authorized_at) }}</template>
      </el-table-column>
      <el-table-column label="最近同步" width="170">
        <template #default="{ row }">{{ formatDateTime(row.last_synced_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="110" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="canConnect && row.status !== 'disconnected'"
            text
            type="danger"
            size="small"
            :loading="disconnectLoadingId === row.id"
            @click="disconnect(row as Store)"
          >
            断开
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>

  <el-dialog v-model="authorizeVisible" title="授权店铺" width="480px" destroy-on-close>
    <el-form label-width="86px" style="margin-bottom: 8px">
      <el-form-item label="授权渠道">
        <el-radio-group v-model="authorizeChannel">
          <el-radio-button label="tiktok">TikTok Shop</el-radio-button>
          <el-radio-button label="shopee">Shopee</el-radio-button>
        </el-radio-group>
      </el-form-item>
    </el-form>
    <el-alert type="info" :closable="false" show-icon>
      <p>点击「前往授权」会在新标签页打开 {{ authorizeChannelLabel }} 授权页，授权完成后自动回到本页。授权会话一次性有效。</p>
      <p class="authorize-hint">
        <template v-if="authorizeChannel === 'tiktok'">
          若提示「平台 TikTok Shop 应用未配置」，请先在 平台管理 → 渠道应用（TikTok Shop）填写 App ID / 应用密钥 / 应用密码，并将回调地址加入 TikTok 应用白名单。
        </template>
        <template v-else>
          若提示「平台 Shopee 应用未配置」，请先在 平台管理 → 渠道应用 填写 Partner ID / Partner Key / Secret。
        </template>
      </p>
      <div v-if="generatedAuthUrl" class="auth-link-box">
        <div class="auth-link-label">授权链接（可复制到任意浏览器打开）</div>
        <el-input :model-value="generatedAuthUrl" readonly>
          <template #append>
            <el-button @click="copyAuthUrl">复制</el-button>
          </template>
        </el-input>
      </div>
    </el-alert>
    <template #footer>
      <template v-if="generatedAuthUrl">
        <el-button @click="authorizeVisible = false">关闭</el-button>
        <el-button @click="copyAuthUrl">复制授权链接</el-button>
        <el-button type="primary" @click="reopenAuthUrl">再次打开</el-button>
      </template>
      <template v-else>
        <el-button @click="authorizeVisible = false">取消</el-button>
        <el-button type="primary" :loading="authorizing" @click="confirmAuthorize">前往授权</el-button>
      </template>
    </template>
  </el-dialog>
</template>

<style scoped>
.authorize-hint {
  margin-top: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.auth-link-box {
  margin-top: 12px;
}

.auth-link-label {
  margin-bottom: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
