import { Alert, App, Button, Collapse, Form, Input, Modal, Progress, Radio, Select, Space, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { sceneRestructureApi } from '../../api/sceneStructure'
import { sceneBlockers } from '../../api/scenes'
import { hasTaskIdentity, queueLabel, taskApi } from '../../api/tasks'
import type { SceneRestructureOperation, SceneRestructurePreview, SceneRestructureResult, Task } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useTaskStream } from '../../hooks/useTaskStream'
import { captureProjectScope } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { verifyDraftScope } from '../editorDrafts/verifyDraftScope'
import type { SceneDraftController, SceneDraftSnapshot } from './sceneDraftController'
import { sceneConflictLabels, sceneLabel } from './scenePresentation'
import { runAfterSceneSave, type RunSavedSceneAction } from './sceneSavedAction'
import { matchesRestructureScope, restructureRequest } from './sceneStructureState'
import { useSceneStructureRequest } from './useSceneStructureRequest'

interface Props {
  projectId: number
  chapterId: number
  state: SceneDraftSnapshot
  controller: SceneDraftController
  disabled: boolean
  hasModel: boolean | null
  ensureSaved: () => Promise<boolean>
  runSavedAction?: RunSavedSceneAction
  taskKey?: string
  onCloseTask: () => void
  onApplied: (sceneId: number) => Promise<void>
}

function SavedExcerpt({ text }: { text: string }) {
  return <Typography.Paragraph style={{
    whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 200, overflowY: 'auto', marginBottom: 4,
  }}>{text || '（空）'}</Typography.Paragraph>
}

/** 在章节编辑器内发起并核对只读建议，采纳始终经过现有保存屏障。 */
export default function SceneRestructureAction(props: Props) {
  const { canEdit, canUseAI } = useProjectAccess(props.projectId)
  const [open, setOpen] = useState(false)
  if (!canEdit || !canUseAI) return null
  const close = () => {
    setOpen(false)
    if (props.taskKey) props.onCloseTask()
  }
  return <>
    <Button size="small" disabled={props.disabled} onClick={() => setOpen(true)}>AI 拆并建议</Button>
    {(open || props.taskKey) && <SuggestDialog key={props.taskKey ?? 'new'} {...props} onClose={close}
      onApplied={async (sceneId) => { setOpen(false); await props.onApplied(sceneId) }} />}
  </>
}

function SuggestDialog({
  projectId, chapterId, state, controller, disabled, hasModel, ensureSaved, runSavedAction, taskKey, onApplied, onClose,
}: Props & { onClose: () => void }) {
  const { canEditNow, canUseAINow } = useProjectAccess(projectId)
  const canRequest = useCallback(() => canEditNow() && canUseAINow(), [canEditNow, canUseAINow])
  const { run, error, setError, busy } = useSceneStructureRequest(canRequest)
  const { follow, detach, cancel, task, running, reconnecting } = useTaskStream()
  const track = useTasksStore((store) => store.track)
  const { message } = App.useApp()
  const width = useModalWidth(680)
  const [operation, setOperation] = useState<SceneRestructureOperation>('split')
  const [neighbor, setNeighbor] = useState<number>()
  const [preview, setPreview] = useState<SceneRestructurePreview>()
  const [following, setFollowing] = useState<Task>()
  const [loaded, setLoaded] = useState<{ key: string; result: SceneRestructureResult }>()
  const [candidate, setCandidate] = useState<number>()
  const [rightTitle, setRightTitle] = useState('')
  const [metadata, setMetadata] = useState<'left' | 'right'>()
  const [blockers, setBlockers] = useState<string[]>([])
  const [applyBlocked, setApplyBlocked] = useState(false)
  const stopping = useRef(false)
  const [cancelling, setCancelling] = useState(false)
  const sceneId = state.scene?.id
  const currentIndex = state.page?.items.findIndex((item) => item.id === sceneId) ?? -1
  const neighbors = currentIndex < 0 ? [] : [state.page?.items[currentIndex - 1], state.page?.items[currentIndex + 1]]
    .flatMap((item) => item ? [{ value: item.id, label: sceneLabel(item) }] : [])
  const readingTask = !!taskKey && !loaded && !following
  const result = loaded?.result
  const freshResult = !!(result && state.scene && matchesRestructureScope(result, state.scene, state.page))
  const resultLabels = result?.scene_ids.flatMap((id) => {
    const item = state.page?.items.find((scene) => scene.id === id)
    return item ? [sceneLabel(item)] : []
  })

  const readResult = useCallback((key: string, options: Parameters<typeof sceneRestructureApi.result>[3]) =>
    sceneRestructureApi.result(projectId, chapterId, key, options).then((result) => {
      if (options.isCurrent()) {
        setLoaded({ key, result })
        setCandidate(undefined)
        setPreview(undefined)
        setApplyBlocked(false)
      }
    }), [projectId, chapterId])

  useEffect(() => {
    let active = true
    // 挂载到明确的章节后再认领属于当前账户和章节的可见任务。
    queueMicrotask(() => {
      if (!active) return
      void run(async (options) => {
        const isCurrent = () => active && options.isCurrent()
        const scope = captureProjectScope(projectId)
        if (!(await verifyDraftScope(scope, isCurrent, options.signal))) {
          if (isCurrent()) setError('无法核对当前连接或作品权限，请刷新章节后再查看建议。')
          return
        }
        if (!isCurrent()) return
        const tasks = await taskApi.list({ project_id: projectId, chapter_id: chapterId, kind: 'scene.restructure', active: true }, { ...options, isCurrent })
        if (!isCurrent()) return
        const selected = tasks.find((item) => hasTaskIdentity(item) && item.project_id === projectId
          && item.chapter_id === chapterId && item.kind === 'scene.restructure'
          && (taskKey ? taskKey === item.task_key : item.scene_id === sceneId))
        if (selected) setFollowing(selected)
        else if (taskKey) await readResult(taskKey, { ...options, isCurrent })
      })
    })
    return () => { active = false }
  }, [projectId, chapterId, sceneId, taskKey, readResult, run, setError])

  useEffect(() => {
    if (!hasTaskIdentity(following)) return
    let mounted = true
    void follow(following).then((outcome) => {
      if (!mounted || !canRequest() || outcome.status === 'detached') return
      if ('task' in outcome && outcome.task) track(outcome.task)
      setFollowing(undefined)
      if (outcome.status === 'succeeded') {
        if (outcome.unpublished) {
          setError(outcome.detail)
          return
        }
        void run((options) => readResult(following.task_key, options))
      } else if (outcome.status === 'cancelled') {
        setPreview(undefined)
        setError('已取消。可以重新预览稿件并发起建议。')
      } else if (outcome.status === 'failed' || outcome.status === 'interrupted') {
        setError(outcome.detail)
      } else if (outcome.status === 'lost') {
        setError(outcome.message)
      }
    })
    return () => { mounted = false; detach() }
  }, [following, follow, detach, canRequest, readResult, run, setError, track])

  const clearPreview = () => {
    setPreview(undefined)
    setBlockers([])
    setError('')
  }

  const prepare = () => {
    const selectedNeighbor = operation === 'split' ? null : neighbor
    if (disabled || running || busy || selectedNeighbor === undefined) return
    clearPreview()
    return run(async (options) => {
      const ok = await runAfterSceneSave(runSavedAction, ensureSaved, async () => {
        const saved = controller.getSnapshot()
        if (!options.isCurrent() || !saved.scene || saved.scene.id !== sceneId) return false
        const payload = restructureRequest(saved.scene, saved.page?.items ?? [], selectedNeighbor)
        if (!payload) throw new Error('相邻场景已经变化，请重新选择。')
        const proposed = await sceneRestructureApi.preview(projectId, chapterId, payload, options)
        if (!options.isCurrent()) return false
        if (proposed.operation !== operation || proposed.scene_ids.length !== payload.scene_ids.length
          || !proposed.scene_ids.every((id, index) => id === payload.scene_ids[index])
          || !matchesRestructureScope(proposed, saved.scene, saved.page)) {
          throw new Error('稿件或预览目标已经变化，请重新核对。')
        }
        setPreview(proposed)
        return true
      })
      if (!ok && options.isCurrent()) setError('请先核对或保存当前草稿。')
    }, (error) => setBlockers(sceneBlockers(error).map((item) => item.label)))
  }

  const submit = () => {
    const selectedNeighbor = operation === 'split' ? null : neighbor
    if (!preview || disabled || busy || running || hasModel === false || preview.blockers.length
      || !preview.candidates.length || selectedNeighbor === undefined) return
    const reviewed = preview
    let sent = false
    return run(async (options) => {
      const ok = await runAfterSceneSave(runSavedAction, ensureSaved, async () => {
        const current = controller.getSnapshot()
        if (!options.isCurrent()) return false
        if (!current.scene || current.scene.id !== sceneId || !matchesRestructureScope(reviewed, current.scene, current.page)) {
          setPreview(undefined)
          throw new Error('保存后的场景版本已变化，请重新核对发送范围。')
        }
        const payload = restructureRequest(current.scene, current.page?.items ?? [], selectedNeighbor)
        if (!payload) throw new Error('相邻场景已经变化，请重新选择。')
        sent = true
        const queued = await sceneRestructureApi.start(projectId, chapterId, { ...payload, signature: reviewed.signature }, options)
        if (!options.isCurrent()) return false
        if (!hasTaskIdentity(queued) || queued.project_id !== projectId || queued.chapter_id !== chapterId || queued.kind !== 'scene.restructure') {
          throw new Error('任务身份无法确认，请在后台任务中核对。')
        }
        track(queued)
        setFollowing(queued)
        setPreview(undefined)
        return true
      })
      if (!ok && options.isCurrent()) setError('请先核对或保存当前草稿。')
    }, (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setPreview(undefined)
        void controller.load()
      } else if (sent && (!(error instanceof ApiError) || error.status === 0 || error.status >= 500)) {
        setPreview(undefined)
        setError('任务受理结果未知。请先在后台任务中核对，再重新发起。')
      }
    })
  }

  const selectCandidate = (candidateId: number) => {
    setCandidate(candidateId)
    setMetadata(undefined)
    setRightTitle('')
  }

  const apply = () => {
    if (!loaded || !freshResult || disabled || busy || applyBlocked || candidate === undefined) return
    const { key, result } = loaded
    const choice = result.operation === 'split' ? { right_title: rightTitle.trim() }
      : metadata ? { metadata_source: metadata } : null
    if (!choice || !result.proposals.some((item) => item.candidate_id === candidate)) return
    return run(async (options) => {
      const ok = await runAfterSceneSave(runSavedAction, ensureSaved, async () => {
        const current = controller.getSnapshot()
        if (!options.isCurrent()) return false
        if (!current.scene || current.scene.id !== sceneId || !matchesRestructureScope(result, current.scene, current.page)) {
          setApplyBlocked(true)
          throw new Error('场景已经变化，请重新生成建议。')
        }
        await sceneRestructureApi.apply(projectId, chapterId, key, {
          candidate_id: candidate, signature: result.signature, ...choice,
        }, options)
        if (!options.isCurrent()) return false
        message.success(result.operation === 'split' ? '已拆分场景' : '已合并相邻场景')
        await onApplied(result.scene_ids[0])
        return true
      })
      if (!ok && options.isCurrent()) setError('请先核对或保存当前草稿。')
    }, (error) => {
      setApplyBlocked(true)
      setBlockers(sceneBlockers(error).map((item) => item.label))
      if (!(error instanceof ApiError) || error.status === 0 || error.status >= 500) {
        setError('采纳结果未知。请刷新章节，再从后台任务核对建议。')
      }
      void controller.load()
    })
  }

  const stop = async () => {
    if (stopping.current || !canRequest()) return
    stopping.current = true
    setCancelling(true)
    try {
      await cancel()
    } catch (error) {
      if (canRequest()) setError(errorMessage(error))
    } finally {
      stopping.current = false
      if (canRequest()) setCancelling(false)
    }
  }
  const watching = running || !!following
  return (
    <Modal open title="AI 拆并建议" width={width} centered maskClosable={false}
      closable={!busy} keyboard={!busy} onCancel={busy ? undefined : onClose}
      styles={{ body: { maxHeight: 'calc(100dvh - 190px)', overflowY: 'auto' } }}
      footer={<Space wrap>
        <Button disabled={busy} onClick={onClose}>关闭</Button>
        {watching ? <Button danger loading={cancelling} disabled={!running} onClick={() => void stop()}>取消任务</Button>
          : result ? (result.proposals.length > 0 && <Button type="primary" loading={busy}
            disabled={disabled || applyBlocked || !freshResult || candidate === undefined || (result.operation === 'merge' && !metadata)}
            onClick={() => void apply()}>采纳{result.operation === 'split' ? '拆分' : '合并'}</Button>)
            : !readingTask && (preview ? <Button type="primary" loading={busy}
              disabled={disabled || hasModel === false || !!preview.blockers.length || !preview.candidates.length}
              onClick={() => void submit()}>发送并生成建议</Button>
              : <Button type="primary" loading={busy} disabled={disabled || (operation === 'merge' && neighbor === undefined)}
                onClick={() => void prepare()}>预览发送正文</Button>)}
      </Space>}>
      <Space direction="vertical" size={10} style={{ display: 'flex' }}>
        {readingTask && busy && <Typography.Text>正在核对任务与稿件…</Typography.Text>}
        {watching && <>
          <Typography.Text>{task?.status === 'queued' ? queueLabel(task) : task?.stage || '正在生成建议…'}</Typography.Text>
          <Progress percent={task?.progress ?? 0} size="small" />
          {reconnecting && <Alert type="info" showIcon message="正在重新连接后台任务。" />}
        </>}
        {!readingTask && !watching && !result && <>
          <Radio.Group aria-label="建议类型" value={operation} disabled={busy} options={[
            { label: '拆分当前场景', value: 'split' }, { label: '合并相邻场景', value: 'merge', disabled: !neighbors.length },
          ]} onChange={(event) => {
            const selected = event.target.value
            if (selected === 'split' || selected === 'merge') { setOperation(selected); clearPreview() }
          }} />
          {operation === 'merge' && <Form.Item label="合并对象" htmlFor="scene-restructure-neighbor" style={{ marginBottom: 0 }}>
            <Select id="scene-restructure-neighbor" style={{ width: '100%' }} placeholder="选择一个相邻场景"
              options={neighbors} disabled={busy} value={neighbor} onChange={(value) => { setNeighbor(value); clearPreview() }} />
          </Form.Item>}
          {hasModel === false && <Alert type="info" showIcon message="需要可用的默认文本模型才能生成建议。" />}
          {preview && <>
            <Typography.Text strong>发送给文本模型的已保存正文</Typography.Text>
            <Collapse size="small" defaultActiveKey={preview.scenes.map((item) => String(item.scene_id))} items={preview.scenes.map((item, index) => ({
              key: String(item.scene_id), label: item.title || `场景 ${index + 1}`, children: <SavedExcerpt text={item.content} />,
            }))} />
            {!preview.candidates.length && !preview.blockers.length && <Alert type="info" showIcon message="当前稿件没有可安全采用的位置。" />}
            {preview.blockers.map((item) => <Alert key={`${item.kind}:${item.id}`} type="warning" showIcon message={item.label} />)}
          </>}
        </>}
        {result && (result.proposals.length === 0
          ? <Alert type="info" showIcon message={result.operation === 'split' ? 'AI 未建议拆分这段稿件。' : 'AI 未建议合并所选场景。'} />
          : <>
            {!!resultLabels?.length && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>
              {resultLabels.join(' → ')}
            </Typography.Text>}
            {!freshResult && <Alert type="warning" showIcon message="建议对应的章节版本已变化。请重新核对稿件并生成建议。" />}
            <Radio.Group value={candidate} disabled={busy} aria-label="选择要采纳的建议"
              onChange={(event) => { if (typeof event.target.value === 'number') selectCandidate(event.target.value) }}>
              <Space direction="vertical" size={12} style={{ display: 'flex' }}>
                {result.proposals.map((item, index) => <div key={item.candidate_id} style={{ overflowWrap: 'anywhere' }}>
                  <Radio value={item.candidate_id}>{result.operation === 'split' ? `拆分建议 ${index + 1}` : '合并这两个场景'}</Radio>
                  <Typography.Paragraph style={{ marginBlock: '6px' }}>{item.reason}</Typography.Paragraph>
                  <Typography.Text type="secondary">{result.operation === 'split' ? '拆分点前' : '左场景结尾'}</Typography.Text>
                  <SavedExcerpt text={item.before} />
                  <Typography.Text type="secondary">{result.operation === 'split' ? '拆分点后' : '右场景开头'}</Typography.Text>
                  <SavedExcerpt text={item.after} />
                </div>)}
              </Space>
            </Radio.Group>
            {candidate !== undefined && (result.operation === 'split'
              ? <Form.Item label="右侧场景标题" htmlFor="scene-restructure-title" style={{ marginBottom: 0 }}>
                <Input id="scene-restructure-title" value={rightTitle} disabled={busy} maxLength={200}
                  onChange={(event) => setRightTitle(event.target.value)} />
              </Form.Item>
              : <>
                {result.conflicts.map((conflict) => <Alert key={conflict.field} type="warning"
                  message={sceneConflictLabels[conflict.field] ?? conflict.field}
                  description={<><div>左：{conflict.left || '无'}</div><div>右：{conflict.right || '无'}</div></>} />)}
                <Typography.Text strong>选择保留哪一侧的标题、摘要、本地规划和故事日期</Typography.Text>
                <Radio.Group aria-label="合并后的元数据" disabled={busy} value={metadata} options={[
                  { value: 'left', label: '保留左侧' }, { value: 'right', label: '保留右侧' },
                ]} onChange={(event) => {
                  if (event.target.value === 'left' || event.target.value === 'right') setMetadata(event.target.value)
                }} />
              </>)}
          </>)}
        {error && <Alert type="warning" showIcon message={error} />}
        {blockers.map((label) => <Alert key={label} type="warning" message={label} />)}
      </Space>
    </Modal>
  )
}
