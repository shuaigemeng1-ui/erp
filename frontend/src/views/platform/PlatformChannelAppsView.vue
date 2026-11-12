<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { platformApi } from '@/api/platform'
import StatusTag from '@/components/StatusTag.vue'
import { environmentLabels } from '@/utils/dict'
import { textOrDash } from '@/utils/format'
import type { ChannelApp, ChannelAppPayload } from '@/types'

const channel = ref<'shopee' | 'tiktok'>('tiktok')
const environment = ref('production')
const loading = ref(false)
const saving = ref(false)
const app = ref<ChannelApp | null>(null)
const isTikTok = computed(() => channel.value === 'tiktok')
const channelLabel = computed(() => (isTikTok.value ? 'TikTok Shop' : 'Shopee'))
const redirectPlaceholder = computed(
  () => `${window.location.origin}/api/v1/integrations/${channel.value}/oauth/callback`,
)

const formRef = ref<FormInstance>()
const channelForm = reactive({
  partner_id: '',
  partner_key: '',
  partner_secret: '',
  app_key: '',
  app_secret: '',
  redirect_url: '',
})
const rules: FormRules = {
  redirect_url: [{ required: true, message: '请输入回调地址', trigger: 'blur' }],
}

async function load(): Promise<void> {
  loading.value = true
  try {
    app.value = await platformApi.channelApp(channel.value, environment.value)
    channelForm.partner_id = app.value.partner_id ?? ''
    channelForm.app_key = app.value.app_key ?? ''
    channelForm.redirect_url = app.value.redirect_url || redirectPlaceholder.value
    channelForm.partner_key = ''
    channelForm.partner_secret = ''
    channelForm.app_secret = ''
  } catch {
    app.value = null
    channelForm.partner_id = ''
    channelForm.app_key = ''
    channelForm.redirect_url = redirectPlaceholder.value
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  if (isTikTok.value && !channelForm.app_key.trim()) {
    ElMessage.warning('请填写 TikTok App Key')
    return
  }
  if (!isTikTok.value && !channelForm.partner_id.trim()) {
    ElMessage.warning('请填写 Shopee Partner ID')
    return
  }
  saving.value = true
  try {
    const payload: ChannelAppPayload = { redirect_url: channelForm.redirect_url.trim() }
    if (isTikTok.value) {
      payload.app_key = channelForm.app_key.trim()
      // 密钥不回显：只有填写时才提交
      if (channelForm.app_secret.trim()) payload.app_secret = channelForm.app_secret.trim()
    } else {
      payload.partner_id = channelForm.partner_id.trim()
      if (channelForm.partner_key.trim()) payload.partner_key = channelForm.partner_key.trim()
      if (channelForm.partner_secret.trim()) {
        payload.partner_secret = channelForm.partner_secret.trim()
      }
    }
    app.value = await platformApi.saveChannelApp(channel.value, environment.value, payload)
    channelForm.partner_key = ''
    channelForm.partner_secret = ''
    channelForm.app_secret = ''
    ElMessage.success('渠道应用已保存')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar">
      <el-select v-model="channel" style="width: 160px" @change="load">
        <el-option label="TikTok Shop" value="tiktok" />
        <el-option label="Shopee" value="shopee" />
      </el-select>
      <el-select v-model="environment" style="width: 160px" @change="load">
        <el-option label="生产环境" value="production" />
        <el-option label="测试环境" value="test" />
      </el-select>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-descriptions :column="3" border size="small" class="detail-descriptions">
      <el-descriptions-item label="渠道">{{ channelLabel }}</el-descriptions-item>
      <el-descriptions-item label="环境">
        {{ environmentLabels[app?.environment ?? environment] ?? (app?.environment ?? environment) }}
      </el-descriptions-item>
      <el-descriptions-item label="状态">
        <StatusTag :status="app?.status ?? 'inactive'" />
      </el-descriptions-item>
      <template v-if="isTikTok">
        <el-descriptions-item label="App Key（App ID）">
          <span class="mono">{{ textOrDash(app?.app_key) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="App Secret">
          <el-tag :type="app?.has_app_secret ? 'success' : 'info'" size="small">
            {{ app?.has_app_secret ? '已配置（不回显）' : '未配置' }}
          </el-tag>
        </el-descriptions-item>
      </template>
      <template v-else>
        <el-descriptions-item label="Partner ID">{{ textOrDash(app?.partner_id) }}</el-descriptions-item>
        <el-descriptions-item label="Partner Key">
          <el-tag :type="app?.has_partner_key ? 'success' : 'info'" size="small">
            {{ app?.has_partner_key ? '已配置（不回显）' : '未配置' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="Partner Secret">
          <el-tag :type="app?.has_partner_secret ? 'success' : 'info'" size="small">
            {{ app?.has_partner_secret ? '已配置（不回显）' : '未配置' }}
          </el-tag>
        </el-descriptions-item>
      </template>
    </el-descriptions>

    <el-form ref="formRef" v-loading="loading" :model="channelForm" :rules="rules" label-width="130px" style="max-width: 720px">
      <template v-if="isTikTok">
        <el-form-item label="App Key（App ID）">
          <el-input
            v-model="channelForm.app_key"
            placeholder="填 TikTok Shop 开发者后台的 App Key（即 App ID）"
          />
          <div class="form-tip">TikTok Shop Open API 里叫 App Key，授权参数是 app_key；它就是你应用的 App ID。</div>
        </el-form-item>
        <el-form-item label="App Secret">
          <el-input v-model="channelForm.app_secret" type="password" show-password placeholder="不填写则保持原值" />
        </el-form-item>
      </template>
      <template v-else>
        <el-form-item label="Partner ID">
          <el-input v-model="channelForm.partner_id" placeholder="Shopee 平台应用 Partner ID" />
        </el-form-item>
        <el-form-item label="Partner Key">
          <el-input v-model="channelForm.partner_key" type="password" show-password placeholder="不填写则保持原值" />
        </el-form-item>
        <el-form-item label="Partner Secret">
          <el-input v-model="channelForm.partner_secret" type="password" show-password placeholder="不填写则保持原值" />
        </el-form-item>
      </template>
      <el-form-item label="回调地址" prop="redirect_url">
        <el-input v-model="channelForm.redirect_url" :placeholder="redirectPlaceholder" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        <span class="form-tip" style="margin-left: 8px">密钥加密保存，接口不回显明文。</span>
      </el-form-item>
    </el-form>

    <el-alert v-if="isTikTok" type="info" :closable="false" show-icon style="max-width: 720px">
      <p>把上面的回调地址加入 TikTok Shop 应用的白名单；保存 App Key（App ID）/ App Secret 后，租户即可在「店铺连接」发起 TikTok Shop 授权。</p>
    </el-alert>
  </el-card>
</template>
