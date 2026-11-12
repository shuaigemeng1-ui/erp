<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { refreshCsrfToken } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import {
  clearRememberedCredentials,
  loadRememberedCredentials,
  saveRememberedCredentials,
} from '@/utils/rememberedCredentials'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const formRef = ref<FormInstance>()
const loading = ref(false)
const remember = ref(false)
const form = reactive({
  email: '',
  password: '',
})

const rules: FormRules = {
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' },
  ],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

onMounted(() => {
  // 进入登录页强制获取新的匿名 CSRF，避免旧会话 Cookie 导致登录 403
  void refreshCsrfToken()
  void restoreRemembered()
})

async function restoreRemembered(): Promise<void> {
  const saved = await loadRememberedCredentials('platform')
  if (!saved) return
  form.email = saved.email
  form.password = saved.password
  remember.value = true
}

async function persistRemembered(): Promise<void> {
  if (remember.value) {
    await saveRememberedCredentials('platform', form.email.trim(), form.password)
  } else {
    clearRememberedCredentials('platform')
  }
}

function onRememberChange(value: string | number | boolean): void {
  if (!value) clearRememberedCredentials('platform')
}

async function submit(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await auth.login({ email: form.email.trim(), password: form.password }, 'platform')
    if (!auth.isPlatformAdmin) {
      await auth.logout()
      ElMessage.error('该账号不是平台管理员')
      return
    }
    await persistRemembered()
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    const target = redirect.startsWith('/platform/') ? redirect : '/platform/tenants'
    await router.replace(target)
  } catch {
    // 统一错误提示由 HTTP 层处理（非平台管理员会被后端拒绝）
  } finally {
    loading.value = false
  }
}

</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <h1 class="auth-title">ERP 平台控制台</h1>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
        <el-form-item label="邮箱" prop="email">
          <el-input
            v-model="form.email"
            placeholder="请输入平台管理员邮箱"
            autocomplete="username"
            clearable
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入登录密码"
            show-password
            autocomplete="current-password"
            @keyup.enter="submit"
          />
        </el-form-item>
        <div class="login-form-options">
          <el-checkbox v-model="remember" @change="onRememberChange">记住密码</el-checkbox>
        </div>
        <el-button type="primary" :loading="loading" style="width: 100%" @click="submit">
          登录平台控制台
        </el-button>
      </el-form>
    </div>
  </div>
</template>
