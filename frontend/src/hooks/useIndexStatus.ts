import { useCallback, useEffect, useState } from 'react'
import { kbApi } from '../api/kb'
import { isActiveTask } from '../api/tasks'
import type { IndexStatus } from '../api/types'
import { useTasksStore } from '../stores/tasks'

/** 建索引进行中时的刷新间隔 */
export const INDEX_POLL_MS = 3000

/**
 * 项目索引状态（知识库页 / 记忆页共用）：挂载时加载；任务结束（store `version` 变化）后刷新；
 * 本项目有活动 `kb.index` 任务、后端报告 `indexing` 或调用方报告 `busy`（如有文档在处理）时每 3 秒刷新，并调用 `onTick`。
 */
export function useIndexStatus(pid: number, { busy = false, onTick }: { busy?: boolean; onTick?: () => void } = {}) {
  const [status, setStatus] = useState<IndexStatus>()
  const version = useTasksStore((s) => s.version)
  const kbActive = useTasksStore((s) =>
    s.active.some((t) => t.kind === 'kb.index' && t.project_id === pid && isActiveTask(t.status)),
  )

  // 递增即重新读取（刷新 / 轮询）；读取放在 effect 的回调里，避免在 effect 中同步 setState
  const [tick, setTick] = useState(0)
  const refresh = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    let alive = true
    kbApi.indexStatus(pid).then(
      (s) => {
        if (alive) setStatus(s)
      },
      () => {
        // 状态只用于横幅提示，读取失败不打扰用户（页面其他请求会显示错误）
      },
    )
    return () => {
      alive = false
    }
  }, [pid, version, tick])

  const polling = busy || kbActive || Boolean(status?.indexing)
  useEffect(() => {
    if (!polling) return
    const timer = setInterval(() => {
      refresh()
      onTick?.()
    }, INDEX_POLL_MS)
    return () => clearInterval(timer)
  }, [polling, refresh, onTick])

  return { status, refresh, polling, version }
}
