import type { FormInstance } from 'antd'
import { App } from 'antd'
import { useLayoutEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { progressionApi } from '../../api/progressions'
import { worldApi } from '../../api/story'
import type { SettingBaseline, WorldCategory, WorldSetting, WorldSettingInput } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { invalidateProject } from '../../utils/projectRefresh'
import { createRecycleRequestGate } from '../../utils/recycleBin'
import { hasWorldContentDraft, toWorldBaselinePatch, worldBaselineMatches, worldFormValues, worldMetadataMatches, type WorldFormValues } from './worldForm'
import { readConsistentWorldSnapshot } from './worldSnapshot'

/** 世界观基础草稿独立于最新投影；切换目标、身份或关闭后旧响应失效。 */
export function useWorldBaselineEditor(projectId: number, form: FormInstance<WorldFormValues>) {
  const { message } = App.useApp()
  const { canRead, canReadNow, canEditNow } = useProjectAccess(projectId)
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const [editing, setEditing] = useState<WorldSetting | null>(null)
  const [open, setOpen] = useState(false)
  const [openScope, setOpenScope] = useState('')
  const scope = `${projectId}:${identity}`
  const [saving, setSaving] = useState(false)
  const [baseline, setBaseline] = useState<SettingBaseline | null>(null)
  const [baselineLoading, setBaselineLoading] = useState(false)
  const [baselineError, setBaselineError] = useState('')
  const [baselineConflict, setBaselineConflict] = useState(false)
  const session = useRef(createRecycleRequestGate())
  const loads = useRef(createRecycleRequestGate())
  const activeTicket = useRef<number | null>(null)
  const itemRef = useRef<WorldSetting | null>(null)
  const initialRef = useRef<WorldFormValues | null>(null)
  const baselineRef = useRef<SettingBaseline | null>(null)
  const blocked = useRef(false)
  const savingRef = useRef(false)
  const abortRef = useRef<AbortController | null>(null)

  useLayoutEffect(() => {
    const editorGate = session.current
    const loadGate = loads.current
    return () => {
      editorGate.invalidate()
      loadGate.invalidate()
      abortRef.current?.abort()
      activeTicket.current = null
    }
  }, [projectId, identity])

  const current = (ticket: number) => session.current.isCurrent(ticket) && activeTicket.current === ticket && canReadNow()

  const loadBaseline = async (ticket: number, item: WorldSetting, mode: 'replace' | 'reconcile') => {
    if (!current(ticket)) return
    const loadTicket = loads.current.begin()
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    blocked.current = true
    setBaselineLoading(true)
    setBaselineError('')
    const stillLoading = () => current(ticket) && loads.current.isCurrent(loadTicket) && !controller.signal.aborted
    try {
      const { baseline: next, item: latest } = await readConsistentWorldSnapshot(projectId, item.id, controller.signal)
      if (!stillLoading()) return
      const initial = initialRef.current
      if (!initial || typeof next.baseline.content !== 'string') throw new Error('基础设定内容不完整，请刷新后重试')
      if (mode === 'reconcile' && (!worldBaselineMatches(initial, next.baseline) || !worldMetadataMatches(initial, latest))) {
        setBaselineConflict(true)
        setBaselineError('基础设定或条目信息已在其他位置修改，当前草稿已保留。请复制需要的内容后重新读取。')
        return
      }
      if (mode === 'replace') {
        const replacement = worldFormValues(latest, next.baseline.content)
        form.setFieldsValue(replacement)
        initialRef.current = replacement
        itemRef.current = latest
        setEditing(latest)
      }
      baselineRef.current = next
      setBaseline(next)
      setBaselineConflict(false)
      blocked.current = false
    } catch (err) {
      if (stillLoading()) setBaselineError(errorMessage(err))
    } finally {
      if (stillLoading()) setBaselineLoading(false)
    }
  }

  const openEditor = (item: WorldSetting | null, defaultCategory: WorldCategory) => {
    abortRef.current?.abort()
    loads.current.invalidate()
    const ticket = session.current.begin()
    activeTicket.current = ticket
    itemRef.current = item
    baselineRef.current = null
    savingRef.current = false
    blocked.current = !!item
    setSaving(false)
    setBaseline(null)
    setBaselineError('')
    setBaselineConflict(false)
    setBaselineLoading(!!item)
    setEditing(item)
    const initial = item ? worldFormValues(item, '') : { title: '', category: defaultCategory, content: '', sort: null, aliases: [] }
    initialRef.current = initial
    form.setFieldsValue(initial)
    setOpenScope(scope)
    setOpen(true)
    if (item) void loadBaseline(ticket, item, 'replace')
  }

  const closeEditor = () => {
    session.current.invalidate()
    loads.current.invalidate()
    abortRef.current?.abort()
    activeTicket.current = null
    savingRef.current = false
    setSaving(false)
    setOpen(false)
  }

  const reloadBaseline = (discardContentDraft = false) => {
    const ticket = activeTicket.current
    const item = itemRef.current
    if (ticket === null || !item || !current(ticket)) return
    const mode = discardContentDraft || !baselineRef.current ? 'replace' : 'reconcile'
    void loadBaseline(ticket, item, mode)
  }

  const save = async (seriesManaged = false) => {
    const ticket = activeTicket.current
    if (ticket === null || !current(ticket) || !canEditNow() || savingRef.current || blocked.current) return
    savingRef.current = true
    setSaving(true)
    try {
      let values: WorldFormValues
      try { values = await form.validateFields() } catch { return }
      if (!current(ticket) || !canEditNow()) return
      const item = itemRef.current
      let categoryChanged = false
      if (item) {
        const base = baselineRef.current
        const initial = initialRef.current
        if (!base || !initial) return
        const managed = seriesManaged || item.origin?.scope === 'series'
        const patch = toWorldBaselinePatch(managed ? { ...initial, sort: values.sort } : values, initial, base.timeline_revision)
        categoryChanged = patch?.category !== undefined
        if (patch) await progressionApi.updateWorldBaseline(projectId, item.id, patch)
      } else {
        const body: WorldSettingInput = {
          title: values.title.trim(), content: values.content ?? '', category: values.category ?? 'other', sort: values.sort ?? undefined, aliases: values.aliases ?? [],
        }
        await worldApi.create(projectId, body)
      }
      if (!current(ticket) || !canEditNow()) return
      message.success('已保存')
      closeEditor()
      invalidateProject(projectId, ['world', ...(categoryChanged ? ['setting-ai-visibility' as const] : [])])
    } catch (err) {
      if (!current(ticket) || !canEditNow()) return
      if (err instanceof ApiError && err.status === 409 && err.body?.code === 'stale_revision') {
        blocked.current = true
        setBaselineError('设定已在其他位置更新，当前草稿已保留。请先刷新基础设定再保存。')
      } else {
        message.error(errorMessage(err))
      }
    } finally {
      if (current(ticket)) { savingRef.current = false; setSaving(false) }
    }
  }

  const progressionChanged = (revision: number) => {
    const ticket = activeTicket.current
    const item = itemRef.current
    if (ticket !== null && item && current(ticket) && baselineRef.current?.timeline_revision !== revision) {
      void loadBaseline(ticket, item, 'reconcile')
    }
  }

  const confirmBaseline = async () => {
    const ticket = activeTicket.current
    const base = baselineRef.current
    const initial = initialRef.current
    if (ticket === null || !base || !initial || !current(ticket) || !canEditNow() || blocked.current || savingRef.current) return
    if (hasWorldContentDraft(form.getFieldsValue(true), initial)) {
      message.warning('请先保存修改后的基础内容，再确认校准')
      return
    }
    const loadTicket = loads.current.begin()
    abortRef.current?.abort()
    blocked.current = true
    setBaselineLoading(true)
    const stillLoading = () => current(ticket) && loads.current.isCurrent(loadTicket) && canEditNow()
    try {
      const next = await progressionApi.updateBaseline(projectId, base.timeline_id, {
        expected_revision: base.timeline_revision, changes: {}, confirm: true,
      })
      if (!stillLoading()) return
      if (!worldBaselineMatches(initial, next.baseline)) {
        setBaselineConflict(true)
        setBaselineError('基础设定已变化，当前草稿已保留。请重新读取后检查。')
        return
      }
      baselineRef.current = next
      setBaseline(next)
      setBaselineError('')
      blocked.current = false
      message.success('基础设定已确认')
      invalidateProject(projectId, ['world'])
    } catch (err) {
      if (!stillLoading()) return
      if (err instanceof ApiError && err.status === 409 && err.body?.code === 'stale_revision') {
        setBaselineError('设定已在其他位置更新，当前草稿已保留。请先刷新基础设定再确认。')
      } else {
        setBaselineError(errorMessage(err))
      }
    } finally {
      if (stillLoading()) setBaselineLoading(false)
    }
  }

  return {
    editing, open: open && openScope === scope && canRead, saving, baseline, baselineLoading, baselineError, baselineConflict,
    openEditor, closeEditor, save, reloadBaseline, progressionChanged, confirmBaseline,
  }
}
