import { Alert, App, Button, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { captureProjectScope } from '../../stores/project'
import DraftConflictChoices from '../editorDrafts/DraftConflictChoices'
import RichDraftDiff, { DraftTextReview } from '../editorDrafts/RichDraftDiff'
import { verifyDraftScope } from '../editorDrafts/verifyDraftScope'
import type { SceneConflictStamp, SceneDraftController, SceneDraftSnapshot } from './sceneDraftController'

export interface SceneConflictActions {
  pending?: boolean
  canKeepLocal?: boolean
  canUseRemote?: boolean
  onReview: () => Promise<void>
  onResolve: (choice: 'keep-local' | 'use-remote', stamp: SceneConflictStamp) => Promise<boolean>
  onExport?: () => void
}

interface Props {
  projectId: number
  state: SceneDraftSnapshot
  controller: SceneDraftController
  actions?: SceneConflictActions
  disabled?: boolean
}

/** 比较同一场景的当前本地快照与已核对的服务端版本。写入选择由编排层再次检查。 */
export default function SceneConflictReview({ projectId, state, controller, actions, disabled }: Props) {
  const { message } = App.useApp()
  const [checking, setChecking] = useState(false)
  const lifecycle = useRef<AbortController | null>(null)
  useEffect(() => {
    const abort = new AbortController()
    lifecycle.current = abort
    return () => abort.abort()
  }, [])
  const remote = state.conflict
  if (!remote && !state.externalChange && !state.unknownSave) return null
  const pending = state.unknownSave || Boolean(actions?.pending)
  const snapshot: SceneConflictStamp | null = remote ? {
    sceneId: remote.id,
    revision: remote.content_revision,
    planningRevision: remote.planning_revision,
    editSeq: state.editSeq,
  } : null
  const stamp = snapshot ? `${snapshot.sceneId}:${snapshot.revision}:${snapshot.planningRevision}:${snapshot.editSeq}` : ''
  const blocked = disabled || state.loading || state.saving || state.composing || checking
  const canKeepLocal = snapshot && actions && !pending && actions.canKeepLocal !== false && controller.canAcknowledgeLatest()
  const canUseRemote = snapshot && actions && !pending && actions.canUseRemote === true
  const review = async () => {
    if (blocked) return
    setChecking(true)
    try {
      if (actions) await actions.onReview()
      else {
        const signal = lifecycle.current?.signal
        if (await verifyDraftScope(captureProjectScope(projectId), () => !signal?.aborted, signal)) await controller.reviewLatest()
      }
    }
    catch (error) { if (!lifecycle.current?.signal.aborted) message.error(errorMessage(error)) }
    finally { setChecking(false) }
  }
  const copyText = async () => {
    const signal = lifecycle.current?.signal
    const isCurrent = () => !signal?.aborted && controller.getSnapshot().editSeq === state.editSeq
      && controller.getSnapshot().scene?.id === state.scene?.id
    try {
      if (!(await verifyDraftScope(captureProjectScope(projectId), isCurrent, signal)) || !isCurrent()) return
      if (!navigator.clipboard?.writeText) throw new Error('浏览器限制了剪贴板访问，请选中文字手动复制。')
      await navigator.clipboard.writeText(state.draft.content)
      if (isCurrent()) message.success('已复制纯文本正文')
    } catch (error) { if (isCurrent()) message.error(errorMessage(error)) }
  }
  return (
    <Alert type="warning" showIcon style={{ marginBottom: 8 }} message="场景保存需要核对" description={
      <Space direction="vertical" style={{ width: '100%', overflowWrap: 'anywhere' }}>
        {pending && <Typography.Text>上一次保存的结果仍待确认。</Typography.Text>}
        {!remote ? <Button loading={checking} disabled={blocked} onClick={() => void review()}>读取最新场景</Button> : <>
          <Typography.Text>服务端标题：{remote.title || '（空）'}</Typography.Text>
          <Typography.Text>本地标题：{state.draft.title || '（空）'}</Typography.Text>
          {remote.summary !== state.draft.summary && <>
            <Typography.Text strong>场景摘要差异</Typography.Text>
            <DraftTextReview remote={remote.summary} local={state.draft.summary} />
          </>}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>服务端场景版本 {remote.content_revision} · POV {remote.planning.effective.pov_character_name ?? '无'} · 标签 {remote.planning.effective.tags.map((tag) => tag.name).join('、') || '无'} · 引用 {remote.planning.effective.references.map((ref) => `${ref.kind} #${ref.kind === 'relationship' ? ref.timeline_id : ref.id}`).join('、') || '无'}</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>规划来源：{Object.entries(remote.planning.raw.modes).map(([field, mode]) => `${field} ${mode === 'inherit' ? '继承本章' : '本场景覆盖'}`).join('、')}</Typography.Text>
          {state.documentDraft && state.conflictDocument ? (
            <RichDraftDiff remote={state.conflictDocument.document} local={state.documentDraft} />
          ) : <DraftTextReview remote={remote.content} local={state.draft.content} />}
          {actions && !blocked && !pending && !canKeepLocal && <Typography.Text>当前结构差异需要人工整理。你可以下载本地草稿保留资料。</Typography.Text>}
          {canUseRemote && <Typography.Text type="secondary">采用服务端版本后，本地稿会保留在草稿列表。</Typography.Text>}
          <DraftConflictChoices stamp={stamp} disabled={blocked}
            onKeepLocal={canKeepLocal ? () => actions.onResolve('keep-local', snapshot) : undefined}
            onUseRemote={canUseRemote ? () => actions.onResolve('use-remote', snapshot) : undefined} />
          <Button size="small" loading={checking} disabled={blocked} onClick={() => void review()}>重新核对</Button>
        </>}
        {state.scene && <Space wrap>
          <Button size="small" onClick={() => void copyText()}>复制本地正文（纯文本）</Button>
          {actions?.onExport && <Button size="small" onClick={actions.onExport}>下载完整草稿</Button>}
        </Space>}
      </Space>
    } />
  )
}
