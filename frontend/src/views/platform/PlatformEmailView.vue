<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { platformApi } from '@/api/platform'
import type { EmailProfile, EmailProfilePayload, EmailProvider, EmailProviderMeta } from '@/types'

const loading = ref(false)
const profiles = ref<EmailProfile[]>([])
const providers = ref<EmailProviderMeta[]>([])
const saving = ref(false)
const actionBusy = ref('')

const dialogVisible = ref(false)
const editing = ref<EmailProfile | null>(null)
const formRef = ref<FormInstance>()
const form = reactive({
  name: '',
  provider: 'aliyun_directmail' as EmailProvider,
  region: '',
  from_address: '',
  from_name: '',
  enabled: true,
  access_key_id: '',
  secret_access_key: '',
})

const testVisible = ref(false)
const testTarget = ref<EmailProfile | null>(null)
const testTo = ref('')
const testSubject = ref('ERP 邮件服务测试')
const testLoading = ref(false)
const testResult = ref<{ ok: boolean; message: string; request_id?: string } | null>(null)

const providerOptions = computed<EmailProviderMeta[]>(() => {
  if (providers.value.length) return providers.value
  return [
    {
      code: 'aliyun_directmail',
      name: '阿里云 DirectMail',
      default_region: 'cn-hangzhou',
      region_label: '地域 ID，默认 cn-hangzhou',
    },
    {
      code: 'tencent_ses',
      name: '腾讯云 SES',
      default_region: 'ap-guangzhou',
      region_label: '地域，默认 ap-guangzhou',
    },
  ]
})

const currentProvider = computed(
  () => providerOptions.value.find((item) => item.code === form.provider) ?? providerOptions.value[0],
)
const providerChanged = computed(
  () => Boolean(editing.value && editing.value.provider !== form.provider),
)
const credentialsRequired = computed(() => !editing.value || providerChanged.value)
const keyLabels = computed(() =>
  form.provider === 'tencent_ses'
    ? ['SecretId', 'SecretKey']
    : ['AccessKey ID', 'AccessKey Secret'],
)
const defaultProfile = computed(() => profiles.value.find((item) => item.is_default) ?? null)

const rules: FormRules = {
  name: [{ required: true, message: '请输入配置名称', trigger: 'blur' }],
  from_address: [
    { required: true, message: '请输入发信地址', trigger: 'blur' },
    { type: 'email', message: '发信地址格式不正确', trigger: 'blur' },
  ],
}

function providerName(code: string): string {
  return providerOptions.value.find((item) => item.code === code)?.name ?? code
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const data = await platformApi.emailProfiles()
    profiles.value = data.profiles
    providers.value = data.providers
  } finally {
    loading.value = false
  }
}

function onProviderChange(value: EmailProvider): void {
  const meta = providerOptions.value.find((item) => item.code === value)
  if (meta && (!form.region || form.region === currentProvider.value?.default_region)) {
    form.region = meta.default_region
  }
  form.access_key_id = ''
  form.secret_access_key = ''
}

function openEditor(profile?: EmailProfile): void {
  editing.value = profile ? { ...profile } : null
  const provider = profile?.provider ?? 'aliyun_directmail'
  const meta = providerOptions.value.find((item) => item.code === provider)
  Object.assign(form, {
    name: profile?.name ?? '',
    provider,
    region: profile?.region ?? meta?.default_region ?? '',
    from_address: profile?.from_address ?? '',
    from_name: profile?.from_name ?? '',
    enabled: profile?.enabled ?? true,
    access_key_id: '',
    secret_access_key: '',
  })
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

async function save(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  if (credentialsRequired.value && !form.access_key_id.trim()) {
    ElMessage.warning(`请填写 ${keyLabels.value[0]}`)
    return
  }
  if (credentialsRequired.value && !form.secret_access_key.trim()) {
    ElMessage.warning(`请填写 ${keyLabels.value[1]}`)
    return
  }
  const payload: EmailProfilePayload = {
    name: form.name.trim(),
    provider: form.provider,
    region: form.region.trim(),
    from_address: form.from_address.trim(),
    from_name: form.from_name.trim(),
    enabled: form.enabled,
  }
  if (form.access_key_id.trim()) payload.access_key_id = form.access_key_id.trim()
  if (form.secret_access_key.trim()) payload.secret_access_key = form.secret_access_key.trim()
  saving.value = true
  try {
    if (editing.value) {
      payload.version = editing.value.version
      await platformApi.updateEmailProfile(editing.value.id, payload)
      ElMessage.success('邮件配置已更新')
    } else {
      await platformApi.createEmailProfile(payload)
      ElMessage.success('邮件配置已创建')
    }
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function setDefault(profile: EmailProfile): Promise<void> {
  if (actionBusy.value || profile.is_default) return
  actionBusy.value = `default:${profile.id}`
  try {
    await platformApi.setDefaultEmailProfile(profile.id)
    ElMessage.success(`已将 ${profile.name} 设为默认邮件账号`)
    await load()
  } finally {
    actionBusy.value = ''
  }
}

async function toggle(profile: EmailProfile): Promise<void> {
  if (actionBusy.value) return
  actionBusy.value = `toggle:${profile.id}`
  try {
    await platformApi.updateEmailProfile(profile.id, {
      name: profile.name,
      provider: profile.provider,
      region: profile.region,
      from_address: profile.from_address,
      from_name: profile.from_name,
      enabled: !profile.enabled,
      version: profile.version,
    })
    ElMessage.success(profile.enabled ? '邮件配置已停用' : '邮件配置已启用')
    await load()
  } finally {
    actionBusy.value = ''
  }
}

function openTest(profile: EmailProfile): void {
  testTarget.value = profile
  testTo.value = ''
  testSubject.value = 'ERP 邮件服务测试'
  testResult.value = null
  testVisible.value = true
}

async function sendTest(): Promise<void> {
  if (!testTarget.value) return
  if (!testTo.value.trim()) {
    ElMessage.warning('请输入测试收件邮箱')
    return
  }
  testLoading.value = true
  testResult.value = null
  try {
    const result = await platformApi.testEmailProfile(
      testTarget.value.id,
      testTo.value.trim(),
      testSubject.value.trim() || undefined,
    )
    testResult.value = result
    ElMessage.success(result.message || '测试邮件已提交')
  } finally {
    testLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar storage-toolbar">
      <strong>邮件服务</strong>
      <el-tag :type="defaultProfile ? 'success' : 'warning'">
        {{ defaultProfile ? `默认：${defaultProfile.name}` : '未设置默认邮件账号' }}
      </el-tag>
      <div class="spacer" />
      <el-button :icon="Refresh" :disabled="Boolean(actionBusy)" @click="load">刷新</el-button>
      <el-button type="primary" :icon="Plus" :disabled="loading" @click="openEditor()">
        新增邮件配置
      </el-button>
    </div>

    <el-table v-loading="loading" :data="profiles" border row-key="id">
      <el-table-column label="配置名称" min-width="150">
        <template #default="{ row }">
          <span>{{ row.name }}</span>
          <el-tag v-if="row.is_default" size="small" class="default-tag">默认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="服务商" width="160">
        <template #default="{ row }">{{ providerName(row.provider) }}</template>
      </el-table-column>
      <el-table-column prop="from_address" label="发信地址" min-width="210" show-overflow-tooltip />
      <el-table-column prop="region" label="区域" width="140" />
      <el-table-column label="密钥" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="row.has_access_key && row.has_secret_key ? 'success' : 'warning'">
            {{ row.has_access_key && row.has_secret_key ? '已配置' : '未配置' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">
            {{ row.enabled ? '启用' : '停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="330" fixed="right">
        <template #default="{ row }">
          <el-button text type="primary" @click="openEditor(row as EmailProfile)">编辑</el-button>
          <el-button text :disabled="Boolean(actionBusy)" @click="openTest(row as EmailProfile)">
            测试发送
          </el-button>
          <el-button
            v-if="!row.is_default"
            text
            type="primary"
            :loading="actionBusy === `default:${row.id}`"
            :disabled="!row.enabled || Boolean(actionBusy)"
            @click="setDefault(row as EmailProfile)"
          >
            设为默认
          </el-button>
          <el-button
            v-if="!row.is_default"
            text
            :type="row.enabled ? 'danger' : 'success'"
            :loading="actionBusy === `toggle:${row.id}`"
            :disabled="Boolean(actionBusy)"
            @click="toggle(row as EmailProfile)"
          >
            {{ row.enabled ? '停用' : '启用' }}
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="尚未添加邮件服务配置" :image-size="72" />
      </template>
    </el-table>

    <el-alert type="info" :closable="false" show-icon class="email-tip">
      <p>支持阿里云 DirectMail 与腾讯云 SES。AccessKey/Secret 加密保存，接口不回显明文。</p>
      <p>保存后请点「测试发送」，确认厂商侧发信地址、模板权限和收件人白名单配置正确。</p>
    </el-alert>
  </el-card>

  <el-dialog
    v-model="dialogVisible"
    :title="editing ? '编辑邮件配置' : '新增邮件配置'"
    width="min(700px, 96vw)"
    :close-on-click-modal="false"
  >
    <el-form ref="formRef" :model="form" :rules="rules" label-width="120px" :disabled="saving">
      <el-form-item label="配置名称" prop="name">
        <el-input v-model="form.name" maxlength="100" placeholder="例如：平台通知邮箱" />
      </el-form-item>
      <el-form-item label="服务商" required>
        <el-select v-model="form.provider" style="width: 100%" @change="onProviderChange">
          <el-option
            v-for="item in providerOptions"
            :key="item.code"
            :label="item.name"
            :value="item.code"
          />
        </el-select>
      </el-form-item>
      <el-row :gutter="16">
        <el-col :xs="24" :md="12">
          <el-form-item label="发信地址" prop="from_address">
            <el-input v-model="form.from_address" placeholder="noreply@example.com" />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :md="12">
          <el-form-item label="发信人名称">
            <el-input v-model="form.from_name" maxlength="100" placeholder="ERP 系统通知" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="区域">
        <el-input v-model="form.region" :placeholder="currentProvider?.region_label" />
        <div class="form-tip">留空使用默认区域：{{ currentProvider?.default_region }}</div>
      </el-form-item>
      <el-row :gutter="16">
        <el-col :xs="24" :md="12">
          <el-form-item :label="keyLabels[0]" :required="credentialsRequired">
            <el-input
              v-model="form.access_key_id"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="editing?.has_access_key && !providerChanged ? '已配置，留空保持原值' : `请输入 ${keyLabels[0]}`"
            />
          </el-form-item>
        </el-col>
        <el-col :xs="24" :md="12">
          <el-form-item :label="keyLabels[1]" :required="credentialsRequired">
            <el-input
              v-model="form.secret_access_key"
              type="password"
              show-password
              autocomplete="new-password"
              :placeholder="editing?.has_secret_key && !providerChanged ? '已配置，留空保持原值' : `请输入 ${keyLabels[1]}`"
            />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="启用">
        <el-switch v-model="form.enabled" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button :disabled="saving" @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="testVisible" title="发送测试邮件" width="520px" destroy-on-close>
    <el-descriptions v-if="testTarget" :column="2" border size="small">
      <el-descriptions-item label="配置">{{ testTarget.name }}</el-descriptions-item>
      <el-descriptions-item label="服务商">{{ providerName(testTarget.provider) }}</el-descriptions-item>
      <el-descriptions-item label="发信地址" :span="2">{{ testTarget.from_address }}</el-descriptions-item>
    </el-descriptions>
    <el-form label-width="90px" class="test-form">
      <el-form-item label="收件邮箱" required>
        <el-input v-model="testTo" placeholder="接收测试邮件的邮箱" />
      </el-form-item>
      <el-form-item label="邮件主题">
        <el-input v-model="testSubject" maxlength="200" />
      </el-form-item>
    </el-form>
    <el-alert
      v-if="testResult"
      :type="testResult.ok ? 'success' : 'warning'"
      :closable="false"
      show-icon
    >
      <p>{{ testResult.message }}</p>
      <p v-if="testResult.request_id" class="form-tip">RequestId: {{ testResult.request_id }}</p>
    </el-alert>
    <template #footer>
      <el-button @click="testVisible = false">关闭</el-button>
      <el-button type="primary" :loading="testLoading" @click="sendTest">发送测试</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.storage-toolbar {
  margin-bottom: 16px;
  gap: 12px;
}

.spacer {
  flex: 1;
}

.default-tag {
  margin-left: 8px;
}

.email-tip {
  margin-top: 16px;
}

.test-form {
  margin-top: 16px;
}

:deep(.el-select) {
  width: 100%;
}
</style>
