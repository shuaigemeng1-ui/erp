<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import { useEnterSession } from '@/composables/useEnterSession'
import { formatDateTime } from '@/utils/format'
import type { InvitationInfo } from '@/types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { enterApp } = useEnterSession()

const token = String(route.params.token ?? '')
const loading = ref(true)
const submitting = ref(false)
const invitation = ref<InvitationInfo | null>(null)
const loadError = ref('')

const formRef = ref<FormInstance>()
const form = reactive({
  display_name: '',
  password: '',
  confirm_password: '',
})
const rules: FormRules = {
  display_name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  password: [
    { required: true, message: '请设置密码', trigger: 'blur' },
    { min: 8, message: '密码至少 8 位', trigger: 'blur' },
  ],
  confirm_password: [{ required: true, message: '请再次输入密码', trigger: 'blur' }],
}

const pending = ref(false)

onMounted(async () => {
  try {
    const info = await authApi.getInvitation(token)
    invitation.value = info
    pending.value = info.status === 'pending'
  } catch (cause) {
    loadError.value = cause instanceof Error ? cause.message : '邀请信息加载失败'
  } finally {
    loading.value = false
  }
})

async function submit(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return
  if (form.password !== form.confirm_password) {
    ElMessage.warning('两次输入的密码不一致')
    return
  }
  submitting.value = true
  try {
    await auth.acceptInvitation({
      token,
      password: form.password,
      display_name: form.display_name.trim(),
    })
    await enterApp()
  } catch {
    // 错误提示由 HTTP 层统一处理
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <h1 class="auth-title">激活账号</h1>
      <p class="auth-subtitle">设置登录密码后加入租户</p>

      <el-skeleton v-if="loading" :rows="4" animated />

      <el-alert v-else-if="loadError" :title="loadError" type="error" :closable="false" show-icon />

      <template v-else-if="invitation">
        <el-descriptions :column="1" border size="small" class="detail-descriptions">
          <el-descriptions-item label="租户">{{ invitation.tenant_name }}</el-descriptions-item>
          <el-descriptions-item label="邮箱">{{ invitation.email }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ invitation.status }}</el-descriptions-item>
          <el-descriptions-item label="有效期至">{{ formatDateTime(invitation.expires_at) }}</el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="!pending"
          title="该邀请已失效、已使用或被撤销，请联系租户管理员重新邀请。"
          type="warning"
          :closable="false"
          show-icon
          style="margin-bottom: 12px"
        />

        <el-form v-else ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
          <el-form-item label="姓名" prop="display_name">
            <el-input v-model="form.display_name" placeholder="请输入姓名" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input v-model="form.password" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-form-item label="确认密码" prop="confirm_password">
            <el-input v-model="form.confirm_password" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-button type="primary" :loading="submitting" style="width: 100%" @click="submit">激活并登录</el-button>
        </el-form>
      </template>

      <div class="auth-footer">
        <el-button text type="primary" @click="router.replace('/login')">返回登录</el-button>
      </div>
    </div>
  </div>
</template>
