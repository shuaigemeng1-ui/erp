<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { platformApi } from '@/api/platform'
import StatusTag from '@/components/StatusTag.vue'
import { environmentLabels } from '@/utils/dict'
import { textOrDash } from '@/utils/format'
import type { ChannelApp } from '@/types'

const channel = ref('shopee')
const environment = ref('production')
const loading = ref(false)
const saving = ref(false)
const app = ref<ChannelApp | null>(null)

const formRef = ref<FormInstance>()
const redirectPlaceholder = 'https://your-domain/api/v1/integrations/shopee/oauth/callback'
const channelForm = reactive({
  partner_id: '',
  partner_key: '',
  partner_secret: '',
  redirect_url: '',
})
const rules: FormRules = {
  redirect_url: [{ required: true, message: '请输入回调地址', trigger: 'blur' }],
  partner_id: [{ required: true, message: '请输入 Partner ID', trigger: 'blur' }],
}

async function load(): Promise<void> {
  loading.value = true
  try {
    app.value = await platformApi.channelApp(channel.value, environment.value)
    channelForm.partner_id = app.value.partner_id ?? ''
    channelForm.redirect_url = app.value.redirect_url ?? ''
    channelForm.partner_key = ''
    channelForm.partner_secret = ''
  } catch {
    app.value = null
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  saving.value = true
  try {
    const payload: {
      partner_id: string
      redirect_url: string
      partner_key?: string
      partner_secret?: string
    } = {
      partner_id: channelForm.partner_id.trim(),
      redirect_url: channelForm.redirect_url.trim(),
    }
    // 密钥不回显：只有填写时才提交
    if (channelForm.partner_key.trim()) payload.partner_key = channelForm.partner_key.trim()
    if (channelForm.partner_secret.trim()) payload.partner_secret = channelForm.partner_secret.trim()

    app.value = await platformApi.saveChannelApp(channel.value, environment.value, payload)
    channelForm.partner_key = ''
    channelForm.partner_secret = ''
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
      <el-select v-model="channel" style="width: 150px" @change="load">
        <el-option label="Shopee" value="shopee" />
      </el-select>
      <el-select v-model="environment" style="width: 160px" @change="load">
        <el-option label="生产环境" value="production" />
        <el-option label="测试环境" value="test" />
      </el-select>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-descriptions :column="3" border size="small" class="detail-descriptions">
      <el-descriptions-item label="渠道">{{ app?.channel ?? channel }}</el-descriptions-item>
      <el-descriptions-item label="环境">
        {{ environmentLabels[app?.environment ?? environment] ?? (app?.environment ?? environment) }}
      </el-descriptions-item>
      <el-descriptions-item label="状态">
        <StatusTag :status="app?.status ?? 'inactive'" />
      </el-descriptions-item>
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
      <el-descriptions-item label="Partner ID">{{ textOrDash(app?.partner_id) }}</el-descriptions-item>
    </el-descriptions>

    <el-form ref="formRef" v-loading="loading" :model="channelForm" :rules="rules" label-width="120px" style="max-width: 680px">
      <el-form-item label="Partner ID" prop="partner_id">
        <el-input v-model="channelForm.partner_id" placeholder="平台应用 Partner ID" />
      </el-form-item>
      <el-form-item label="Partner Key">
        <el-input v-model="channelForm.partner_key" type="password" show-password placeholder="不填写则保持原值" />
      </el-form-item>
      <el-form-item label="Partner Secret">
        <el-input v-model="channelForm.partner_secret" type="password" show-password placeholder="不填写则保持原值" />
      </el-form-item>
      <el-form-item label="回调地址" prop="redirect_url">
        <el-input v-model="channelForm.redirect_url" :placeholder="redirectPlaceholder" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
        <span class="form-tip" style="margin-left: 8px">密钥加密保存，接口不回显明文。</span>
      </el-form-item>
    </el-form>
  </el-card>
</template>
