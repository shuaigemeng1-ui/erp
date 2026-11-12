/**
 * 登录页「记住密码」本地保存。
 *
 * 说明：浏览器端无法做到真正的不可逆加密（密钥也在前端），这里使用
 * Web Crypto AES-GCM 做一层本地混淆，避免 localStorage 出现明文密码；
 * 公共电脑不建议勾选。该能力只影响表单回填，不改变服务端会话有效期。
 */

export type RememberScope = 'tenant' | 'platform'

interface RememberedPayload {
  email: string
  password: string
}

const STORAGE_PREFIX = 'erp.remembered.'
const CRYPTO_CONTEXT = 'erp-remember-v1'

function storageKey(scope: RememberScope): string {
  return `${STORAGE_PREFIX}${scope}`
}

function toBase64(bytes: Uint8Array): string {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}

function fromBase64(value: string): Uint8Array {
  const binary = atob(value)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return bytes
}

async function deriveKey(): Promise<CryptoKey> {
  const context = `${CRYPTO_CONTEXT}:${window.location.origin}`
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(context))
  return crypto.subtle.importKey('raw', digest, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt'])
}

export async function saveRememberedCredentials(
  scope: RememberScope,
  email: string,
  password: string,
): Promise<void> {
  if (!email || !password) return
  const payload: RememberedPayload = { email, password }
  const text = JSON.stringify(payload)
  try {
    if (window.crypto?.subtle) {
      const key = await deriveKey()
      const iv = crypto.getRandomValues(new Uint8Array(12))
      const encrypted = await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv: iv as unknown as BufferSource },
        key,
        new TextEncoder().encode(text),
      )
      localStorage.setItem(
        storageKey(scope),
        JSON.stringify({ v: 1, iv: toBase64(iv), data: toBase64(new Uint8Array(encrypted)) }),
      )
      return
    }
  } catch {
    // 浏览器不支持 Web Crypto 时退回编码存储，仍不出现明文 JSON
  }
  localStorage.setItem(storageKey(scope), JSON.stringify({ v: 0, data: btoa(unescape(encodeURIComponent(text))) }))
}

export async function loadRememberedCredentials(
  scope: RememberScope,
): Promise<RememberedPayload | null> {
  try {
    const raw = localStorage.getItem(storageKey(scope))
    if (!raw) return null
    const stored = JSON.parse(raw) as { v?: number; iv?: string; data?: string }
    if (!stored.data) return null
    if (stored.v === 1 && stored.iv && window.crypto?.subtle) {
      const key = await deriveKey()
      const decrypted = await crypto.subtle.decrypt(
        {
          name: 'AES-GCM',
          iv: fromBase64(stored.iv) as unknown as BufferSource,
        },
        key,
        fromBase64(stored.data) as unknown as BufferSource,
      )
      const payload = JSON.parse(new TextDecoder().decode(decrypted)) as RememberedPayload
      return payload.email && payload.password ? payload : null
    }
    const decoded = decodeURIComponent(escape(atob(stored.data)))
    const payload = JSON.parse(decoded) as RememberedPayload
    return payload.email && payload.password ? payload : null
  } catch {
    return null
  }
}

export function clearRememberedCredentials(scope: RememberScope): void {
  try {
    localStorage.removeItem(storageKey(scope))
  } catch {
    // localStorage 不可用时忽略
  }
}
