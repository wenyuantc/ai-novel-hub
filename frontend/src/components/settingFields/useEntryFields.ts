import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { chapterApi } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import { settingFieldsApi } from '../../api/settingFields'
import type { ChapterBrief, SettingEntryFields, SettingEntryRef } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { entryKey } from './valueDraft'

const identityNow = () => {
  const state = useAuthStore.getState()
  return `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`
}

/** 刷新只替换读取快照；编辑弹窗持有自己的起始快照和草稿。 */
export function useEntryFields(projectId: number, entry: SettingEntryRef) {
  const access = useProjectAccess(projectId)
  const { canReadNow } = access
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const kind = entry.kind, id = entry.id
  const key = `${identity}:${projectId}:${entryKey(entry)}`
  const actorScope = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}`) + `:${projectId}:${entryKey(entry)}`
  const [version, setVersion] = useState(0)
  const [state, setState] = useState<{ key: string; actorScope: string; version: number; data: SettingEntryFields | null; chapters: ChapterBrief[]; error: string | null } | null>(null)
  const scope = useRef('')
  const sequence = useRef(0)
  const requests = useRef(new Set<AbortController>())
  const refresh = useCallback(() => setVersion((value) => value + 1), [])
  useLayoutEffect(() => {
    const pending = requests.current
    scope.current = key
    return () => { scope.current = ''; sequence.current += 1; pending.forEach((controller) => controller.abort()); pending.clear() }
  }, [key])
  useProjectRefresh(projectId, ['setting_fields', 'setting-ai-visibility', 'characters', 'world', 'organizations', 'careers', 'chapters'], refresh)
  const isCurrent = useCallback(() => scope.current === key && identityNow() === identity && canReadNow(), [key, identity, canReadNow])
  useEffect(() => {
    if (!access.canRead) return
    const controller = new AbortController(), ticket = ++sequence.current
    const pending = requests.current
    pending.add(controller)
    void Promise.all([settingFieldsApi.getEntryFields(projectId, { kind, id }, null, controller.signal), chapterApi.list(projectId)]).then(([data, chapters]) => {
      if (!controller.signal.aborted && ticket === sequence.current && isCurrent()) setState({ key, actorScope, version, data, chapters, error: null })
    }).catch((error: unknown) => {
      if (!controller.signal.aborted && ticket === sequence.current && isCurrent()) setState((previous) => ({ key, actorScope, version, data: previous?.actorScope === actorScope ? previous.data : null, chapters: previous?.actorScope === actorScope ? previous.chapters : [], error: errorMessage(error) }))
    }).finally(() => pending.delete(controller))
    return () => { controller.abort(); pending.delete(controller) }
  }, [projectId, kind, id, key, actorScope, version, access.canRead, isCurrent])
  const current = access.canRead && state?.actorScope === actorScope ? state : null
  const readLatest = async () => {
    if (!isCurrent()) throw new Error('权限或当前条目已变化，请重新打开')
    const controller = new AbortController(), ticket = ++sequence.current
    requests.current.add(controller)
    try {
      const data = await settingFieldsApi.getEntryFields(projectId, { kind, id }, null, controller.signal)
      if (controller.signal.aborted || ticket !== sequence.current || !isCurrent()) throw new Error('当前条目已切换')
      setState((previous) => ({ key, actorScope, version, data, chapters: previous?.actorScope === actorScope ? previous.chapters : [], error: null }))
      return data
    } finally { requests.current.delete(controller) }
  }
  const run = async <T,>(write: (signal: AbortSignal) => Promise<T>) => {
    if (!isCurrent() || !access.canEditNow()) throw new Error('当前条目不可修改，请重新打开')
    const controller = new AbortController()
    requests.current.add(controller)
    try {
      const result = await write(controller.signal)
      if (controller.signal.aborted || !isCurrent() || !access.canEditNow()) throw new Error('权限或当前条目已变化')
      return result
    } finally { requests.current.delete(controller) }
  }
  return { ...access, key, data: current?.data ?? null, chapters: current?.chapters ?? [], error: current?.error ?? null,
    loading: access.canRead && (!current || current.key !== key || current.version !== version), refresh, readLatest, run, isCurrent }
}
