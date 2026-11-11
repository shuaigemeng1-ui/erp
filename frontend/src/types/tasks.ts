import type { EntityId } from './common'

export interface SyncRun {
  id: EntityId
  job_type: string
  store_id?: EntityId | null
  store_name?: string | null
  status: string
  triggered_by: string
  requested_by?: EntityId | null
  started_at?: string | null
  finished_at?: string | null
  total_processed?: number | null
  error_message?: string | null
  cursor?: string | null
}

export interface SyncRunItem {
  id?: EntityId
  object_type?: string
  object_id?: string
  status?: string
  message?: string
}

export interface SyncRunDetail extends SyncRun {
  items?: SyncRunItem[]
  logs?: unknown[]
  result?: unknown
}

export interface SyncSchedule {
  id: EntityId
  job_type: string
  store_id?: EntityId | null
  store_name?: string | null
  interval_minutes: number
  enabled: boolean
  last_run_at?: string | null
  next_run_at?: string | null
}

export interface SyncSchedulePatch {
  enabled: boolean
  interval_minutes: number
}

export interface AsyncTask {
  task_id: EntityId
  type?: string
  status: string
  result?: unknown
  error?: unknown
}

export type TaskStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'partial' | string
