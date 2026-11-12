<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useTenantStore } from '@/stores/tenant'
import StatusTag from '@/components/StatusTag.vue'

const auth = useAuthStore()
const tenant = useTenantStore()
const router = useRouter()

const switchingId = ref('')

const tenants = computed(() => auth.tenants)
const hasTenant = computed(() => tenants.value.length > 0)

onMounted(async () => {
  if (auth.isAuthenticated) await auth.refreshTenants()
})

async function enter(tenantId: string): Promise<void> {
  switchingId.value = tenantId
  try {
    const success = await tenant.switchTenant(tenantId)
    if (!success) {
      ElMessage.error(tenant.error || '租户权限加载失败')
      return
    }
    await router.replace('/')
  } finally {
    switchingId.value = ''
  }
}

async function logout(): Promise<void> {
  await auth.logout()
  tenant.resetForLogout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="auth-page">
    <div class="auth-card" style="width: 560px">
      <h1 class="auth-title">选择租户</h1>
      <p class="auth-subtitle">当前账号 {{ auth.user?.email }}</p>

      <div v-if="hasTenant" class="tenant-card-list">
        <el-card v-for="item in tenants" :key="item.id" shadow="hover">
          <div style="display: flex; justify-content: space-between; align-items: center; gap: 12px">
            <div>
              <div style="font-weight: 600">{{ item.name }}</div>
              <div class="text-muted mono">{{ item.code }} · {{ item.timezone }}</div>
              <div style="margin-top: 4px">
                <el-tag v-for="role in item.roles" :key="role" size="small" type="info" style="margin-right: 4px">
                  {{ role }}
                </el-tag>
              </div>
            </div>
            <div style="display: flex; align-items: center; gap: 8px">
              <StatusTag :status="item.status" />
              <el-button
                type="primary"
                :disabled="item.status !== 'active'"
                :loading="switchingId === item.id"
                @click="enter(item.id)"
              >
                进入
              </el-button>
            </div>
          </div>
        </el-card>
      </div>

      <el-empty v-else description="当前账号还没有可访问的租户" />

      <div class="auth-footer">
        <el-button text @click="logout">退出登录</el-button>
      </div>
    </div>
  </div>
</template>
