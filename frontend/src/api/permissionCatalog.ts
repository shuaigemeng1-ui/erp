import type { PermissionGroup, PermissionItem } from '@/types'

/**
 * 权限目录归一化：把 `GET /tenants/{tenant_id}/permissions` 的响应统一成
 * `[{code, name, items: [{code, name, group}]}]`，供角色编辑器直接按分组渲染。
 *
 * 后端契约是 `{groups: [{code, name, permissions: [{code, name}]}], permissions: [code]}`，
 * 分组内的条目字段叫 `permissions`（不是 `items`），历史上的扁平结构也一并兼容。
 */

type RawRecord = Record<string, unknown>

function asRecord(value: unknown): RawRecord | null {
  return value !== null && typeof value === 'object' ? (value as RawRecord) : null
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function readString(record: RawRecord | null, key: string): string | null {
  const value = record?.[key]
  return typeof value === 'string' && value.length > 0 ? value : null
}

function toPermissionItem(raw: unknown, group: string): PermissionItem | null {
  const record = asRecord(raw)
  const code = readString(record, 'code')
  if (!code) return null
  return { code, name: readString(record, 'name') ?? code, group }
}

/** 归一化单个分组；没有任何权限条目时返回 null（顶层 `permissions` 是编码字符串数组，需排除）。 */
function toPermissionGroup(raw: unknown, index: number): PermissionGroup | null {
  const record = asRecord(raw)
  if (!record) return null
  const rawItems = [...asArray(record.items), ...asArray(record.permissions)]
  if (rawItems.length === 0) return null
  const code = readString(record, 'code') ?? readString(record, 'group') ?? `group_${index}`
  const name = readString(record, 'name') ?? readString(record, 'label') ?? code
  const items = rawItems
    .map((item) => toPermissionItem(item, code))
    .filter((item): item is PermissionItem => item !== null)
  return items.length > 0 ? { code, name, items } : null
}

/** 扁平权限列表兜底：按 `a.b` 的前缀分组，分组名沿用前缀编码。 */
function groupFlatItems(rawItems: unknown[]): PermissionGroup[] {
  const grouped = new Map<string, PermissionItem[]>()
  for (const raw of rawItems) {
    const item = toPermissionItem(raw, '')
    if (!item) continue
    const key = item.group || item.code.split('.')[0] || 'other'
    const list = grouped.get(key) ?? []
    list.push({ ...item, group: key })
    grouped.set(key, list)
  }
  return [...grouped.entries()].map(([code, items]) => ({ code, name: code, items }))
}

/** 归一化权限目录响应，兼容三种结构：分组对象、分组数组、扁平列表。 */
export function normalizePermissionGroups(raw: unknown): PermissionGroup[] {
  const record = asRecord(raw)
  const candidates = Array.isArray(raw) ? raw : asArray(record?.groups)
  const groups = candidates
    .map((candidate, index) => toPermissionGroup(candidate, index))
    .filter((group): group is PermissionGroup => group !== null)
  if (groups.length > 0) return groups
  return groupFlatItems(Array.isArray(raw) ? raw : [...asArray(record?.items), ...asArray(record?.permissions)])
}

/** 展开为扁平权限项列表（勾选项 / 权限码选择器使用）。 */
export function flattenPermissionGroups(groups: PermissionGroup[]): PermissionItem[] {
  return groups.flatMap((group) => group.items)
}
