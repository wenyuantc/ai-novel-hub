import { Alert, App, Button, Form, Input, Modal, Radio, Space, Typography } from 'antd'
import { useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { sceneApi, sceneBlockers } from '../../api/scenes'
import type { SceneMergePreview } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { utf16ToCodePoint } from '../../utils/textOffset'
import type { TextSelection } from '../chapter/RewritePanel'
import type { SceneDraftController, SceneDraftSnapshot } from './sceneDraftController'
import SceneConflictReview from './SceneConflictReview'

interface Props { projectId: number; chapterId: number; state: SceneDraftSnapshot; controller: SceneDraftController; selection: TextSelection; disabled: boolean; ensureSaved: () => Promise<boolean>; onChanged: () => void }
/** 手动光标拆分、相邻合并和受保护退场；不提供跨章入口。 */
export default function SceneStructureActions({ projectId, chapterId, state, controller, selection, disabled, ensureSaved, onChanged }: Props) {
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(projectId)
  const { message } = App.useApp()
  const [mode, setMode] = useState<'split' | 'merge' | 'delete'>()
  const [working, setWorking] = useState(false)
  const [rightTitle, setRightTitle] = useState('')
  const [preview, setPreview] = useState<SceneMergePreview>()
  const [metadata, setMetadata] = useState<'left' | 'right'>()
  const [error, setError] = useState('')
  const [blockers, setBlockers] = useState<string[]>([])
  const scene = state.scene
  if (!scene) return null
  const next = state.page?.items[state.page.items.findIndex((item) => item.id === scene.id) + 1]
  const cpOffset = utf16ToCodePoint(state.draft.content, selection.start)
  const canSplit = selection.start === selection.end && cpOffset > 0 && cpOffset < Array.from(state.draft.content).length
  const open = async (action: 'split' | 'merge' | 'delete') => {
    if (!canEditNow() || disabled || !(await ensureSaved()) || !canEditNow()) return
    setMode(action); setError(''); setBlockers([]); setPreview(undefined); setMetadata(undefined)
    if (action !== 'merge' || !next) return
    setWorking(true)
    try {
      const current = controller.getSnapshot().scene
      if (!current) return
      const result = await sceneApi.mergePreview(projectId, chapterId, { expected_content_revision: current.content_revision, scene_ids: [current.id, next.id] })
      if (canReadNow()) setPreview(result)
    } catch (err) { if (canReadNow()) { setError(errorMessage(err)); setBlockers(sceneBlockers(err).map((item) => item.label)) } }
    finally { if (canReadNow()) setWorking(false) }
  }
  const submit = async () => {
    const current = controller.getSnapshot().scene
    if (!current || !canEditNow() || working || disabled || state.conflict) return
    setWorking(true); setError('')
    try {
      if (mode === 'split') await sceneApi.split(projectId, chapterId, current.id, { expected_content_revision: current.content_revision, offset: cpOffset, right_title: rightTitle.trim() })
      else if (mode === 'merge' && preview && metadata) await sceneApi.merge(projectId, chapterId, { scene_ids: preview.scene_ids, expected_content_revision: preview.content_revision, expected_planning_revision: preview.planning_revision, signature: preview.signature, metadata_source: metadata })
      else if (mode === 'delete') await sceneApi.remove(projectId, chapterId, current.id, current.content_revision)
      else return
      if (!canEditNow()) return
      setMode(undefined); await controller.load(undefined, false); onChanged(); message.success(mode === 'split' ? '已在指定光标无损拆分，右侧摘要为空，请校对' : mode === 'merge' ? '相邻场景已合并' : '场景已退场，身份与历史保留')
    } catch (err) {
      if (!canReadNow()) return
      setError(errorMessage(err)); setBlockers(sceneBlockers(err).map((item) => item.label))
      if (err instanceof ApiError && err.status === 409) await controller.reviewLatest()
    } finally { if (canReadNow()) setWorking(false) }
  }
  const blocked = mode === 'merge' && (!preview || preview.blockers.length > 0 || !metadata)
  return <>
    <Space wrap><Button size="small" disabled={!canEdit || disabled || !canSplit} onClick={() => void open('split')}>在光标处拆分</Button><Button size="small" disabled={!canEdit || disabled || !next} onClick={() => void open('merge')}>与下一场景合并</Button><Button size="small" danger disabled={!canEdit || disabled || (state.page?.total ?? 1) <= 1} onClick={() => void open('delete')}>移除当前场景</Button></Space>
    <Modal open={!!mode} title={mode === 'split' ? '在指定光标拆分当前场景' : mode === 'merge' ? '合并本章相邻场景' : '移除当前场景？'} onCancel={() => { if (!working) setMode(undefined) }} maskClosable={false}
      footer={<Space><Button disabled={working} onClick={() => setMode(undefined)}>取消</Button><Button type="primary" danger={mode === 'delete'} loading={working} disabled={!canEdit || !!state.conflict || blocked || blockers.length > 0 || (mode === 'split' && !canSplit)} onClick={() => void submit()}>确认{mode === 'split' ? '拆分' : mode === 'merge' ? '合并' : '移除'}</Button></Space>}>
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        {mode === 'split' && <><Typography.Paragraph>按正文第 {cpOffset} 个码点切开，不添加、删除或修剪任何正文。左侧保留原身份和摘要；右侧沿用本地覆盖意图，摘要为空，不推断剧情。</Typography.Paragraph><Form.Item label="右侧场景标题"><Input value={rightTitle} maxLength={200} onChange={(event) => setRightTitle(event.target.value)} /></Form.Item><Typography.Text type="secondary">若存在无法无损分配的演变锚点、支线节点或交汇，将拒绝并要求先处理。</Typography.Text></>}
        {mode === 'merge' && <><Typography.Paragraph>{scene.title || '当前场景'} → {next?.title || '下一场景'}。原文与结构分隔按原顺序无损合并，右侧场景退场。</Typography.Paragraph>{preview && <>
          {preview.blockers.map((item) => <Alert key={`${item.kind}:${item.id}`} type="error" message={item.label} />)}
          {preview.conflicts.map((item) => <Alert key={item.field} type="warning" message={`${item.field} 存在差异`} description={`左：${item.left || '无'}；右：${item.right || '无'}`} />)}
          <Typography.Text strong>明确选择保留哪一侧的标题、摘要及本地规划：</Typography.Text><Radio.Group value={metadata} onChange={(event) => { if (event.target.value === 'left' || event.target.value === 'right') setMetadata(event.target.value) }} options={[{ value: 'left', label: '保留左侧元数据' }, { value: 'right', label: '使用右侧元数据' }]} />
          <Typography.Text type="secondary">此选择不能吞并独立时间事实或支线；中间锚点必须先在原界面处理。</Typography.Text>
        </>}</>}
        {mode === 'delete' && <Alert type="warning" showIcon message="正文将从当前章退出，但保留场景身份与版本历史" description="有演变锚点或独立规划时先拒绝，不会静默移动事实；最后一个场景不能移除，可清空正文后保留。" />}
        {error && <Alert type="error" showIcon message={error} />}{blockers.map((item) => <Typography.Text key={item} type="danger">{item}</Typography.Text>)}
        <SceneConflictReview state={state} controller={controller} disabled={!canEdit || working} />
      </Space>
    </Modal>
  </>
}
