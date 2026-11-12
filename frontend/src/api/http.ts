import { ElMessage } from 'element-plus'
import type {
  ApiEnvelope,
  ApiErrorEnvelope,
  PagedData,
  QueryParams,
} from '@/types'

/** API 前缀：生产环境由 Nginx 反代 /api/v1，开发环境由 Vite 代理，均可通过环境变量覆盖。 */
const API_BASE = (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/+$/, '')

const CSRF_COOKIE = 'vs_csrf'

/** 后端统一错误（401/403/404/409/422 等） */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string
  readonly details: unknown

  constructor(status: number, code: string, message: string, requestId = '', details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.details = details
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

export function isAbortError(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false
  const name = (error as { name?: string }).name
  return name === 'AbortError' || name === 'TimeoutError'
}

export function describeApiError(error: unknown): string {
  if (isApiError(error)) {
    const extras: string[] = []
    if (error.code) extras.push(`code=${error.code}`)
    if (error.requestId) extras.push(`request_id=${error.requestId}`)
    return extras.length > 0 ? `${error.message}（${extras.join('，')}）` : error.message
  }
  if (error instanceof Error) return error.message
  return '请求失败'
}

/* ------------------------------------------------------------------ *
 * 租户切换 / 页面卸载时的统一取消范围
 * ------------------------------------------------------------------ */

let scopeController = new AbortController()

/** 当前请求范围信号；所有请求都会自动合并该信号。 */
export function currentScopeSignal(): AbortSignal {
  return scopeController.signal
}

/** 取消旧范围在途请求并开启新范围（租户切换、退出登录时调用）。 */
export function resetRequestScope(): void {
  const previous = scopeController
  scopeController = new AbortController()
  previous.abort()
}

function combineSignals(signals: Array<AbortSignal | undefined>): AbortSignal | undefined {
  const usable = signals.filter((signal): signal is AbortSignal => signal !== undefined)
  if (usable.length === 0) return undefined
  if (usable.length === 1) return usable[0]
  const controller = new AbortController()
  for (const signal of usable) {
    if (signal.aborted) {
      controller.abort()
      break
    }
    signal.addEventListener('abort', () => controller.abort(), { once: true })
  }
  return controller.signal
}

/* ------------------------------------------------------------------ *
 * 401 处理
 * ------------------------------------------------------------------ */

type UnauthorizedHandler = () => void

let unauthorizedHandler: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler
}

/* ------------------------------------------------------------------ *
 * CSRF Token（读取可读 Cookie vs_csrf，写请求放 X-CSRF-Token）
 * ------------------------------------------------------------------ */

let csrfToken: string | null = null
let csrfRequest: Promise<string | null> | null = null

export function readCookie(name: string): string | null {
  if (typeof document === 'undefined' || !document.cookie) return null
  const prefix = `${name}=`
  for (const part of document.cookie.split('; ')) {
    if (part.startsWith(prefix)) {
      try {
        return decodeURIComponent(part.slice(prefix.length))
      } catch {
        return part.slice(prefix.length)
      }
    }
  }
  return null
}

/** 登录/退出后服务端会轮换 CSRF，清理内存缓存。 */
export function clearCsrfCache(): void {
  csrfToken = null
  csrfRequest = null
}

async function fetchCsrfToken(): Promise<string | null> {
  try {
    const response = await fetch(`${API_BASE}/auth/csrf`, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    })
    if (!response.ok) return null
    const payload = (await response.json()) as ApiEnvelope<{ csrf_token?: string }>
    csrfToken = payload.data?.csrf_token ?? readCookie(CSRF_COOKIE)
    return csrfToken
  } catch {
    return null
  }
}

/** 强制重新签发匿名 CSRF：登录页进入 / CSRF 失效重试时使用。 */
export async function refreshCsrfToken(): Promise<string | null> {
  clearCsrfCache()
  const token = await fetchCsrfToken()
  if (token) csrfToken = token
  return token
}

async function ensureCsrfToken(): Promise<string | null> {
  const fromCookie = readCookie(CSRF_COOKIE)
  if (fromCookie) {
    csrfToken = fromCookie
    return fromCookie
  }
  if (csrfToken) return csrfToken
  if (!csrfRequest) {
    csrfRequest = fetchCsrfToken().finally(() => {
      csrfRequest = null
    })
  }
  return csrfRequest
}

/* ------------------------------------------------------------------ *
 * 请求实现
 * ------------------------------------------------------------------ */

function createRequestId(): string {
  const cryptoObject = globalThis.crypto as Crypto | undefined
  if (cryptoObject && typeof cryptoObject.randomUUID === 'function') {
    return cryptoObject.randomUUID()
  }
  return `req-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`
}

function buildUrl(path: string, query?: QueryParams): string {
  const url = `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
  if (!query) return url
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      for (const item of value) {
        if (item === undefined || item === null || item === '') continue
        search.append(key, String(item))
      }
    } else {
      search.append(key, String(value))
    }
  }
  const queryString = search.toString()
  return queryString ? `${url}?${queryString}` : url
}

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface RequestOptions {
  method?: HttpMethod
  query?: QueryParams
  body?: unknown
  signal?: AbortSignal
  /** 静默：不弹统一错误提示（守卫探测类请求使用） */
  silent?: boolean
  /** 401 时不触发全局跳转（登录探测 / 登录页本身使用） */
  skipUnauthorizedHandler?: boolean
  headers?: Record<string, string>
  /** 内部使用：CSRF 失效后已自动刷新并重试过一次 */
  csrfRetried?: boolean
}

export type ExtraRequestOptions = Omit<RequestOptions, 'method' | 'body' | 'query' | 'csrfRetried'>

function defaultErrorMessage(status: number): string {
  switch (status) {
    case 400:
      return '请求参数不合法'
    case 401:
      return '登录状态已失效，请重新登录'
    case 403:
      return '当前账号没有该操作权限'
    case 404:
      return '对象不存在或不可见'
    case 409:
      return '业务冲突，请刷新后重试'
    case 422:
      return '参数校验失败'
    case 429:
      return '请求过于频繁，请稍后再试'
    default:
      return status >= 500 ? '服务暂时不可用，请稍后重试' : '请求失败'
  }
}

function notifyError(error: ApiError): void {
  ElMessage({
    type: 'error',
    message: describeApiError(error),
    duration: 6000,
    showClose: true,
  })
}

async function parseJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get('Content-Type') ?? ''
  if (!contentType.toLowerCase().includes('application/json')) return null
  try {
    return (await response.json()) as unknown
  } catch {
    return null
  }
}

async function handleErrorResponse(response: Response, options: RequestOptions): Promise<never> {
  const payload = (await parseJson(response)) as ApiErrorEnvelope | null
  const headerRequestId = response.headers.get('X-Request-ID') ?? ''
  const requestId = payload?.request_id || headerRequestId
  const apiError = new ApiError(
    response.status,
    payload?.error?.code ? String(payload.error.code) : String(response.status),
    payload?.error?.message || defaultErrorMessage(response.status),
    requestId,
    payload?.error?.details,
  )

  if (response.status === 401) {
    if (!options.skipUnauthorizedHandler) {
      ElMessage({
        type: 'warning',
        message: apiError.message || defaultErrorMessage(401),
        duration: 4000,
        showClose: true,
      })
      unauthorizedHandler?.()
    }
    throw apiError
  }

  if (!options.silent) notifyError(apiError)
  throw apiError
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const unsafe = method !== 'GET'
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...options.headers,
  }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (unsafe) {
    const token = await ensureCsrfToken()
    if (token) headers['X-CSRF-Token'] = token
  }
  headers['X-Request-ID'] = createRequestId()

  const signal = combineSignals([currentScopeSignal(), options.signal])

  let response: Response
  try {
    response = await fetch(buildUrl(path, options.query), {
      method,
      headers,
      // 同源 Cookie 会话认证
      credentials: 'same-origin',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal,
      cache: 'no-store',
    })
  } catch (error) {
    if (isAbortError(error)) throw error
    const networkError = new ApiError(0, 'network_error', '网络连接失败，请检查后端服务是否可用')
    if (!options.silent) notifyError(networkError)
    throw networkError
  }

  if (response.status === 204) return undefined as T
  if (!response.ok) {
    // CSRF 失效（旧会话 Cookie、多标签页轮换、服务端清会话）时：
    // 重新签发匿名 CSRF 并自动重试一次，避免用户登录/提交被卡死。
    if (response.status === 403 && unsafe && !options.csrfRetried) {
      const payload = (await response
        .clone()
        .json()
        .catch(() => null)) as ApiErrorEnvelope | null
      if (payload?.error?.code === 'AUTH_CSRF_INVALID') {
        const token = await refreshCsrfToken()
        if (token) {
          return request<T>(path, { ...options, csrfRetried: true })
        }
      }
    }
    return handleErrorResponse(response, options)
  }

  const payload = await parseJson(response)
  if (payload && typeof payload === 'object' && 'data' in payload) {
    return (payload as ApiEnvelope<T>).data
  }
  // 兼容未包裹的响应
  return payload as T
}

/** 分页请求：兼容 {items,total,page,page_size} 与裸数组两种响应。 */
export async function requestPage<T>(
  path: string,
  query?: QueryParams,
  options?: ExtraRequestOptions,
): Promise<PagedData<T>> {
  const data = await request<PagedData<T> | T[]>(path, { ...options, method: 'GET', query })
  if (Array.isArray(data)) {
    return { items: data, total: data.length, page: 1, page_size: data.length }
  }
  if (!data) return { items: [], total: 0, page: 1, page_size: 20 }
  return {
    items: data.items ?? [],
    total: data.total ?? 0,
    page: data.page ?? 1,
    page_size: data.page_size ?? 20,
  }
}

export const http = {
  get<T>(path: string, query?: QueryParams, options?: ExtraRequestOptions): Promise<T> {
    return request<T>(path, { ...options, method: 'GET', query })
  },
  post<T>(path: string, body?: unknown, options?: ExtraRequestOptions): Promise<T> {
    return request<T>(path, { ...options, method: 'POST', body })
  },
  put<T>(path: string, body?: unknown, options?: ExtraRequestOptions): Promise<T> {
    return request<T>(path, { ...options, method: 'PUT', body })
  },
  patch<T>(path: string, body?: unknown, options?: ExtraRequestOptions): Promise<T> {
    return request<T>(path, { ...options, method: 'PATCH', body })
  },
  delete<T>(path: string, options?: ExtraRequestOptions): Promise<T> {
    return request<T>(path, { ...options, method: 'DELETE' })
  },
}
