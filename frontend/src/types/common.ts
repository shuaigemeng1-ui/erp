/** 通用类型：大整数 ID 一律为字符串，禁止转 number。 */
export type EntityId = string

/** 金额/数量：后端 DECIMAL 可能返回字符串，展示层统一格式化。 */
export type Decimal = string | number

/** q/分页等查询参数值。 */
export type QueryValue = string | number | boolean | null | undefined | readonly (string | number)[]

export type QueryParams = Record<string, QueryValue>

export interface ApiEnvelope<T> {
  data: T
  request_id?: string
}

export interface ApiErrorPayload {
  code: string
  message: string
  details?: unknown
}

export interface ApiErrorEnvelope {
  error?: ApiErrorPayload
  request_id?: string
}

export interface PageParams {
  page?: number
  page_size?: number
}

export interface PagedData<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/** 202 异步任务：前端轮询 /tenants/{tenant_id}/tasks/{task_id} */
export interface AcceptedTask {
  task_id: string
}

export type SortOrder = 'asc' | 'desc'
