import { Alert, App, Button, Drawer, Empty, Input, Pagination, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { annotationApi, type AnnotationMessage, type AnnotationSelection, type AnnotationThread } from '../../api/annotations'
import { ApiError, errorMessage } from '../../api/client'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { createIntentKey } from '../../utils/novelTemplateCreation'
import { formatTime } from '../../utils/time'

export interface AnnotationTarget { selection: AnnotationSelection; text: string }
interface Props {
  pid: number; cid: number; owner: boolean; selectionDisabled: boolean; current: () => boolean
  capture: () => Promise<AnnotationTarget | null>
  jump: (thread: AnnotationThread) => Promise<boolean>
  finishJump: () => void
  refreshScene: () => Promise<void>
}

const pageSize = 10
const fullWidth = { width: '100%' }
const messageCount = { show: true, max: 4000, strategy: (value: string) => Array.from(value.trim()).length }
const prose = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } as const

function Comment({ item }: { item: AnnotationMessage }) {
  return <div>
    <Typography.Text strong>{item.author_name}</Typography.Text>{' '}
    <Typography.Text type="secondary"><time dateTime={item.created_at}>{formatTime(item.created_at)}</time></Typography.Text>
    <Typography.Paragraph style={prose}>{item.body}</Typography.Paragraph>
  </div>
}

export function AnnotationDiscussion({ item, owner, pid, cid, current, onResolve, onJump }: {
  item: AnnotationThread; owner: boolean; pid: number; cid: number; current: Props['current']
  onResolve: (item: AnnotationThread) => void; onJump: (item: AnnotationThread) => Promise<void>
}) {
  const [page, setPage] = useState(1)
  const fetch = useCallback((signal: AbortSignal) => annotationApi.messages(pid, cid, item.id, (page - 1) * pageSize, pageSize, current, signal), [pid, cid, item.id, page, current])
  const messages = useMentionQuery(pid, `annotations:${cid}:${item.id}:messages:${page}`, fetch)
  const [reply, setReply] = useState('')
  const operation = useRef('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async (resolve: boolean) => {
    if (busy || !current() || (resolve ? !owner : !reply.trim())) return
    setBusy(true); setError('')
    try {
      if (resolve) {
        const updated = await annotationApi.resolve(pid, cid, item.id, current)
        if (current()) onResolve(updated)
      } else {
        operation.current ||= createIntentKey()
        await annotationApi.reply(pid, cid, item.id, { body: reply, operation_id: operation.current }, current)
        if (!current()) return
        setReply(''); operation.current = ''
        setPage(Math.ceil(((messages.data?.total ?? 0) + 1) / pageSize)); messages.reload()
      }
    } catch (e) { if (current()) setError(errorMessage(e)) }
    finally { if (current()) setBusy(false) }
  }
  return <Space direction="vertical" size="middle" style={fullWidth}>
    <Typography.Paragraph style={prose}><blockquote>{item.excerpt}</blockquote></Typography.Paragraph>
    <Space wrap>
      <Tag color={item.resolved ? 'green' : 'gold'}>{item.resolved ? '已处理' : '待处理'}</Tag>
      {item.anchor_valid ? <Button size="small" onClick={() => void onJump(item)}>跳到原文</Button> : <Tag>原文位置已失效</Tag>}
      {owner && !item.resolved && <Button size="small" loading={busy} onClick={() => void submit(true)}>标记已处理</Button>}
    </Space>
    {messages.error ? <Alert type="error" message={messages.error} action={<Button onClick={messages.reload}>重试</Button>} />
      : messages.loading ? <Spin /> : messages.data?.items.map((entry) => <Comment key={entry.id} item={entry} />)}
    <Pagination simple hideOnSinglePage current={page} pageSize={pageSize} total={messages.data?.total ?? 0} onChange={setPage} />
    {error && <Alert type="error" showIcon message={error} />}
    <Input.TextArea count={messageCount} aria-label="回复内容" placeholder="写下回复" value={reply} autoSize={{ minRows: 2, maxRows: 8 }}
      disabled={busy} onChange={(event) => { setReply(event.target.value); operation.current = '' }} />
    <Button disabled={busy || !reply.trim() || Array.from(reply.trim()).length > 4000} onClick={() => void submit(false)}>发表回复</Button>
  </Space>
}

function ThreadList({ pid, cid, owner, current, onJump }: Pick<Props, 'pid' | 'cid' | 'owner' | 'current'> & {
  onJump: (item: AnnotationThread) => Promise<void>
}) {
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<AnnotationThread | null>(null)
  const fetch = useCallback((signal: AbortSignal) => annotationApi.list(pid, cid, (page - 1) * pageSize, pageSize, current, signal), [pid, cid, page, current])
  const list = useMentionQuery(pid, `annotations:${cid}:threads:${page}`, fetch)
  if (selected) return <Space direction="vertical" size="middle" style={fullWidth}>
    <Button onClick={() => { setSelected(null); list.reload() }}>返回本章讨论</Button>
    <AnnotationDiscussion key={selected.id} item={selected} {...{ pid, cid, owner, current, onJump }} onResolve={(updated) => setSelected((active) => active?.id === updated.id ? updated : active)} />
  </Space>
  return <Space direction="vertical" size="middle" style={fullWidth}>
    {list.error ? <Alert type="error" message={list.error} action={<Button onClick={list.reload}>重试</Button>} />
      : list.loading ? <Spin /> : list.data?.items.length ? list.data.items.map((item) => <article key={item.id}>
        <Tag color={item.resolved ? 'green' : 'gold'}>{item.resolved ? '已处理' : '待处理'}</Tag>
        {!item.anchor_valid && <Tag>原文位置已失效</Tag>}
        <Typography.Paragraph style={prose}><blockquote>{item.excerpt}</blockquote></Typography.Paragraph>
        <Comment item={item.first_message} />
        <Button size="small" onClick={() => setSelected(item)}>查看讨论</Button>
      </article>) : <Empty description="本章暂无批注" />}
    <Pagination simple hideOnSinglePage current={page} pageSize={pageSize} total={list.data?.total ?? 0} onChange={setPage} />
  </Space>
}

/** 实例按作品、章节和授权轮次挂载，草稿只在当前审阅者的工作台内保留。 */
export default function AnnotationPanel({ pid, cid, owner, current: projectCurrent, capture, jump, finishJump, refreshScene, selectionDisabled }: Props) {
  const { message } = App.useApp()
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const current = useCallback(() => mounted.current && projectCurrent(), [projectCurrent])
  const [mode, setMode] = useState<'create' | 'list' | null>(null)
  const [target, setTarget] = useState<AnnotationTarget | null>(null)
  const [body, setBody] = useState('')
  const operation = useRef('')
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState('')
  const [stale, setStale] = useState(false)
  const begin = async () => {
    if (busy || !current()) return
    setBusy(true)
    try {
      const next = await capture()
      if (!next || !current()) return
      operation.current = ''
      setTarget(next); setStale(false); setProblem(''); setMode('create')
    } catch (e) { if (current()) message.warning(errorMessage(e)) }
    finally { if (current()) setBusy(false) }
  }
  const submit = async () => {
    if (busy || stale || !target || !body.trim() || !current()) return
    setBusy(true); setProblem('')
    try {
      operation.current ||= createIntentKey()
      await annotationApi.create(pid, cid, { ...target.selection, body, operation_id: operation.current }, current)
      if (!current()) return
      setBody(''); setTarget(null); setMode('list'); operation.current = ''
      message.success('批注已发表')
    } catch (e) {
      if (!current()) return
      if (e instanceof ApiError && e.status === 409) {
        setStale(true); setProblem('稿件或选区已变化。意见草稿已保留，请重新核对原文后发表。')
      } else setProblem(errorMessage(e))
    } finally { if (current()) setBusy(false) }
  }
  const onJump = async (item: AnnotationThread) => {
    try { if (await jump(item) && current()) setMode(null) }
    catch (e) { if (current()) message.warning(errorMessage(e)) }
  }
  return <>
    <Space size={6}>
      <Button size="small" onPointerDown={(event) => event.preventDefault()} disabled={selectionDisabled || busy} onClick={() => void begin()}>批注选区</Button>
      <Button size="small" disabled={busy} onClick={() => setMode('list')}>本章批注</Button>
    </Space>
    <Drawer title={mode === 'create' ? '批注选区' : '本章批注'} open={mode !== null} width="min(480px, 100vw)"
      onClose={() => { if (!busy) setMode(null) }} afterOpenChange={(open) => { if (!open && current()) finishJump() }}>
      {mode === 'create' && target && <Space direction="vertical" size="middle" style={fullWidth}>
        <Typography.Paragraph style={prose}><blockquote>{target.text}</blockquote></Typography.Paragraph>
        <Input.TextArea count={messageCount} aria-label="批注意见" placeholder="针对这段正文写下意见" value={body} autoSize={{ minRows: 3, maxRows: 10 }}
          disabled={busy} onChange={(event) => { setBody(event.target.value); operation.current = '' }} />
        {problem && <Alert type="warning" showIcon message={problem} />}
        {stale ? <Button onClick={async () => {
          try { await refreshScene(); if (current()) setMode(null) }
          catch (e) { if (current()) setProblem(errorMessage(e)) }
        }}>重新选择原文</Button> : <Button type="primary" loading={busy} disabled={!body.trim() || Array.from(body.trim()).length > 4000} onClick={() => void submit()}>发表批注</Button>}
      </Space>}
      {mode === 'list' && <ThreadList {...{ pid, cid, owner, current, onJump }} />}
    </Drawer>
  </>
}
