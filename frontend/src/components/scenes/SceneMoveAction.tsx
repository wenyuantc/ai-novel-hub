import { Alert, Button, Collapse, Form, Modal, Select, Space, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { chapterApi } from '../../api/chapters'
import { ApiError } from '../../api/client'
import { sceneMoveApi } from '../../api/sceneStructure'
import { readChapterScenes, sceneBlockers } from '../../api/scenes'
import type { SceneMoveChapter, SceneMovePreview, SceneMoveSelection, ScenePlanningField } from '../../api/types'
import { useIsMobile, useModalWidth } from '../../hooks/useIsMobile'
import { useLoader } from '../../hooks/useLoader'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { sceneLabel } from './scenePresentation'
import type { SceneDraftController } from './sceneDraftController'
import { runAfterSceneSave, type RunSavedSceneAction } from './sceneSavedAction'
import { locateMoveAfterDisconnect, matchesMovePreview } from './sceneStructureState'
import { useSceneStructureRequest } from './useSceneStructureRequest'

interface Props {
  projectId: number
  chapterId: number
  sceneId: number
  disabled: boolean
  controller: SceneDraftController
  ensureSaved: () => Promise<boolean>
  runSavedAction?: RunSavedSceneAction
  onMoved: (target: SceneMoveChapter, sceneId: number) => Promise<void>
}

const PLANNING_LABELS: Record<ScenePlanningField, string> = {
  pov: '视角人物', tags: '标签', character: '出场角色', world: '世界观',
  organization: '组织', career: '职业', relationship: '关系',
}

function chapterLabel(chapter: SceneMoveChapter, position: number) {
  return `第 ${chapter.chapter_no} 章 · ${chapter.title || '未命名'} · 场景 ${position}`
}

function Excerpt({ label, value }: { label: string; value: string }) {
  return <div style={{ minWidth: 0 }}>
    <Typography.Text type="secondary">{label}</Typography.Text>
    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginBottom: 4 }}>
      {value || '无'}
    </Typography.Paragraph>
  </div>
}

function MovePreview({ value }: { value: SceneMovePreview }) {
  return (
    <Space direction="vertical" size={10} style={{ display: 'flex' }}>
      <div style={{ overflowWrap: 'anywhere' }}>
        <div>原位置 · {chapterLabel(value.source, value.source_position)} · {value.story_date_before}</div>
        <div>新位置 · {chapterLabel(value.target, value.target_position)} · {value.story_date_after}</div>
      </div>
      {value.source_will_be_empty && <Alert type="info" showIcon message="源章移出最后一个场景后会创建空场景。" />}
      {value.planning_changes.length > 0 && <div>
        <Typography.Text strong>随目标章继承的规划变化</Typography.Text>
        <ul style={{ paddingInlineStart: 20, overflowWrap: 'anywhere' }}>
          {value.planning_changes.map((change) => <li key={change.field}>
            {PLANNING_LABELS[change.field]}：{change.before.join('、') || '无'} → {change.after.join('、') || '无'}
          </li>)}
        </ul>
      </div>}
      {value.timeline_events.length > 0 && <div>
        <Typography.Text strong>随场景移动的设定变化</Typography.Text>
        <ul style={{ paddingInlineStart: 20, overflowWrap: 'anywhere' }}>
          {value.timeline_events.map((event) => <li key={event.id}>{event.label}（场景{event.boundary === 'start' ? '开头' : '结尾'}）</li>)}
        </ul>
      </div>}
      <Collapse size="small" defaultActiveKey={['target']} items={[
        { key: 'target', label: '目标章插入接缝', children: <>
          <Excerpt label="插入点前" value={value.target_seam.before} />
          <Excerpt label="移入场景开头" value={value.moved_start} />
          <Excerpt label="移入场景结尾" value={value.moved_end} />
          <Excerpt label="插入点后" value={value.target_seam.after} />
        </> },
        { key: 'source', label: '源章移出后的接缝', children: <>
          <Excerpt label="接缝前" value={value.source_seam.before} />
          <Excerpt label="接缝后" value={value.source_seam.after} />
        </> },
      ]} />
      {value.blockers.map((item) => <Alert key={`${item.kind}:${item.id}`} type="warning" showIcon message={item.label} />)}
    </Space>
  )
}

/** 选择同书目标章节、核对双章快照并确认移动。 */
export default function SceneMoveAction(props: Props) {
  const { canEdit } = useProjectAccess(props.projectId)
  const [open, setOpen] = useState(false)
  if (!canEdit) return null
  return <>
    <Button size="small" disabled={props.disabled} onClick={() => setOpen(true)}>跨章移动</Button>
    {open && <MoveDialog {...props} onClose={() => setOpen(false)} />}
  </>
}

function MoveDialog({ projectId, chapterId, sceneId, disabled, controller, ensureSaved, runSavedAction, onMoved, onClose }: Props & { onClose: () => void }) {
  const { canEditNow } = useProjectAccess(projectId)
  const mobile = useIsMobile()
  const width = useModalWidth(640)
  const [targetChapter, setTargetChapter] = useState<number>()
  const [beforeScene, setBeforeScene] = useState<number | null>(null)
  const [preview, setPreview] = useState<SceneMovePreview>()
  const [unknown, setUnknown] = useState<SceneMovePreview>()
  const [blockers, setBlockers] = useState<string[]>([])
  const request = useSceneStructureRequest(canEditNow)
  const loadChapters = useCallback(() => chapterApi.list(projectId, undefined, canEditNow), [projectId, canEditNow])
  const chapters = useLoader(loadChapters)
  const loadScenes = useCallback(async () => targetChapter === undefined ? null : {
    chapterId: targetChapter, page: await readChapterScenes(projectId, targetChapter, undefined, canEditNow),
  }, [projectId, targetChapter, canEditNow])
  const positions = useLoader(loadScenes)
  const targetChoices = (chapters.data ?? []).filter((chapter) => chapter.id !== chapterId)
  const currentChoices = positions.data?.chapterId === targetChapter ? positions.data?.page.items : undefined
  const positionLoading = targetChapter !== undefined && !positions.error && (positions.loading || !currentChoices)

  const selectTarget = (id: number) => {
    setTargetChapter(id)
    setBeforeScene(null)
    setPreview(undefined)
    setBlockers([])
    request.setError('')
  }

  const loadPreview = () => {
    if (targetChapter === undefined || !currentChoices || disabled || request.busy) return
    const selection: SceneMoveSelection = { target_chapter_id: targetChapter, insert_before_scene_id: beforeScene }
    setPreview(undefined)
    setBlockers([])
    return request.run(async (options) => {
      const loaded = await runAfterSceneSave(runSavedAction, ensureSaved, async () => {
        const scene = controller.getSnapshot().scene
        if (!options.isCurrent() || scene?.id !== sceneId || scene.chapter_id !== chapterId) return false
        const fresh = await sceneMoveApi.preview(projectId, chapterId, sceneId, selection, options)
        if (!options.isCurrent()) return false
        if (!matchesMovePreview(fresh, scene, selection)) {
          await controller.load()
          throw new Error('源场景已变化，请核对最新稿件后重新预览。')
        }
        setPreview(fresh)
        return true
      })
      if (!loaded && options.isCurrent()) request.setError('请先确认输入法文字并保存当前场景，再重新预览。')
    }, (error) => setBlockers(sceneBlockers(error).map((item) => item.label)))
  }

  const submit = () => {
    if (!preview || preview.blockers.length || disabled || request.busy) return
    const reviewed = preview
    const selection: SceneMoveSelection = {
      target_chapter_id: reviewed.target.chapter_id, insert_before_scene_id: reviewed.insert_before_scene_id,
    }
    let sent = false
    setBlockers([])
    return request.run(async (options) => {
      const moved = await runAfterSceneSave(runSavedAction, ensureSaved, async () => {
        const scene = controller.getSnapshot().scene
        if (!options.isCurrent()) return false
        if (!scene || !matchesMovePreview(reviewed, scene, selection)) {
          setPreview(undefined)
          throw new Error('保存后的场景版本已变化，请重新预览。')
        }
        sent = true
        const result = await sceneMoveApi.move(projectId, chapterId, sceneId, { ...selection, signature: reviewed.signature }, options)
        if (!options.isCurrent()) return false
        if (result.scene_id !== sceneId || result.source_chapter_id !== chapterId
          || result.target_chapter_id !== selection.target_chapter_id
          || result.source_content_revision <= reviewed.source.content_revision
          || result.target_content_revision <= reviewed.target.content_revision) {
          throw new Error('移动回执与所选场景不一致。')
        }
        await onMoved(reviewed.target, result.scene_id)
        onClose()
        return true
      })
      if (!moved && options.isCurrent()) request.setError('请先核对或保存当前草稿。')
    }, (error) => {
      setPreview(undefined)
      setBlockers(sceneBlockers(error).map((item) => item.label))
      if (sent && (!(error instanceof ApiError) || error.status === 0 || error.status >= 500)) {
        setUnknown(reviewed)
        request.setError('移动结果未知，请重新连接后核对两章的场景归属。')
      } else {
        if (error instanceof ApiError && error.status === 404) {
          setTargetChapter(undefined)
          setBeforeScene(null)
        }
        chapters.reload()
        positions.reload()
        if (error instanceof ApiError && [404, 409].includes(error.status)) void controller.load()
      }
    })
  }

  const checkResult = () => {
    if (!unknown) return
    return request.run(async ({ signal, isCurrent }) => {
      const [source, target] = await Promise.all([
        readChapterScenes(projectId, chapterId, signal, isCurrent),
        readChapterScenes(projectId, unknown.target.chapter_id, signal, isCurrent),
      ])
      if (!isCurrent()) return
      const location = locateMoveAfterDisconnect(sceneId, source, target)
      if (location === 'target') {
        await onMoved(unknown.target, sceneId)
        onClose()
      } else if (location === 'source') {
        setUnknown(undefined)
        request.setError('场景仍在源章。继续移动前请重新选择位置并预览。')
        positions.reload()
        await controller.load()
      } else {
        request.setError('两章读取结果暂不能确认场景归属，请刷新目录后再核对。')
      }
    })
  }

  const selectDisabled = request.busy || disabled || !!unknown
  return <Modal open title="跨章移动场景" width={width} centered maskClosable={false}
    closable={!request.busy} keyboard={!request.busy} onCancel={request.busy ? undefined : onClose}
    styles={{ body: { maxHeight: 'calc(100dvh - 190px)', overflowY: 'auto' } }}
    footer={<Space wrap>
      <Button disabled={request.busy} onClick={onClose}>关闭</Button>
      {unknown ? <Button type="primary" loading={request.busy} onClick={() => void checkResult()}>核对移动结果</Button>
        : preview ? <Button type="primary" loading={request.busy} disabled={disabled || preview.blockers.length > 0}
          onClick={() => void submit()}>确认移动</Button>
          : <Button type="primary" loading={request.busy} disabled={selectDisabled || !currentChoices || positionLoading || !!positions.error}
            onClick={() => void loadPreview()}>预览移动</Button>}
    </Space>}>
    <Form layout="vertical">
      <Form.Item label="目标章节" htmlFor="scene-move-chapter">
        <Select id="scene-move-chapter" placeholder="选择同书的另一章" showSearch optionFilterProp="label"
          size={mobile ? 'large' : 'middle'} value={targetChapter} onChange={selectTarget}
          disabled={selectDisabled || chapters.loading} loading={chapters.loading}
          options={targetChoices.map((chapter) => ({
            value: chapter.id, label: `第 ${chapter.chapter_no} 章 · ${chapter.title || '未命名'}`,
          }))} />
      </Form.Item>
      {chapters.error && <Alert type="error" showIcon message={chapters.error}
        action={<Button size="small" onClick={chapters.reload}>重新读取</Button>} />}
      {!chapters.error && !chapters.loading && !targetChoices.length && <Alert type="info" showIcon message="同书暂无其他章节。新建章节后即可移动。" />}
      {targetChapter !== undefined && <Form.Item label="插入位置" htmlFor="scene-move-position">
        <Select<number | 'end'> id="scene-move-position" size={mobile ? 'large' : 'middle'}
          value={beforeScene ?? 'end'} loading={positionLoading} disabled={selectDisabled || !currentChoices || positionLoading}
          onChange={(id) => {
            setBeforeScene(id === 'end' ? null : id)
            setPreview(undefined)
            setBlockers([])
            request.setError('')
          }}
          options={[{ value: 'end', label: '章末' }, ...(currentChoices ?? []).map((item) => ({
            value: item.id, label: `插到${sceneLabel(item)}之前`,
          }))]} />
      </Form.Item>}
      {targetChapter !== undefined && positions.error && <Alert type="warning" showIcon message={positions.error}
        action={<Button size="small" onClick={positions.reload}>重读场景</Button>} />}
      <Space direction="vertical" size={8} style={{ display: 'flex' }}>
        {preview && <MovePreview value={preview} />}
        {request.error && <Alert type="warning" showIcon message={request.error} />}
        {blockers.map((label) => <Alert key={label} type="warning" message={label} />)}
      </Space>
    </Form>
  </Modal>
}
