import { App } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { bookSearchApi, type ApplyRevision, type HitPage, type SearchHit, type SelectedHit } from '../../api/bookSearch'
import type { EditorActorIdentity } from '../../api/client'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { invalidateMismatchedEditorActor } from '../editorDrafts/verifyDraftScope'
import { createIntentKey } from '../../utils/novelTemplateCreation'
import { invalidateProject } from '../../utils/projectRefresh'
import { asApplication, changeSelection, describeError, MAX_SELECTED, type OperationError, type SearchMode } from './selection'

export function useBookSearch(projectId: number, actor: EditorActorIdentity) {
  const { message } = App.useApp()
  const { canReadNow, canEditNow } = useProjectAccess(projectId)
  const [mode, setMode] = useState<SearchMode | null>(null)
  const [result, setResult] = useState<HitPage | null>(null)
  const [selected, setSelected] = useState(new Map<string, SelectedHit>())
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<OperationError | null>(null)
  const [retry, setRetry] = useState<ApplyRevision | null>(null)
  const read = useRef<AbortController | null>(null)
  const writing = useRef(false)
  useEffect(() => () => read.current?.abort(), [])

  const reset = useCallback(() => {
    read.current?.abort()
    setLoading(false)
    setMode(null)
    setResult(null)
    setSelected(new Map())
    setRetry(null)
    setError(null)
  }, [])

  // 若写回执未知，保留原请求；重试同一请求才能查明该批是否已经提交。
  useProjectRefresh(projectId, ['chapters'], () => {
    if (writing.current || retry || !mode) return
    reset()
    setError({ text: '章节已更新，请重新查找或预览。', conflicts: [], conflict: true, unknown: false })
  })

  const load = async (nextMode: SearchMode, page = 1, snapshot?: string) => {
    const allowed = nextMode.replacement === undefined ? canReadNow : canEditNow
    if (!allowed() || writing.current || retry) return
    read.current?.abort()
    const controller = new AbortController()
    read.current = controller
    const current = () => !controller.signal.aborted && allowed()
    setError(null)
    if (snapshot === undefined) {
      setSelected(new Map())
      setResult(null)
    }
    setMode(nextMode)
    setLoading(true)
    try {
      const body = { ...nextMode, page, page_size: 20, snapshot }
      const data = nextMode.replacement === undefined
        ? await bookSearchApi.query(projectId, body, controller.signal, current)
        : await bookSearchApi.preview(projectId, { ...body, replacement: nextMode.replacement }, controller.signal, current)
      if (current()) setResult(data)
    } catch (err) {
      if (!current()) return
      const failure = describeError(err)
      if (failure.conflict || snapshot === undefined) {
        setResult(null)
        setSelected(new Map())
      }
      setError(failure)
    } finally {
      if (current()) setLoading(false)
    }
  }

  const select = (items: SearchHit[], checked: boolean) => {
    if (writing.current || retry || loading || !canEditNow()) return
    const next = changeSelection(selected, items, checked)
    if (next.size > MAX_SELECTED) {
      message.warning(`每批最多选择 ${MAX_SELECTED} 处，请分批修订`)
      return
    }
    setSelected(next)
  }

  const apply = async () => {
    if (writing.current || loading || !mode || !result || !canEditNow()) return
    let body: ApplyRevision
    try { body = retry ?? asApplication(mode, selected, createIntentKey()) }
    catch (err) { setError(describeError(err)); return }
    writing.current = true
    setSaving(true)
    setError(null)
    try {
      const batch = await bookSearchApi.apply(projectId, body, actor, canEditNow)
      if (!canEditNow()) return
      reset()
      invalidateProject(projectId, ['chapters'])
      message.success(`已确认修订记录：${batch.chapter_count} 章、${batch.hit_count} 处（${batch.status === 'undone' ? '已撤销' : '已应用'}）`)
    } catch (err) {
      invalidateMismatchedEditorActor(err, canEditNow)
      if (!canEditNow()) return
      const failure = describeError(err)
      if (failure.unknown) {
        setRetry(body)
      } else {
        setRetry(null)
        if (failure.conflict) {
          setSelected(new Map())
          setResult(null)
        }
      }
      setError(failure)
    } finally {
      writing.current = false
      if (canEditNow()) setSaving(false)
    }
  }
  return { mode, result, selected, loading, saving, error, retry, reset, load, select, apply,
    clearSelection: () => setSelected(new Map()) }
}
