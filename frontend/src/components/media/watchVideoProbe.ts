import { ApiError } from '../../api/client'
import { isActiveTask, taskApi } from '../../api/tasks'
import type { Task } from '../../api/types'
import { startVisiblePolling, type PollingVisibility } from '../../utils/visiblePolling'

/** 后端重启后旧 SSE 连接可能仍未关闭；用持久化的任务身份核对终态。 */
export function watchVideoProbe(task: Task, visibility: PollingVisibility, onFinished: (result: Task) => void,
  onUnavailable: () => void) {
  const controller = new AbortController()
  let reading = false
  const stopPolling = startVisiblePolling(() => {
    if (reading || controller.signal.aborted) return
    reading = true
    void taskApi.get(task, controller.signal).then((latest) => {
      if (!controller.signal.aborted && !isActiveTask(latest.status)) {
        stopPolling()
        onFinished(latest)
      }
    }).catch((error: unknown) => {
      if (!controller.signal.aborted && error instanceof ApiError && [403, 404, 409].includes(error.status)) {
        stopPolling()
        onUnavailable()
      }
      // 临时网络错误可能正处于重启窗口，下次继续检查同一任务。
    }).finally(() => { reading = false })
  }, visibility, 3000)
  return () => { controller.abort(); stopPolling() }
}
