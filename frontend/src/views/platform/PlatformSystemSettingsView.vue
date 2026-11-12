<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { platformApi } from '@/api/platform'
import { isApiError } from '@/api/http'
import type { PlatformSystemSettings, StorageProfile, StorageProfilePayload, StorageProvider } from '@/types/storage'

const settings = ref<PlatformSystemSettings | null>(null)
const loading = ref(false)
const saving = ref(false)
const dialogVisible = ref(false)
const conflict = ref(false)
const actionBusy = ref('')
const editing = ref<StorageProfile | null>(null)
const formRef = ref<FormInstance>()
const fallbackProviders = [
  { code: 'oss' as const, name: '阿里云 OSS' },
  { code: 'cos' as const, name: '腾讯云 COS' },
  { code: 'obs' as const, name: '华为云 OBS' },
  { code: 's3' as const, name: 'AWS S3' },
  { code: 's3_compatible' as const, name: 'S3 兼容服务' },
]
const providers = computed(() => settings.value?.providers.length ? settings.value.providers : fallbackProviders)
const profiles = computed(() => settings.value?.profiles ?? [])
const defaultProfile = computed(() => profiles.value.find((p) => p.id === settings.value?.default_storage_profile_id))
const storageForm = ref<StorageProfilePayload>({
  name: '', provider: 'oss', endpoint: '', bucket: '', region: '', public_base_url: '',
  key_prefix: '', addressing_style: 'auto', enabled: true, access_key_id: '', secret_access_key: '',
})
const providerChanged = computed(() => Boolean(editing.value && editing.value.provider !== storageForm.value.provider))
const credentialsRequired = computed(() => !editing.value || providerChanged.value)
const isDefault = (profile: StorageProfile): boolean => profile.id === settings.value?.default_storage_profile_id
const editingDefault = computed(() => Boolean(editing.value && isDefault(editing.value)))
const keyLabels = computed(() => storageForm.value.provider === 'cos' ? ['SecretId', 'SecretKey']
  : storageForm.value.provider === 'obs' ? ['Access Key（AK）', 'Secret Key（SK）']
    : storageForm.value.provider === 'oss' ? ['AccessKey ID', 'AccessKey Secret'] : ['Access Key ID', 'Secret Access Key'])
const endpointPlaceholder = computed(() => ({
  oss: 'https://s3.oss-cn-hangzhou.aliyuncs.com',
  cos: 'https://cos.ap-guangzhou.myqcloud.com',
  obs: 'https://obs.cn-north-4.myhuaweicloud.com',
  s3: 'https://s3.us-east-1.amazonaws.com',
  s3_compatible: 'https://对象存储服务地址',
})[storageForm.value.provider])

function validateHttps(_rule: unknown, value: string, callback: (error?: Error) => void): void {
  try {
    const url = new URL(value)
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) throw new Error()
    callback()
  } catch { callback(new Error('请输入不含账号、查询参数或片段的 HTTPS 地址')) }
}
const rules: FormRules = {
  name: [{ required: true, message: '请输入配置名称', trigger: 'blur' }],
  provider: [{ required: true, message: '请选择存储厂商', trigger: 'change' }],
  endpoint: [{ required: true, message: '请输入 Endpoint', trigger: 'blur' }, { validator: validateHttps, trigger: 'blur' }],
  bucket: [{ required: true, message: '请输入 Bucket', trigger: 'blur' }],
  public_base_url: [{ required: true, message: '请输入图片访问域名', trigger: 'blur' }, { validator: validateHttps, trigger: 'blur' }],
}

function providerName(code: StorageProvider): string {
  return providers.value.find((p) => p.code === code)?.name ?? code
}

async function load(): Promise<void> {
  loading.value = true
  try { settings.value = await platformApi.systemSettings() }
  finally { loading.value = false }
}

function openEditor(profile?: StorageProfile): void {
  editing.value = profile ? { ...profile } : null
  Object.assign(storageForm.value, {
    name: profile?.name ?? '', provider: profile?.provider ?? 'oss', endpoint: profile?.endpoint ?? '',
    bucket: profile?.bucket ?? '', region: profile?.region ?? '', public_base_url: profile?.public_base_url ?? '',
    key_prefix: profile?.key_prefix ?? '', addressing_style: profile?.addressing_style ?? 'auto',
    enabled: profile?.enabled ?? true, access_key_id: '', secret_access_key: '', version: profile?.version,
  })
  conflict.value = false
  dialogVisible.value = true
  formRef.value?.clearValidate()
}

function clearCredentials(): void {
  storageForm.value.access_key_id = ''
  storageForm.value.secret_access_key = ''
  formRef.value?.clearValidate()
}

async function reloadEditor(): Promise<void> {
  try { await ElMessageBox.confirm('放弃当前未保存的输入，重新加载最新配置？', '重新加载配置', { type: 'warning' }) }
  catch { return }
  const id = editing.value?.id
  await load()
  const current = profiles.value.find((p) => p.id === id)
  if (current) openEditor(current)
  else dialogVisible.value = false
}

function noteConflict(error: unknown, inEditor = false): void {
  if (isApiError(error) && error.status === 409) {
    if (inEditor) conflict.value = true
    ElMessage.warning('配置已被其他人修改，请刷新后重新操作')
  }
}

async function save(): Promise<void> {
  if (saving.value || conflict.value) return
  if (!await formRef.value?.validate().catch(() => false)) return
  if ((credentialsRequired.value || !editing.value?.has_access_key) && !storageForm.value.access_key_id?.trim()) {
    ElMessage.warning(`请填写 ${keyLabels.value[0]}`); return
  }
  if ((credentialsRequired.value || !editing.value?.has_secret_key) && !storageForm.value.secret_access_key?.trim()) {
    ElMessage.warning(`请填写 ${keyLabels.value[1]}`); return
  }
  saving.value = true
  try {
    const payload: StorageProfilePayload = {
      name: storageForm.value.name.trim(), provider: storageForm.value.provider, endpoint: storageForm.value.endpoint.trim(), bucket: storageForm.value.bucket.trim(),
      region: storageForm.value.region.trim(), public_base_url: storageForm.value.public_base_url.trim(), key_prefix: storageForm.value.key_prefix.trim(),
      addressing_style: storageForm.value.addressing_style, enabled: storageForm.value.enabled,
    }
    if (storageForm.value.access_key_id?.trim()) payload.access_key_id = storageForm.value.access_key_id.trim()
    if (storageForm.value.secret_access_key?.trim()) payload.secret_access_key = storageForm.value.secret_access_key.trim()
    if (editing.value) {
      payload.version = editing.value.version
      await platformApi.updateStorageProfile(editing.value.id, payload)
    } else { await platformApi.createStorageProfile(payload) }
    dialogVisible.value = false
    clearCredentials()
    ElMessage.success('存储配置已保存')
    await load()
  } catch (error) { noteConflict(error, true) }
  finally { saving.value = false }
}

async function testConnection(profile: StorageProfile): Promise<void> {
  if (actionBusy.value) return
  actionBusy.value = `test:${profile.id}`
  try {
    const result = await platformApi.testStorageProfile(profile.id)
    if (result.ok) ElMessage.success(result.message || '存储桶连接正常')
    else ElMessage.error(result.message || '连接测试失败')
  } finally { actionBusy.value = '' }
}

async function setDefault(profile: StorageProfile): Promise<void> {
  if (actionBusy.value || !settings.value || !profile.enabled) return
  actionBusy.value = `default:${profile.id}`
  try {
    settings.value = await platformApi.setDefaultStorage(profile.id, settings.value.version)
    ElMessage.success(`新上传文件将使用 ${profile.name}`)
  } catch (error) { noteConflict(error) }
  finally { actionBusy.value = '' }
}

async function toggle(profile: StorageProfile): Promise<void> {
  if (actionBusy.value || isDefault(profile)) return
  actionBusy.value = `toggle:${profile.id}`
  try {
    await platformApi.updateStorageProfile(profile.id, {
      name: profile.name, provider: profile.provider, endpoint: profile.endpoint, bucket: profile.bucket,
      region: profile.region, public_base_url: profile.public_base_url, key_prefix: profile.key_prefix,
      addressing_style: profile.addressing_style, enabled: !profile.enabled, version: profile.version,
    })
    ElMessage.success(profile.enabled ? '存储配置已停用' : '存储配置已启用')
    await load()
  } catch (error) { noteConflict(error) }
  finally { actionBusy.value = '' }
}

onMounted(load)
</script>

<template>
  <el-card shadow="never" class="page-card">
    <div class="filter-bar storage-toolbar">
      <strong>对象存储</strong>
      <el-tag :type="defaultProfile ? 'success' : 'warning'">{{ defaultProfile ? `默认：${defaultProfile.name}` : '未设置默认存储' }}</el-tag>
      <div class="spacer" />
      <el-button :icon="Refresh" :disabled="Boolean(actionBusy)" @click="load">刷新</el-button>
      <el-button type="primary" :icon="Plus" :disabled="loading || Boolean(actionBusy)" @click="openEditor()">新增存储配置</el-button>
    </div>
    <el-table v-loading="loading" :data="profiles" border row-key="id">
      <el-table-column label="配置名称" min-width="150"><template #default="{ row }"><span>{{ row.name }}</span><el-tag v-if="isDefault(row as StorageProfile)" size="small" class="default-tag">默认</el-tag></template></el-table-column>
      <el-table-column label="厂商" width="140"><template #default="{ row }">{{ providerName(row.provider) }}</template></el-table-column>
      <el-table-column prop="bucket" label="Bucket" min-width="160" show-overflow-tooltip />
      <el-table-column prop="endpoint" label="Endpoint" min-width="230" show-overflow-tooltip />
      <el-table-column prop="public_base_url" label="访问域名" min-width="210" show-overflow-tooltip />
      <el-table-column label="凭据" width="100"><template #default="{ row }"><el-tag size="small" :type="row.has_access_key && row.has_secret_key ? 'success' : 'warning'">{{ row.has_access_key && row.has_secret_key ? '已配置' : '未完整配置' }}</el-tag></template></el-table-column>
      <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="315" fixed="right"><template #default="{ row }">
        <el-button text type="primary" :disabled="Boolean(actionBusy)" @click="openEditor(row as StorageProfile)">编辑</el-button>
        <el-button text :loading="actionBusy === `test:${row.id}`" :disabled="Boolean(actionBusy) && actionBusy !== `test:${row.id}`" @click="testConnection(row as StorageProfile)">测试连接</el-button>
        <el-button v-if="!isDefault(row as StorageProfile)" text type="primary" :loading="actionBusy === `default:${row.id}`" :disabled="!row.enabled || (Boolean(actionBusy) && actionBusy !== `default:${row.id}`)" @click="setDefault(row as StorageProfile)">设为默认</el-button>
        <el-button v-if="!isDefault(row as StorageProfile)" text :type="row.enabled ? 'danger' : 'success'" :loading="actionBusy === `toggle:${row.id}`" :disabled="Boolean(actionBusy) && actionBusy !== `toggle:${row.id}`" @click="toggle(row as StorageProfile)">{{ row.enabled ? '停用' : '启用' }}</el-button>
      </template></el-table-column>
      <template #empty><el-empty description="尚未添加对象存储配置" :image-size="72" /></template>
    </el-table>
  </el-card>

  <el-dialog v-model="dialogVisible" :title="editing ? '编辑存储配置' : '新增存储配置'" width="min(780px, 96vw)" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving" @closed="clearCredentials">
    <el-alert v-if="conflict" title="配置已更新，请重新加载后编辑。当前输入尚未丢弃。" type="warning" :closable="false" class="conflict-alert" />
    <el-form ref="formRef" :model="storageForm" :rules="rules" label-width="130px" :disabled="saving">
      <el-row :gutter="18">
        <el-col :xs="24" :md="12"><el-form-item label="配置名称" prop="name"><el-input v-model="storageForm.name" maxlength="100" /></el-form-item></el-col>
        <el-col :xs="24" :md="12"><el-form-item label="云厂商" prop="provider"><el-select v-model="storageForm.provider" @change="clearCredentials"><el-option v-for="item in providers" :key="item.code" :label="item.name" :value="item.code" /></el-select></el-form-item></el-col>
        <el-col :span="24"><el-form-item label="Endpoint" prop="endpoint"><el-input v-model="storageForm.endpoint" :placeholder="endpointPlaceholder" maxlength="500" /></el-form-item></el-col>
        <el-col :xs="24" :md="12"><el-form-item label="Bucket" prop="bucket"><el-input v-model="storageForm.bucket" maxlength="255" /></el-form-item></el-col>
        <el-col :xs="24" :md="12"><el-form-item label="Region"><el-input v-model="storageForm.region" maxlength="100" placeholder="存储桶所在地域" /></el-form-item></el-col>
        <el-col :span="24"><el-form-item label="访问域名" prop="public_base_url"><el-input v-model="storageForm.public_base_url" maxlength="1000" placeholder="https://images.example.com" /><div class="form-tip">填写可长期访问图片的桶域名或 CDN 地址，并在云厂商配置相应读权限。</div></el-form-item></el-col>
        <el-col :xs="24" :md="12"><el-form-item label="存储目录前缀"><el-input v-model="storageForm.key_prefix" maxlength="200" placeholder="如 erp（可留空）" /></el-form-item></el-col>
        <el-col :xs="24" :md="12"><el-form-item label="寻址方式"><el-select v-model="storageForm.addressing_style"><el-option label="自动" value="auto" /><el-option label="虚拟主机（桶名在子域名）" value="virtual" /><el-option label="路径（桶名在路径）" value="path" /></el-select></el-form-item></el-col>
        <el-col :xs="24" :md="12"><el-form-item :label="keyLabels[0]" :required="credentialsRequired"><el-input v-model="storageForm.access_key_id" type="password" show-password autocomplete="new-password" :placeholder="editing?.has_access_key && !providerChanged ? '已配置，留空保持原值' : '请输入访问密钥 ID'" /></el-form-item></el-col>
        <el-col :xs="24" :md="12"><el-form-item :label="keyLabels[1]" :required="credentialsRequired"><el-input v-model="storageForm.secret_access_key" type="password" show-password autocomplete="new-password" :placeholder="editing?.has_secret_key && !providerChanged ? '已配置，留空保持原值' : '请输入访问密钥'" /></el-form-item></el-col>
        <el-col :span="24"><el-form-item label="启用"><el-switch v-model="storageForm.enabled" :disabled="editingDefault" /><span v-if="editingDefault" class="form-tip switch-tip">先切换默认存储后才能停用。</span></el-form-item></el-col>
      </el-row>
    </el-form>
    <template #footer>
      <el-button :disabled="saving" @click="dialogVisible = false">取消</el-button>
      <el-button v-if="conflict" :disabled="saving" @click="reloadEditor">重新加载</el-button>
      <el-button type="primary" :loading="saving" :disabled="conflict" @click="save">保存配置</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.storage-toolbar { margin-bottom:16px; gap:12px; }
.spacer { flex:1; }
.default-tag { margin-left:8px; }
.conflict-alert { margin-bottom:16px; }
.switch-tip { margin-left:10px; }
:deep(.el-select) { width:100%; }
</style>
