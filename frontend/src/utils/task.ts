import { tasksApi } from '@/api/tasks'
import type { AsyncTask, EntityId } from '@/types'

const TERMINAL_STATUSES = new Set([
  'succeeded',
  'success',
  'failed',
  'failure',
  'error',
  'partial',
  'cancelled',
  'canceled',
  'completed',
  'done',
])

export interface PollTaskOptions {
  intervalMs?: number
  timeoutMs?: number
  signal?: AbortSignal
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('已取消', 'AbortError'))
      return
    }
    const timer = setTimeout(() => {
      cleanup()
      resolve()
    }, ms)
    function cleanup(): void {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
    }
    function onAbort(): void {
      cleanup()
      reject(new DOMException('已取消', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

/** 轮询 202 异步任务直到终态；超时返回最后一次结果。 */
export async function pollAsyncTask(
  tenantId: EntityId,
  taskId: EntityId,
  options: PollTaskOptions = {},
): Promise<AsyncTask> {
  const intervalMs = options.intervalMs ?? 1500
  const timeoutMs = options.timeoutMs ?? 120000
  const startedAt = Date.now()
  let last: AsyncTask = { task_id: taskId, status: 'pending' }

  while (Date.now() - startedAt < timeoutMs) {
    if (options.signal?.aborted) throw new DOMException('已取消', 'AbortError')
    last = await tasksApi.task(tenantId, taskId)
    if (TERMINAL_STATUSES.has(String(last.status).toLowerCase())) return last
    await sleep(intervalMs, options.signal)
  }
  return last
}

export function isTaskFailed(task: AsyncTask): boolean {
  const status = String(task.status).toLowerCase()
  return status === 'failed' || status === 'failure' || status === 'error'
}

export function taskErrorMessage(task: AsyncTask): string {
  const error = task.error
  if (!error) return '未知错误'
  if (typeof error === 'string') return error
  if (typeof error === 'object' && error !== null) {
    const record = error as Record<string, unknown>
    if (typeof record.message === 'string') return record.message
    return JSON.stringify(error)
  }
  return String(error)
}
