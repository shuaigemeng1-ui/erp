import { ElMessage } from 'element-plus'
import { useTenantStore } from '@/stores/tenant'
import { isTaskFailed, pollAsyncTask, taskErrorMessage } from '@/utils/task'
import { isAbortError } from '@/api/http'
import type { AcceptedTask, EntityId } from '@/types'

/**
 * 统一处理 202 异步任务：提交 → 提示 task_id → 轮询 → 完成后刷新列表。
 * 租户切换后丢弃结果（不覆盖新租户数据）。
 */
export function useTenantTask() {
  const tenant = useTenantStore()

  async function submitAndPoll(
    submit: (tenantId: EntityId) => Promise<AcceptedTask>,
    onFinished?: () => void | Promise<void>,
  ): Promise<void> {
    const tenantId = tenant.requireTenantId()
    const accepted = await submit(tenantId)
    const taskId = accepted?.task_id ? String(accepted.task_id) : ''
    if (!taskId) {
      await onFinished?.()
      return
    }
    ElMessage.success(`任务已提交，task_id=${taskId}`)
    try {
      const task = await pollAsyncTask(tenantId, taskId)
      if (tenant.activeTenantId !== tenantId) return
      if (isTaskFailed(task)) {
        ElMessage.error(`任务失败：${taskErrorMessage(task)}`)
      } else {
        ElMessage.success(`任务完成：${task.status}`)
      }
      await onFinished?.()
    } catch (cause) {
      if (isAbortError(cause)) return
      // 轮询失败不覆盖已有提示
    }
  }

  return { submitAndPoll }
}
