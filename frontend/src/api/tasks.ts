import { http, requestPage } from './http'
import type { SyncSettings } from '@/types/tasks'
import { segment, tenantPath } from './paths'
import type {
  AcceptedTask,
  AsyncTask,
  EntityId,
  PageParams,
  PagedData,
  QueryParams,
  SyncRun,
  SyncRunDetail,
  SyncSchedule,
  SyncSchedulePatch,
} from '@/types'

export interface SyncRunQuery extends PageParams {
  job_type?: string
  status?: string
  store_id?: EntityId
}

function toList<T>(raw: T[] | { items?: T[] } | null): T[] {
  if (!raw) return []
  return Array.isArray(raw) ? raw : (raw.items ?? [])
}

export const tasksApi = {
  syncSettings(tenantId: EntityId): Promise<SyncSettings> {
    return http.get<SyncSettings>(tenantPath(tenantId, '/sync-settings'))
  },

  updateSyncSettings(tenantId: EntityId, payload: Partial<SyncSettings>): Promise<SyncSettings> {
    return http.put<SyncSettings>(tenantPath(tenantId, '/sync-settings'), payload)
  },

  syncRuns(tenantId: EntityId, query: SyncRunQuery = {}): Promise<PagedData<SyncRun>> {
    return requestPage<SyncRun>(tenantPath(tenantId, '/sync-runs'), query as QueryParams)
  },

  syncRun(tenantId: EntityId, runId: EntityId): Promise<SyncRunDetail> {
    return http.get<SyncRunDetail>(tenantPath(tenantId, `/sync-runs/${segment(runId)}`))
  },

  retrySyncRun(tenantId: EntityId, runId: EntityId): Promise<AcceptedTask> {
    return http.post<AcceptedTask>(tenantPath(tenantId, `/sync-runs/${segment(runId)}/retry`))
  },

  async syncSchedules(tenantId: EntityId): Promise<SyncSchedule[]> {
    const raw = await http.get<SyncSchedule[] | { items?: SyncSchedule[] }>(
      tenantPath(tenantId, '/sync-schedules'),
    )
    return toList(raw)
  },

  updateSyncSchedule(tenantId: EntityId, scheduleId: EntityId, payload: SyncSchedulePatch): Promise<SyncSchedule> {
    return http.put<SyncSchedule>(tenantPath(tenantId, `/sync-schedules/${segment(scheduleId)}`), payload)
  },

  async task(tenantId: EntityId, taskId: EntityId): Promise<AsyncTask> {
    return http.get<AsyncTask>(tenantPath(tenantId, `/tasks/${segment(taskId)}`), undefined, { silent: true })
  },
}
