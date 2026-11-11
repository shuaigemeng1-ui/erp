import type { EntityId } from './common'

export interface AuditLog {
  id: EntityId
  actor_id?: EntityId | null
  actor_name?: string | null
  action: string
  resource_type: string
  resource_id?: EntityId | null
  summary?: string
  payload?: unknown
  ip?: string
  request_id?: string
  created_at?: string
}
