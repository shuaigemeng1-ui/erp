import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
import '@/styles/index.css'

import App from './App.vue'
import router from './router'
import { permissionDirective } from '@/directives/permission'
import { setUnauthorizedHandler } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { useTenantStore } from '@/stores/tenant'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
app.use(ElementPlus, { locale: zhCn })
app.directive('permission', permissionDirective)

let redirectingToLogin = false

/** 任意请求 401：清理会话与租户状态，跳转登录页（带回来路径）。 */
setUnauthorizedHandler(() => {
  if (redirectingToLogin) return
  redirectingToLogin = true

  const auth = useAuthStore()
  const tenant = useTenantStore()
  auth.clearSession()
  tenant.resetForLogout()

  const current = router.currentRoute.value
  const query = current.fullPath && current.fullPath !== '/' ? { redirect: current.fullPath } : undefined
  void router
    .replace({ name: 'login', query })
    .finally(() => {
      window.setTimeout(() => {
        redirectingToLogin = false
      }, 1000)
    })
})

app.mount('#app')
