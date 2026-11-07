import { Alert, Button, Form, Input, Modal, Space, Spin, Typography } from 'antd'
import type { TextAreaRef } from 'antd/es/input/TextArea'
import { useEffect, useRef, useState } from 'react'
import type { MentionAnchor } from '../../api/mentions'
import { sceneApi } from '../../api/scenes'
import { errorMessage } from '../../api/client'
import type { Chapter } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { codePointToUtf16 } from '../../utils/textOffset'
import type { TextSelection } from '../chapter/RewritePanel'
import MentionPreview from '../mentions/MentionPreview'
import type { SceneDraftController, SceneDraftSnapshot } from './sceneDraftController'
import SceneConflictReview from './SceneConflictReview'
import SceneNavigator from './SceneNavigator'
import ScenePlanningPanel from './ScenePlanningPanel'
import SceneSettingPreview from './SceneSettingPreview'
import SceneStructureActions from './SceneStructureActions'

interface Props {
  projectId: number; chapter: Chapter; state: SceneDraftSnapshot; controller: SceneDraftController
  disabled: boolean; selection: TextSelection; onSelection: (selection: TextSelection) => void
  ensureSaved: () => Promise<boolean>; onChanged: () => void; onSelect: (sid: number) => Promise<boolean>
  mentionAnchor: MentionAnchor | null
  onStructureDirtyChange: (dirty: boolean) => void
}
/** 当前场景独立编辑；整章仅通读服务端投影，不在浏览器重造正文拼接规则。 */
export default function SceneWritingPanel({ projectId, chapter, state, controller, disabled, selection, onSelection, ensureSaved, onChanged, onSelect, mentionAnchor, onStructureDirtyChange }: Props) {
  const { canEdit, canReadNow } = useProjectAccess(projectId)
  const [reading, setReading] = useState(!!mentionAnchor)
  const [mentions, setMentions] = useState(false)
  const [locating, setLocating] = useState(false)
  const [locationMessage, setLocationMessage] = useState('')
  const input = useRef<TextAreaRef>(null)
  const [focus, setFocus] = useState<TextSelection>()
  const scene = state.scene
  useEffect(() => {
    const el = input.current?.resizableTextArea?.textArea
    if (!focus || !el) return
    el.focus(); el.setSelectionRange(focus.start, focus.end)
  }, [focus])
  const sync = (el: HTMLTextAreaElement) => onSelection({ start: el.selectionStart, end: el.selectionEnd })
  const locate = async () => {
    if (!mentionAnchor || !canReadNow()) return
    setLocating(true); setLocationMessage('')
    try {
      const resolved = await sceneApi.resolvePosition(projectId, chapter.id, mentionAnchor.content_hash, mentionAnchor.start, mentionAnchor.end)
      if (!canReadNow()) return
      if (!resolved.scene_id || resolved.start === null || resolved.end === null) { setLocationMessage(resolved.reason === 'stale' ? '原文或顺序已变化，旧位置已失效，请回提及列表重新定位。' : '该命中跨场景或结构分隔，仅在整章通读中展示，不猜测场景归属。'); return }
      if (!(await onSelect(resolved.scene_id)) || !canReadNow()) return
      const current = controller.getSnapshot().scene
      if (!current || current.id !== resolved.scene_id || current.content_revision !== resolved.content_revision) { setLocationMessage('定位期间原文又有变化，请重新定位。'); return }
      const range = { start: codePointToUtf16(current.content, resolved.start), end: codePointToUtf16(current.content, resolved.end) }
      onSelection(range); setFocus(range); setReading(false)
    } catch (err) { if (canReadNow()) setLocationMessage(errorMessage(err)) }
    finally { if (canReadNow()) setLocating(false) }
  }
  return <Space direction="vertical" size={12} style={{ width: '100%' }}>
    <Space wrap><Button onClick={() => setReading(true)}>整章通读（只读）</Button><Typography.Text type="secondary">正文编辑与摘要仅作用于当前场景</Typography.Text></Space>
    <SceneNavigator projectId={projectId} chapterId={chapter.id} page={state.page} sceneId={scene?.id} disabled={disabled || state.loading} controller={controller} ensureSaved={ensureSaved} onChanged={onChanged} onSelect={onSelect} onDraftChange={onStructureDirtyChange} />
    {state.error && <Alert type="error" showIcon message={state.error} action={<Button disabled={state.saving || disabled} onClick={() => void controller.load()}>重新读取目录（保留草稿）</Button>} />}
    <SceneConflictReview state={state} controller={controller} disabled={!canEdit || disabled} />
    {state.loading && <Spin />}
    {scene && <>
      <Form layout="vertical" disabled={!canEdit || disabled || state.loading}>
        <Form.Item label={`场景 ${scene.sort + 1} 标题`} style={{ marginBottom: 12 }}><Input aria-label="场景标题" value={state.draft.title} maxLength={200} onChange={(event) => controller.change({ title: event.target.value })} /></Form.Item>
        <Form.Item label="作者摘要（独立保存，不自动推断）" style={{ marginBottom: 8 }}><Input.TextArea aria-label="场景摘要" value={state.draft.summary} maxLength={200000} autoSize={{ minRows: 2, maxRows: 6 }} onChange={(event) => controller.change({ summary: event.target.value })} /></Form.Item>
      </Form>
      <ScenePlanningPanel key={`planning:${scene.id}`} projectId={projectId} scene={scene} disabled={disabled || state.loading} ensureSaved={ensureSaved} onChanged={() => { void controller.load(); onChanged() }} />
      <SceneSettingPreview key={`setting:${scene.id}:${scene.content_revision}`} projectId={projectId} chapterId={chapter.id} sceneId={scene.id} />
      <SceneStructureActions key={`structure:${scene.id}`} projectId={projectId} chapterId={chapter.id} state={state} controller={controller} selection={selection} disabled={disabled || state.loading} ensureSaved={ensureSaved} onChanged={onChanged} />
      <Button aria-pressed={mentions} onClick={() => setMentions((old) => !old)}>{mentions ? '返回当前场景编辑' : '查看当前场景提及标记'}</Button>
      {mentions ? <div style={{ padding: 8, fontSize: 16, lineHeight: 1.9 }}><MentionPreview projectId={projectId} text={state.draft.content} draft /></div> : <Input.TextArea ref={input} aria-label="当前场景正文" value={state.draft.content} readOnly={!canEdit || disabled || state.loading} onChange={(event) => { controller.change({ content: event.target.value }); sync(event.target) }} onSelect={(event) => sync(event.currentTarget)} onKeyUp={(event) => sync(event.currentTarget)} onMouseUp={(event) => sync(event.currentTarget)} autoSize={{ minRows: 20 }} placeholder="书写当前场景，或使用 AI 助手生成当前场景" style={{ fontSize: 16, lineHeight: 1.9, resize: 'none' }} />}
    </>}
    <Modal open={reading} title="整章通读 · 只读已保存正文" width={900} footer={<Space wrap>{mentionAnchor && <Button loading={locating} disabled={disabled} onClick={() => void locate()}>定位对应场景并编辑</Button>}<Button onClick={() => setReading(false)}>关闭通读</Button></Space>} onCancel={() => setReading(false)}>
      <Alert type="info" message="按服务端保存的场景顺序通读；当前未保存的正文或摘要不在此预览中。" style={{ marginBottom: 12 }} />
      {locationMessage && <Alert type="warning" showIcon message={locationMessage} style={{ marginBottom: 12 }} />}
      <div style={{ maxHeight: '65vh', overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', fontSize: 16, lineHeight: 1.9 }}><MentionPreview projectId={projectId} text={chapter.content} anchor={mentionAnchor} /></div>
    </Modal>
  </Space>
}
