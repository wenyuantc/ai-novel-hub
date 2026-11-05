import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { chapterApi } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import { progressionApi } from '../../api/progressions'
import type {
  ChapterBrief,
  ProgressionList,
  ProgressionSubject,
  SettingBaseline,
  SettingProgression,
  SettingTextField,
  SettingTimeline,
} from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'

export type TextProgressionTarget = Extract<ProgressionSubject, { subject_kind: 'world' | 'character' }>

export interface ProgressionPanelData {
  timeline: SettingTimeline
  baseline: SettingBaseline
  progressions: ProgressionList<SettingProgression>
  fields: SettingTextField[]
  chapters: ChapterBrief[]
}

interface LoadState {
  key: string
  refresh: number
  data: ProgressionPanelData | null
  error: string | null
}

const PAGE_SIZE = 20

async function loadPanel(projectId: number, target: TextProgressionTarget, page: number, signal: AbortSignal): Promise<ProgressionPanelData> {
  const timeline = await progressionApi.findTimeline(projectId, target, signal)
  if (!timeline) throw new Error('未找到该条目的基础设定，请刷新或重新打开条目')
  const [baseline, progressions, textFields, chapters] = await Promise.all([
    progressionApi.getBaseline(projectId, timeline.id, signal),
    progressionApi.listProgressions(projectId, timeline.id, { offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE }, signal),
    progressionApi.listTextFields(projectId, timeline.id, signal),
    chapterApi.list(projectId),
  ])
  if (baseline.timeline_revision !== progressions.timeline_revision || baseline.timeline_revision !== textFields.timeline_revision) {
    throw new Error('设定刚刚发生变化，请刷新后重试')
  }
  return {
    timeline: { ...timeline, revision: baseline.timeline_revision, baseline_status: baseline.baseline_status, legacy_compat_allowed: baseline.legacy_compat_allowed },
    baseline,
    progressions,
    fields: textFields.items,
    chapters,
  }
}

/** 以身份、作品、目标和页码划分请求代次，刷新时保留表单和现有数据。 */
export function useProgressionPanelData(projectId: number, target: TextProgressionTarget, page: number) {
  const access = useProjectAccess(projectId)
  const { canRead, canReadNow } = access
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const kind = target.subject_kind
  const targetId = kind === 'world' ? target.world_setting_id : target.character_id
  const subject = useMemo<TextProgressionTarget>(
    () => kind === 'world' ? { subject_kind: 'world', world_setting_id: targetId } : { subject_kind: 'character', character_id: targetId },
    [kind, targetId],
  )
  const key = `${identity}:${projectId}:${kind}:${targetId}:${page}`
  const [refreshCount, setRefreshCount] = useState(0)
  const [state, setState] = useState<LoadState | null>(null)
  const epoch = useRef(0)
  const scope = useRef(key)
  const pending = useRef(new Set<AbortController>())
  const refresh = useCallback(() => setRefreshCount((value) => value + 1), [])

  useLayoutEffect(() => {
    const requests = pending.current
    scope.current = key
    return () => {
      scope.current = ''
      epoch.current += 1
      requests.forEach((controller) => controller.abort())
      requests.clear()
    }
  }, [key])

  useProjectRefresh(projectId, kind === 'world' ? ['world', 'chapters'] : ['characters', 'chapters'], refresh)

  useEffect(() => {
    if (!canRead) return
    const ticket = ++epoch.current
    const controller = new AbortController()
    const requests = pending.current
    requests.add(controller)
    loadPanel(projectId, subject, page, controller.signal).then(
      (data) => {
        if (ticket === epoch.current && scope.current === key && !controller.signal.aborted && canReadNow()) {
          setState({ key, refresh: refreshCount, data, error: null })
        }
      },
      (err: unknown) => {
        if (ticket === epoch.current && scope.current === key && !controller.signal.aborted && canReadNow()) {
          setState((previous) => ({ key, refresh: refreshCount, data: previous?.key === key ? previous.data : null, error: errorMessage(err) }))
        }
      },
    ).finally(() => { requests.delete(controller) })
    return () => { controller.abort(); requests.delete(controller) }
  }, [projectId, subject, page, key, refreshCount, canRead, canReadNow])

  const current = state?.key === key ? state : null
  const loading = canRead && (!current || current.refresh !== refreshCount)

  /** 显式刷新用于冲突对照；它不会卸载正在编辑的弹窗或重置草稿。 */
  const refreshForComparison = async (eventId: number | null) => {
    if (!canReadNow() || !current?.data) throw new Error('权限或设定已变化，请重新打开条目')
    const ticket = ++epoch.current
    const controller = new AbortController()
    pending.current.add(controller)
    try {
      const [data, detail] = await Promise.all([
        loadPanel(projectId, subject, page, controller.signal),
        eventId === null ? Promise.resolve(null) : progressionApi.getProgression(projectId, current.data.timeline.id, eventId, controller.signal),
      ])
      if (controller.signal.aborted || ticket !== epoch.current || scope.current !== key || !canReadNow()) {
        throw new Error('当前条目已切换，请重新打开')
      }
      if (detail && detail.timeline_revision !== data.baseline.timeline_revision) throw new Error('设定再次发生变化，请重新读取最新版本')
      setState({ key, refresh: refreshCount, data, error: null })
      return { data, event: detail?.item ?? null }
    } finally {
      pending.current.delete(controller)
    }
  }

  const isCurrent = () => scope.current === key && canReadNow()
  return { ...access, data: current?.data ?? null, error: current?.error ?? null, loading, refresh, refreshForComparison, isCurrent, pageSize: PAGE_SIZE }
}
