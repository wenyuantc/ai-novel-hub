import { App } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { analysisApi, changeCountsLabel, parseAnalyzeDone, replaceChange } from '../api/analysis'
import { taskApi } from '../api/tasks'
import type { Analysis, AnalysisChange, AnalysisStatus, ChapterAnalysisState, Task } from '../api/types'
import { useTasksStore } from '../stores/tasks'
import { useTaskStream, type TaskOutcome } from './useTaskStream'

/**
 * 章节编辑器的分析状态：分析状态 + 最新一次分析详情 + 正在进行的 chapter.analyze 订阅。
 * 挂载后自动接上本章的活动分析任务（含生成 / 润色 / 重写成功后自动排队的）；卸载只断开订阅。
 */
export function useChapterAnalysis(chapterId: number, enabled = true) {
  const { message } = App.useApp()
  const activeTasks = useTasksStore((s) => s.active)
  const trackTask = useTasksStore((s) => s.track)
  const stream = useTaskStream()
  const { follow } = stream
  const [state, setState] = useState<ChapterAnalysisState | null>(null)
  const [latest, setLatest] = useState<Analysis | null>(null)
  /** 每次刷新状态后递增，供面板重新加载历史 */
  const [version, setVersion] = useState(0)
  const followedRef = useRef(new Set<number>())

  const latestId = state?.latest_analysis_id ?? null

  const refresh = useCallback(async (): Promise<ChapterAnalysisState | null> => {
    if (!enabled) return null
    try {
      const s = await analysisApi.status(chapterId)
      setState(s)
      setVersion((v) => v + 1)
      return s
    } catch {
      // 状态查询失败时保持原状态（不打断编辑）
      return null
    }
  }, [chapterId, enabled])

  useEffect(() => {
    if (!enabled) return
    let alive = true
    analysisApi
      .status(chapterId)
      .then((s) => {
        if (!alive) return
        setState(s)
        setVersion((v) => v + 1)
      })
      .catch(() => {
        // 忽略：按「未分析」显示
      })
    return () => {
      alive = false
    }
  }, [chapterId, enabled])

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
      followedRef.current.add(t.id)
      const outcome = await follow(t)
      if (outcome.status === 'detached') return outcome
      if (outcome.status === 'succeeded') {
        const done = parseAnalyzeDone(outcome.result)
        const counts = done ? changeCountsLabel(done.change_counts) : ''
        message.success(counts ? `本章分析完成（回写：${counts}）` : '本章分析完成')
      } else if (outcome.status === 'failed') {
        message.warning(`本章分析失败：${outcome.detail}`)
      }
      await refresh()
      return outcome
    },
    [follow, refresh, message, enabled],
  )

  // 接上活动分析任务：状态接口给出的 active_task_id，或任务列表里本章的 chapter.analyze
  const followRef = useRef(followAnalysis)
  useEffect(() => {
    followRef.current = followAnalysis
  })
  const activeTaskId = state?.active_task_id ?? null
  useEffect(() => {
    if (!enabled || stream.running) return
    const fromList = activeTasks.find(
      (t) => t.kind === 'chapter.analyze' && t.chapter_id === chapterId && !followedRef.current.has(t.id),
    )
    if (fromList) {
      void followRef.current(fromList)
      return
    }
    if (activeTaskId === null || followedRef.current.has(activeTaskId)) return
    followedRef.current.add(activeTaskId)
    let alive = true
    taskApi
      .get(activeTaskId)
      .then((t) => {
        if (alive) void followRef.current(t)
      })
      .catch(() => {
        // 任务已被删除：忽略
      })
    return () => {
      alive = false
    }
  }, [activeTasks, activeTaskId, chapterId, stream.running, enabled])

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
