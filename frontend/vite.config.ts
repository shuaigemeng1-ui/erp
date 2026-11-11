import { mkdirSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

const workspaceRoot = fileURLToPath(new URL('.', import.meta.url))

/**
 * 部分受限 Windows 环境下系统临时目录禁止删除文件，esbuild 处理大文件（>1MB，
 * 例如压缩后的构建产物）时会因为临时文件无法删除而失败。这里把构建期临时目录
 * 放到工程内（已被 .gitignore 忽略），对正常环境无副作用。
 */
function useWorkspaceTempDir(): void {
  const tempDir = fileURLToPath(new URL('./.tmp-build', import.meta.url))
  try {
    mkdirSync(tempDir, { recursive: true })
    process.env.TEMP = tempDir
    process.env.TMP = tempDir
  } catch {
    // 无法创建时保持系统默认临时目录
  }
}

useWorkspaceTempDir()

/**
 * 开发环境把 /api 代理到后端，避免浏览器跨域与 Cookie 问题。
 * 后端地址通过环境变量 VITE_API_PROXY_TARGET 配置（默认 http://127.0.0.1:8000），
 * 不在源码中写死后端地址。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, workspaceRoot, '')
  const proxyTarget = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000'
  const devPort = Number(env.VITE_DEV_PORT || 5173)

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: devPort,
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
          ws: false,
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: mode !== 'production',
      chunkSizeWarningLimit: 1500,
    },
  }
})
