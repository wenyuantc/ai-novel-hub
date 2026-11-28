import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import type { AIConfig } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useAIConfigStore } from '../../stores/aiConfigs'
import { dramaApi, type EpisodeModelIds, type EpisodeModelType } from './api'

export interface EpisodeModelBinding {
  ready: boolean
  unavailable?: readonly EpisodeModelType[]
  /** undefined 表示选择尚未确认，null 表示使用默认模型。 */
  configFor: (type: EpisodeModelType) => number | null | undefined
  reload?: () => void
}

export const EPISODE_MODEL_TYPES = ['text', 'image', 'video'] as const

export function confirmEpisodeModel(prefs: EpisodeModelIds, type: EpisodeModelType, catalog: readonly AIConfig[]) {
  const id = prefs[type]
  if (id === null) return null
  return catalog.some((config) => config.id === id && config.model_type === type) ? id : undefined
}

export function useEpisodeModelSettings(eid: number, enabled: boolean, current: () => boolean) {
  const prefs = useLoader(
    useCallback(
      () => (enabled && current() ? dramaApi.episodeModels(eid, current) : Promise.resolve(null)),
      [eid, enabled, current],
    ),
  )
  const catalog = useAIConfigStore((s) => s.configs)
  const loaded = useAIConfigStore((s) => s.loaded)
  const loadingCatalog = useAIConfigStore((s) => s.loading)
  const loadCatalog = useAIConfigStore((s) => s.load)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const writing = useRef(false)
  const attempted = useRef('')
  const values = prefs.data ?? null
  const unavailable = EPISODE_MODEL_TYPES.filter(
    (type) => values && loaded && confirmEpisodeModel(values, type, catalog) === undefined,
  )
  const mismatch = unavailable.map((type) => type + ':' + values?.[type]).join('/')
  const reloadPrefs = prefs.reload

  useEffect(() => {
    if (!mismatch) {
      attempted.current = ''
      return
    }
    if (prefs.loading || loadingCatalog || saving || !current() || attempted.current === mismatch) return
    attempted.current = mismatch
    // 另一台设备可能已选取刚添加的配置。本机目录失配时核对两端，避免错误切换付费账号。
    void loadCatalog(true)
      .catch((e) => {
        if (current()) setSaveError(errorMessage(e))
      })
      .finally(() => {
        if (current() && !writing.current) reloadPrefs()
      })
  }, [mismatch, loadCatalog, loadingCatalog, current, prefs.loading, saving, reloadPrefs])

  const reload = () => {
    attempted.current = ''
    setSaveError('')
    reloadPrefs()
  }
  const ready = enabled && Boolean(values) && loaded && !prefs.loading && !prefs.error && !saving
  const configFor = (type: EpisodeModelType) => {
    if (!ready || !values || writing.current || !current()) return undefined
    return confirmEpisodeModel(values, type, useAIConfigStore.getState().configs)
  }
  const change = async (type: EpisodeModelType, id: number | null) => {
    if (!ready || !current() || writing.current || values?.[type] === id) return
    writing.current = true
    setSaving(true)
    setSaveError('')
    try {
      const updated = await dramaApi.setEpisodeModel(eid, type, id, current)
      if (current()) prefs.setData(() => updated, { invalidatePending: true })
    } catch (e) {
      if (current()) {
        setSaveError('模型选择未确认，正在重新读取本集设置。' + errorMessage(e))
        reloadPrefs()
      }
    } finally {
      writing.current = false
      if (current()) setSaving(false)
    }
  }
  return {
    values,
    ready,
    saving,
    unavailable,
    error: prefs.error || saveError,
    reload,
    change,
    binding: { ready, unavailable, configFor, reload },
  }
}
