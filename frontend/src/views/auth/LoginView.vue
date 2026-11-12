<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { FormInstance, FormRules } from 'element-plus'
import { refreshCsrfToken } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { useEnterSession } from '@/composables/useEnterSession'
import {
  clearRememberedCredentials,
  loadRememberedCredentials,
  saveRememberedCredentials,
} from '@/utils/rememberedCredentials'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const { enterApp } = useEnterSession()

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
  const saved = await loadRememberedCredentials('tenant')
  if (!saved) return
  form.email = saved.email
  form.password = saved.password
  remember.value = true
}

async function persistRemembered(): Promise<void> {
  if (remember.value) {
    await saveRememberedCredentials('tenant', form.email.trim(), form.password)
  } else {
    clearRememberedCredentials('tenant')
  }
}

function onRememberChange(value: string | number | boolean): void {
  if (!value) clearRememberedCredentials('tenant')
}

async function submit(): Promise<void> {
  const formInstance = formRef.value
  if (!formInstance) return
  const valid = await formInstance.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await auth.login({ email: form.email.trim(), password: form.password }, 'tenant')
    await persistRemembered()
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    // 租户登录不能跳到平台管理页面；平台入口需要单独登录
    if (redirect && !redirect.startsWith('/platform')) {
      await router.replace(redirect)
      return
    }
    await enterApp()
  } catch {
    // 统一错误提示由 HTTP 层处理
  } finally {
    loading.value = false
  }
}

</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <h1 class="auth-title">ERP 登录</h1>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
        <el-form-item label="邮箱" prop="email">
          <el-input
            v-model="form.email"
            placeholder="请输入登录邮箱"
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
        <el-button type="primary" :loading="loading" style="width: 100%" @click="submit">登录</el-button>
      </el-form>
    </div>
  </div>
</template>


