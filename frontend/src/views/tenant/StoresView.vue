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

async function load(): Promise<void> {
  loading.value = true
  try {
    stores.value = await channelsApi.stores(tid())
  } finally {
    loading.value = false
  }
}

function openAuthorize(): void {
  authorizeVisible.value = true
}

async function confirmAuthorize(): Promise<void> {
  authorizing.value = true
  try {
    const result = await channelsApi.authorizeShopee(tid())
    authorizeVisible.value = false
    ElMessage.success('授权会话已生成，即将跳转 Shopee 授权页')
    // 同一标签页跳转，平台回调完成后 302 回店铺页
    window.location.href = result.auth_url
  } finally {
    authorizing.value = false
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
      <el-button v-if="canConnect" type="primary" @click="openAuthorize">授权 Shopee 店铺</el-button>
    </div>

    <el-table v-loading="loading" :data="stores" border size="small" row-key="id">
      <el-table-column label="ID" width="110">
        <template #default="{ row }"><IdCopy :id="row.id" /></template>
      </el-table-column>
      <el-table-column prop="name" label="店铺名称" min-width="160" />
      <el-table-column label="渠道" width="100">
        <template #default="{ row }">{{ row.channel }}</template>
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

  <el-dialog v-model="authorizeVisible" title="授权 Shopee 店铺" width="460px" destroy-on-close>
    <el-alert
      title="将跳转到 Shopee 授权页，授权完成后自动回到本页。授权会话一次性有效。"
      type="info"
      :closable="false"
      show-icon
    />
    <template #footer>
      <el-button @click="authorizeVisible = false">取消</el-button>
      <el-button type="primary" :loading="authorizing" @click="confirmAuthorize">前往授权</el-button>
    </template>
  </el-dialog>
</template>
