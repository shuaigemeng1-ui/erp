import type { Decimal } from '@/types'

const DATE_PAD_LENGTH = 2

function pad(value: number): string {
  return String(value).padStart(DATE_PAD_LENGTH, '0')
}

/** 后端时间为 UTC，按浏览器本地时区展示。 */
export function formatDateTime(value?: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}:${pad(date.getSeconds())}`
}

export function formatDate(value?: string | null): string {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return String(value)
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 金额/数量展示：字符串保持原值，数字按小数位格式化。 */
export function formatDecimal(value: Decimal | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'string') return value
  return value.toFixed(digits)
}

export function formatQuantity(value: Decimal | null | undefined): string {
  return formatDecimal(value, 0)
}

export function formatMoney(value: Decimal | null | undefined, currency?: string | null): string {
  const amount = formatDecimal(value, 2)
  if (amount === '-') return '-'
  return currency ? `${amount} ${currency}` : amount
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '-'
  return `${(value * 100).toFixed(0)}%`
}

export function textOrDash(value: string | null | undefined): string {
  return value === null || value === undefined || value === '' ? '-' : value
}

export function truncate(value: string | null | undefined, max = 60): string {
  if (!value) return '-'
  return value.length > max ? `${value.slice(0, max)}…` : value
}

export function numberOrUndefined(value: string | number | null | undefined): number | undefined {
  if (value === null || value === undefined || value === '') return undefined
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

export function stringOrUndefined(value: string | number | null | undefined): string | undefined {
  if (value === null || value === undefined || value === '') return undefined
  return String(value)
}

/** 把 JSON 文本安全序列化为对象，非法时返回 null。 */
export function parseJsonObject(value: string): Record<string, unknown> | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  try {
    const parsed = (JSON.parse(trimmed) as unknown) ?? null
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
    return null
  } catch {
    return null
  }
}

export function prettyJson(value: unknown): string {
  if (value === null || value === undefined) return ''
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}
