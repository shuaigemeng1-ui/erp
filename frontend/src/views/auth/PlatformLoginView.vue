<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const formRef = ref<FormInstance>()
const loading = ref(false)
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
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
    const target = redirect.startsWith('/platform/') ? redirect : '/platform/tenants'
    await router.replace(target)
  } catch {
    // 统一错误提示由 HTTP 层处理（非平台管理员会被后端拒绝）
  } finally {
    loading.value = false
  }
}

function goTenantLogin(): void {
  void router.push('/login')
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card">
      <div class="auth-badge">平台管理</div>
      <h1 class="auth-title">ERP 平台控制台</h1>
      <p class="auth-subtitle">平台运营与租户管理（独立入口）</p>
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
        <el-button type="primary" :loading="loading" style="width: 100%" @click="submit">
          登录平台控制台
        </el-button>
      </el-form>
      <div class="auth-footer">
        <el-button text type="primary" @click="goTenantLogin">返回租户登录</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.auth-badge {
  display: inline-block;
  padding: 2px 10px;
  margin-bottom: 12px;
  border-radius: 10px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 12px;
  letter-spacing: 1px;
}
</style>
