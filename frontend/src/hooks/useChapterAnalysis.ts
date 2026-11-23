import { App } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { analysisApi, changeCountsLabel, parseAnalyzeDone, replaceChange } from '../api/analysis'
import type { Analysis, AnalysisChange, AnalysisStatus, ChapterAnalysisState, Task } from '../api/types'
import { POLL_ACTIVE_MS, useTasksStore } from '../stores/tasks'
import { startVisiblePolling } from '../utils/visiblePolling'
import { useTaskStream, type TaskOutcome } from './useTaskStream'

/**
 * 章节编辑器的分析状态：分析状态 + 最新一次分析详情 + 正在进行的 chapter.analyze 订阅。
 * 仅跟随当前页面明确发起并取得身份的任务；卸载只断开订阅。
 */
export function useChapterAnalysis(chapterId: number, enabled = true) {
  const { message } = App.useApp()
  const trackTask = useTasksStore((s) => s.track)
  const stream = useTaskStream()
  const { follow } = stream
  const [state, setState] = useState<ChapterAnalysisState | null>(null)
  const [latest, setLatest] = useState<Analysis | null>(null)
  /** 每次刷新状态后递增，供面板重新加载历史 */
  const [version, setVersion] = useState(0)

  const latestId = state?.latest_analysis_id ?? null

  const [refreshKey, setRefreshKey] = useState(0)
  const refresh = useCallback(() => setRefreshKey((key) => key + 1), [])

  useEffect(() => {
    if (!enabled) return
    let alive = true
    let reading = false
    let stopPolling: (() => void) | undefined
    const read = async () => {
      if (reading) return
      reading = true
      try {
        const s = await analysisApi.status(chapterId)
        if (!alive) return
        setState(s)
        // 自动分析由后台创建。只读章节状态，完成后再刷新详情与历史。
        if (!stopPolling || s.status !== 'analyzing') setVersion((v) => v + 1)
        if (s.status === 'analyzing' && !stopPolling && typeof document !== 'undefined') {
          stopPolling = startVisiblePolling(read, document, POLL_ACTIVE_MS)
        } else if (s.status !== 'analyzing') {
          stopPolling?.()
          stopPolling = undefined
        }
      } catch {
        // 状态查询失败时保留原状态，轮询期间会在下一次重试。
      } finally {
        reading = false
      }
    }
    void read()
    return () => {
      alive = false
      stopPolling?.()
    }
  }, [chapterId, enabled, refreshKey])

  // 最新分析变化 / 刷新后重新加载详情（回写条目可能被其他入口修改）
  useEffect(() => {
    if (!enabled || latestId === null) return
    let alive = true
    analysisApi
      .get(latestId)
      .then((a) => {
        if (alive) setLatest(a)
      })
      .catch(() => {
        // 忽略：面板会显示空状态
      })
    return () => {
      alive = false
    }
  }, [latestId, version, enabled])

  const followAnalysis = useCallback(
    async (t: Task): Promise<TaskOutcome> => {
      if (!enabled) return { status: 'detached' }
      const outcome = await follow(t)
      if (outcome.status === 'detached') return outcome
      if (outcome.status === 'succeeded' && outcome.unpublished) {
        message.info(outcome.detail)
      } else if (outcome.status === 'succeeded') {
        const done = parseAnalyzeDone(outcome.result)
        const counts = done ? changeCountsLabel(done.change_counts) : ''
        message.success(counts ? `本章分析完成（回写：${counts}）` : '本章分析完成')
      } else if (outcome.status === 'failed') {
        message.warning(`本章分析失败：${outcome.detail}`)
      }
      refresh()
      return outcome
    },
    [follow, refresh, message, enabled],
  )

  /** 手动分析：同章已排队时后端返回那条任务（200） */
  const start = useCallback(
    async (aiConfigId: number | null): Promise<void> => {
      if (!enabled) return
      const t = await analysisApi.analyze(chapterId, { ai_config_id: aiConfigId })
      trackTask(t)
      void followAnalysis(t)
    },
    [chapterId, trackTask, followAnalysis, enabled],
  )

  /** 回写条目操作后用服务端返回的条目更新最新分析 */
  const patchChange = useCallback((updated: AnalysisChange) => {
    setLatest((prev) =>
      prev && prev.id === updated.analysis_id ? { ...prev, changes: replaceChange(prev.changes, updated) } : prev,
    )
  }, [])

  const status: AnalysisStatus = stream.running ? 'analyzing' : (state?.status ?? 'none')

  return {
    state,
    status,
    /** 最新一次分析（latest_analysis_id 为空时为 null；切换章节后可能短暂滞后，按 chapter_id 校验） */
    latest: latest && latest.chapter_id === chapterId && latest.id === latestId ? latest : null,
    version,
    task: stream.task,
    running: stream.running,
    error: stream.error,
    refresh,
    start,
    cancel: stream.cancel,
    patchChange,
  }
}

export type ChapterAnalysis = ReturnType<typeof useChapterAnalysis>
