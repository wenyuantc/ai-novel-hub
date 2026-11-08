import { Alert, App, Button, Checkbox, Input, InputNumber, Modal, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { currentRequestGeneration, errorMessage, request, watchRequestGeneration } from '../../api/client'
import { richDocumentApi, type RichReviewPosition, type RichReviewPreview, type RichReviewScene } from '../../api/richDocument'
import type { Task } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import TextDiff from '../TextDiff'
import { reviewTextareaOffset, validReviewAnchor } from './reviewOffsets'

const slot = (sceneId: number, key: string) => `${sceneId}:${key}`
const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose
const sessionIdentity = () => {
  const auth = useAuthStore.getState()
  return JSON.stringify([auth.status, auth.user?.id ?? null, authorizationKey(auth.user), currentRequestGeneration()])
}

function reviewPositions(preview: RichReviewPreview, anchors: Record<string, number>): RichReviewPosition[] | null {
  const result: RichReviewPosition[] = []
  for (const scene of preview.scenes) {
    let previous = -1
    const ordered = [...scene.original.sections].sort((a, b) => a.anchor - b.anchor || a.order - b.order)
    for (const section of ordered) {
      const mapped = scene.proposed_sections.find((item) => item.key === section.key)
      if (!mapped) return null
      const needed = scene.unresolved_sections.includes(section.key)
      const anchor = needed ? anchors[slot(scene.scene_id, section.key)] : mapped.anchor
      if (typeof anchor !== 'number' || !validReviewAnchor(scene.candidate, anchor) || anchor < previous) return null
      if (needed) result.push({ scene_id: scene.scene_id, section_key: section.key, anchor })
      previous = anchor
    }
  }
  return result
}

/** 提案只读到显式确认；遇冲突保持所有预览与位置选择，不在后台重发模型。 */
export default function RichReviewModal({ task, onClose, onChanged }: { task: Task; onClose: () => void; onChanged: (task: Task) => void }) {
  const { message } = App.useApp()
  const [preview, setPreview] = useState<RichReviewPreview>()
  const [loading, setLoading] = useState(true)
  const [working, setWorking] = useState(false)
  const [frozen, setFrozen] = useState(false)
  const [error, setError] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [anchors, setAnchors] = useState<Record<string, number>>({})
  const [cursors, setCursors] = useState<Record<number, number>>({})
  const identity = task.rich_review
  const user = useAuthStore((state) => state.user)
  const authStatus = useAuthStore((state) => state.status)
  const sessionGeneration = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const actor = JSON.stringify([authStatus, user?.id ?? null, authorizationKey(user), sessionGeneration])
  const [boundActor] = useState(actor)
  const allowed = authStatus === 'authenticated' && !!user && actor === boundActor
  const taskIdentity = JSON.stringify([task.id, identity?.task_key, identity?.run_key, identity?.token, identity?.chapter_id])
  const latestTaskIdentity = useRef(taskIdentity)
  const callbacks = useRef({ onClose, onChanged })
  const epoch = useRef(0)
  const pending = useRef<AbortController | null>(null)
  const current = (ticket: number, expectedTask: string) => epoch.current === ticket
    && latestTaskIdentity.current === expectedTask && sessionIdentity() === boundActor

  useLayoutEffect(() => {
    latestTaskIdentity.current = taskIdentity
    callbacks.current = { onClose, onChanged }
  }, [taskIdentity, onClose, onChanged])

  useLayoutEffect(() => {
    if (allowed) return
    epoch.current++
    pending.current?.abort(); pending.current = null
    callbacks.current.onClose()
  }, [allowed])

  useEffect(() => {
    if (!allowed) return
    const controller = new AbortController()
    pending.current = controller
    const ticket = ++epoch.current; const expected = taskIdentity
    queueMicrotask(() => {
      if (controller.signal.aborted || !current(ticket, expected)) return
      setPreview(undefined); setLoading(true); setWorking(false); setError(''); setFrozen(false); setConfirmed(false); setAnchors({}); setCursors({})
    })
    void richDocumentApi.review(task, controller.signal).then((data) => {
      if (!controller.signal.aborted && current(ticket, expected)) setPreview(data)
    }).catch((err: unknown) => {
      if (!controller.signal.aborted && current(ticket, expected)) setError(errorMessage(err))
    }).finally(() => {
      if (!controller.signal.aborted && current(ticket, expected)) setLoading(false)
      if (pending.current === controller) pending.current = null
    })
    const generation = epoch
    return () => { controller.abort(); pending.current?.abort(); pending.current = null; generation.current++ }
    // 每次只绑定明确的任务身份，不因轮询产生的对象引用变化而重置作者选择。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskIdentity, allowed])

  const shownPreview = allowed && preview?.task_key === identity?.task_key && preview?.run_key === identity?.run_key
    && preview?.token === identity?.token && preview?.chapter_id === identity?.chapter_id ? preview : undefined
  const needsFormatConfirmation = !!shownPreview?.scenes.some((scene) => scene.format_loss)
  const positions = shownPreview ? reviewPositions(shownPreview, anchors) : null
  const apply = async () => {
    if (!allowed || !shownPreview || shownPreview.status !== 'pending' || !positions || (needsFormatConfirmation && !confirmed) || working || frozen) return
    const controller = new AbortController(); pending.current = controller
    const ticket = ++epoch.current; const expected = taskIdentity
    setWorking(true); setError('')
    let accepted = false
    try {
      await richDocumentApi.acceptReview(task, positions, confirmed, controller.signal)
      if (!current(ticket, expected) || controller.signal.aborted) return
      accepted = true; setFrozen(true)
      const next = await request<Task>(`/tasks/${task.id}`, { signal: controller.signal })
      if (!current(ticket, expected) || controller.signal.aborted) return
      // 任务整数 ID 可能复用；用原 task_key/run_key/token 再查提案，不能把新任务当作旧结果。
      const checked = await richDocumentApi.review(task, controller.signal)
      if (!current(ticket, expected) || controller.signal.aborted) return
      if (checked.status !== 'applied' || next.id !== task.id
        || next.rich_review && next.rich_review.task_key !== identity?.task_key) throw new Error('任务身份或提案状态已变化')
      callbacks.current.onChanged(next)
      message.success(next.status === 'queued' ? '已采纳，批量任务将从分析阶段继续' : '已采纳候选稿')
      callbacks.current.onClose()
    } catch (err) {
      if (current(ticket, expected) && !controller.signal.aborted) {
        setFrozen(true)
        setError(accepted ? `候选已采纳，但无法核对最新任务状态：${errorMessage(err)}。请关闭并刷新任务，不要重复采纳。`
          : `无法确认本次采纳结果：${errorMessage(err)}。原预览已保留；请关闭并刷新任务核对，不要重复采纳。`)
      }
    } finally {
      if (pending.current === controller) pending.current = null
      if (current(ticket, expected) && !controller.signal.aborted) setWorking(false)
    }
  }
  const selectCursor = (scene: RichReviewScene, element: HTMLTextAreaElement) => {
    if (element.selectionStart !== element.selectionEnd) return
    const offset = reviewTextareaOffset(scene.candidate, element.value, element.selectionStart)
    if (offset === null) { setError('候选正文的可见光标无法安全映射，请使用合法码点位置。'); return }
    setCursors((current) => ({ ...current, [scene.scene_id]: offset }))
  }
  const close = () => { if (!working || !allowed) { epoch.current++; pending.current?.abort(); pending.current = null; callbacks.current.onClose() } }
  return <Modal open title="核对 AI 候选稿" width={960} maskClosable={false} onCancel={close}
    footer={<Space><Button disabled={working && allowed} onClick={close}>稍后核对</Button><Button type="primary" loading={working && allowed} disabled={!allowed || frozen || loading || !shownPreview || shownPreview.status !== 'pending' || !positions || (needsFormatConfirmation && !confirmed)} onClick={() => void apply()}>确认并采纳整章</Button></Space>}>
    <Spin spinning={loading && allowed}>
      <Space direction="vertical" size={12} style={{ width: '100%', maxHeight: '68vh', overflowY: 'auto' }}>
        {!allowed ? <Alert type="warning" showIcon message="登录身份或授权已变化，请重新打开任务。" /> : <>
        <Alert type="info" showIcon message="确认前原章不变，候选会保留供对照" description="为每个未能可靠定位的作者区块选择候选中的位置；其内容及相对顺序会保留。冲突时不会自动重试或重新调用模型。" />
        {error && <Alert type="error" showIcon message={error} description="预览和已选位置仍在；请先核对最新章节或权限。" />}
        {shownPreview && shownPreview.status !== 'pending' && <Alert type="warning" showIcon message="该提案已不处于待核对状态，当前为只读对照。" />}
        {shownPreview?.scenes.map((scene, index) => <section key={scene.scene_id} aria-label={`场景 ${index + 1} 候选`}>
          <Typography.Title level={5}>场景 {index + 1} · #{scene.scene_id}</Typography.Title>
          <TextDiff a={scene.original.content} b={scene.candidate} maxHeight={200} />
          <Typography.Paragraph type="secondary" style={{ marginBottom: 4 }}>在只读候选中放置光标，再将光标位置指定给需要核对的区块。换行和 emoji 按原文码点映射。</Typography.Paragraph>
          <Input.TextArea aria-label={`场景 ${index + 1} 候选正文`} value={scene.candidate} readOnly autoSize={{ minRows: 3, maxRows: 8 }} onSelect={(event) => selectCursor(scene, event.currentTarget)} />
          {scene.format_loss && <Alert type="warning" showIcon message="部分正文格式无法可靠继承" description="候选会保留可证明未变的格式；无法继承的格式须在下方明确确认。" />}
          {scene.proposed_markers.some((item) => item.status === 'stale' && scene.original.markers.find((old) => old.key === item.key)?.status === 'active') && <Alert type="warning" showIcon message="部分正文标记将失效" description="原片段会保留；完整片段超出展示上限时会明确标记为截断，可在时间线重新关联。" />}
          {[...scene.original.sections].sort((a, b) => a.anchor - b.anchor || a.order - b.order).map((section) => {
            const key = slot(scene.scene_id, section.key)
            const unresolved = scene.unresolved_sections.includes(section.key)
            const mapped = scene.proposed_sections.find((item) => item.key === section.key)
            return <div key={section.key} style={{ marginTop: 8, padding: 8, border: '1px solid var(--ant-color-border-secondary)', borderRadius: 6 }}>
              <Space wrap><Typography.Text strong>{section.title}</Typography.Text><Tag>{section.include_in_ai ? '允许 AI 上下文' : '默认不向 AI 发送'}</Tag><Typography.Text type="secondary">原位置 {section.anchor}</Typography.Text></Space>
              <details><summary>查看作者区块原文</summary><div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 160, overflow: 'auto' }}>{section.content || '（空）'}</div></details>
              {unresolved ? <Space wrap><Typography.Text>新位置（码点）</Typography.Text><InputNumber aria-label={`${section.title}的新位置`} min={0} max={Array.from(scene.candidate).length} precision={0} value={anchors[key]} onChange={(value) => setAnchors((current) => { const next = { ...current }; if (typeof value === 'number') next[key] = value; else delete next[key]; return next })} /><Button disabled={cursors[scene.scene_id] === undefined} onClick={() => setAnchors((current) => ({ ...current, [key]: cursors[scene.scene_id] }))}>使用候选光标</Button></Space> : <Typography.Text type="secondary">已可靠定位：{mapped?.anchor}</Typography.Text>}
            </div>
          })}
        </section>)}
        {shownPreview && !positions && <Typography.Text type="warning">请为每个待核对区块选择合法位置，并保持原有相对顺序；CRLF 中间不可落点。</Typography.Text>}
        {needsFormatConfirmation && <Checkbox checked={confirmed} disabled={working || frozen} onChange={(event) => setConfirmed(event.target.checked)}>我已核对候选，确认接受所提示的格式损失</Checkbox>}
        </>}
      </Space>
    </Spin>
  </Modal>
}
