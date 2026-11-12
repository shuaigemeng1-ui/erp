<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { ArrowDown, Expand, Fold, OfficeBuilding, UserFilled } from '@element-plus/icons-vue'
import { authApi } from '@/api/auth'
import { platformMenuItems, tenantMenuItems } from '@/router/menu'
import { useAuthStore } from '@/stores/auth'
import { useTenantStore } from '@/stores/tenant'

const auth = useAuthStore()
const tenant = useTenantStore()
const route = useRoute()
const router = useRouter()

const collapsed = ref(false)
const isPlatformArea = computed(() => route.meta.platform === true)
const visibleTenantMenu = computed(() =>
  tenantMenuItems.filter((item) => (!item.permission || tenant.has(item.permission)) && (!item.permissions || tenant.hasAny(item.permissions))),
)
const visiblePlatformMenu = computed(() => (auth.isPlatformAdmin ? platformMenuItems : []))
const activeMenu = computed(() => route.path)
const pageTitle = computed(() => route.meta.title ?? '')

/** 租户切换/重新加载权限后通过 key 强制重建页面：清空旧数据与勾选。 */
const viewKey = computed(() => `${tenant.activeTenantId || 'platform'}-${tenant.epoch}-${route.fullPath}`)

const switchableTenants = computed(() =>
  auth.tenants.filter((item) => item.id !== tenant.activeTenantId && item.status === 'active'),
)

async function handleSwitchTenant(command: string | number | object): Promise<void> {
  const tenantId = String(command)
  if (tenantId === '__tenant_select__') {
    await router.push('/tenant-select')
    return
  }
  const success = await tenant.switchTenant(tenantId)
  if (!success) {
    ElMessage.error(tenant.error || '切换租户失败')
    return
  }
  ElMessage.success(`已切换到 ${tenant.activeTenant?.name ?? tenantId}`)
  await router.replace('/')
}

async function handleUserCommand(command: string | number | object): Promise<void> {
  const action = String(command)
  if (action === 'password') {
    passwordVisible.value = true
    return
  }
  if (action === 'logout') {
    try {
      await ElMessageBox.confirm('确认退出登录？', '提示', { type: 'warning' })
    } catch {
      return
    }
    const wasPlatform = auth.isPlatformMode
    await auth.logout()
    tenant.resetForLogout()
    await router.replace({ name: wasPlatform ? 'platform-login' : 'login' })
  }
}

/* ------------------------------ 修改密码 ------------------------------ */
const passwordVisible = ref(false)
const passwordSubmitting = ref(false)
const passwordFormRef = ref<FormInstance>()
const passwordForm = reactive({
  current_password: '',
  new_password: '',
  confirm_password: '',
})
const passwordRules: FormRules = {
  current_password: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 8, message: '新密码至少 8 位', trigger: 'blur' },
  ],
  confirm_password: [{ required: true, message: '请再次输入新密码', trigger: 'blur' }],
}

async function submitPassword(): Promise<void> {
  const form = passwordFormRef.value
  if (!form) return
  const valid = await form.validate().catch(() => false)
  if (!valid) return
  if (passwordForm.new_password !== passwordForm.confirm_password) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  passwordSubmitting.value = true
  try {
    await authApi.changePassword({
      current_password: passwordForm.current_password,
      new_password: passwordForm.new_password,
    })
    ElMessage.success('密码已修改')
    passwordVisible.value = false
    passwordForm.current_password = ''
    passwordForm.new_password = ''
    passwordForm.confirm_password = ''
  } finally {
    passwordSubmitting.value = false
  }
}
</script>

<template>
  <el-container class="app-layout">
    <el-aside :width="collapsed ? '64px' : '224px'" class="app-aside">
      <div class="app-brand">
        <span v-if="!collapsed">ERP</span>
        <span v-else>ERP</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        router
        class="app-menu"
      >
        <template v-if="!isPlatformArea">
          <el-menu-item v-for="item in visibleTenantMenu" :key="item.path" :index="item.path">
            <el-icon><component :is="item.icon" /></el-icon>
            <template #title>{{ item.title }}</template>
          </el-menu-item>
        </template>
        <template v-else>
          <el-menu-item v-for="item in visiblePlatformMenu" :key="item.path" :index="item.path">
            <el-icon><component :is="item.icon" /></el-icon>
            <template #title>{{ item.title }}</template>
          </el-menu-item>
        </template>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="app-header" height="56px">
        <div class="header-left">
          <el-button text :icon="collapsed ? Expand : Fold" @click="collapsed = !collapsed" />
          <span class="header-title">{{ pageTitle }}</span>
        </div>
        <div class="header-right">
          <template v-if="!isPlatformArea && tenant.activeTenant">
            <el-dropdown trigger="click" @command="handleSwitchTenant">
              <el-button text>
                <el-icon><OfficeBuilding /></el-icon>
                <span class="tenant-name">{{ tenant.activeTenant.name }}</span>
                <el-icon><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item disabled>当前租户：{{ tenant.activeTenant.code }}</el-dropdown-item>
                  <el-dropdown-item
                    v-for="item in switchableTenants"
                    :key="item.id"
                    :command="item.id"
                    divided
                  >
                    {{ item.name }}（{{ item.code }}）
                  </el-dropdown-item>
                  <el-dropdown-item command="__tenant_select__">去租户选择页</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-divider direction="vertical" />
          </template>
          <el-dropdown trigger="click" @command="handleUserCommand">
            <el-button text>
              <el-icon><UserFilled /></el-icon>
              <span>{{ auth.displayName }}</span>
              <el-icon><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>{{ auth.user?.email }}</el-dropdown-item>
                <el-dropdown-item command="password">修改密码</el-dropdown-item>
                <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="app-main">
        <router-view :key="viewKey" />
      </el-main>
    </el-container>
  </el-container>

  <el-dialog v-model="passwordVisible" title="修改密码" width="420px" destroy-on-close>
    <el-form ref="passwordFormRef" :model="passwordForm" :rules="passwordRules" label-width="96px">
      <el-form-item label="当前密码" prop="current_password">
        <el-input v-model="passwordForm.current_password" type="password" show-password autocomplete="current-password" />
      </el-form-item>
      <el-form-item label="新密码" prop="new_password">
        <el-input v-model="passwordForm.new_password" type="password" show-password autocomplete="new-password" />
      </el-form-item>
      <el-form-item label="确认新密码" prop="confirm_password">
        <el-input v-model="passwordForm.confirm_password" type="password" show-password autocomplete="new-password" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="passwordVisible = false">取消</el-button>
      <el-button type="primary" :loading="passwordSubmitting" @click="submitPassword">保存</el-button>
    </template>
  </el-dialog>
</template>
