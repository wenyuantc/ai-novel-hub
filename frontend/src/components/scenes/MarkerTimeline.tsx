import { Alert, Button, Empty, Popconfirm, Space, Spin, Tag, Typography, theme } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { richDocumentApi, type MarkerColor, type MarkerTimelineItem, type MarkerTimelinePage } from '../../api/richDocument'
import { useAuthStore } from '../../stores/auth'

const PAGE_SIZE = 20
const COLORS: Record<MarkerColor, string> = { yellow: '黄色', green: '绿色', blue: '蓝色', purple: '紫色', red: '红色' }

export interface MarkerTimelineProps {
  projectId: number
  /** 本页保存、身份或可见范围变化后传入新值，重新取得第一页快照。 */
  refreshKey?: string | number
  editable?: boolean
  /** 调用方负责跨章、跨景的未保存草稿确认；取消返回 false。 */
  onJump: (item: MarkerTimelineItem) => Promise<boolean>
  /** 调用方负责选区、版本校验和保存；未完成或取消返回 false。 */
  onRelink?: (item: MarkerTimelineItem) => Promise<boolean>
  onDelete?: (item: MarkerTimelineItem) => Promise<boolean>
}

interface ListProps {
  page: MarkerTimelinePage
  editable: boolean
  busy: boolean
  canRelink: boolean
  canDelete: boolean
  onJump: (item: MarkerTimelineItem) => void
  onRelink: (item: MarkerTimelineItem) => void
  onDelete: (item: MarkerTimelineItem) => void
}

function Excerpt({ text, truncated }: { text: string; truncated: boolean }) {
  return <>
    <Typography.Paragraph ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
      style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', margin: '6px 0' }}>
      {text || '（无可显示的原文片段）'}
    </Typography.Paragraph>
    {truncated && <Typography.Text type="secondary">原文片段过长，已截断。</Typography.Text>}
  </>
}

/** 无副作用的列表，失效状态、截断和颜色均有文字，不依靠颜色表达。 */
export function MarkerTimelineList({ page, editable, busy, canRelink, canDelete, onJump, onRelink, onDelete }: ListProps) {
  const { token } = theme.useToken()
  if (!page.items.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有已保存的正文标记" />
  return <div role="list" aria-label="全书正文标记">
    {page.items.map((item) => <div key={`${item.chapter_id}:${item.scene_id}:${item.key}`} role="listitem"
      style={{ borderBottom: `1px solid ${token.colorBorderSecondary}`, padding: '12px 0' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, minWidth: 0 }}>
        <Typography.Text strong style={{ minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>{`第 ${item.chapter_no} 章${item.chapter_title ? ` ${item.chapter_title}` : ''}`}</Typography.Text>
        <Typography.Text type="secondary" style={{ minWidth: 0, maxWidth: '100%', overflowWrap: 'anywhere' }}>{item.scene_title || `场景 #${item.scene_id}`}</Typography.Text>
        {item.section_key && <Tag style={{ whiteSpace: 'normal', overflowWrap: 'anywhere', maxWidth: '100%' }}>区块：{item.section_title || '已退场区块'}</Tag>}
      </div>
      <div style={{ marginTop: 6 }}>
        <Tag color={item.color}>{COLORS[item.color]}标记</Tag>
        {item.status === 'stale' && <Tag color="warning">已失效</Tag>}
        {!item.scene_active && <Tag>来源场景已退场</Tag>}
      </div>
      {item.draft_excerpt != null ? <>
        <Typography.Text type="secondary">草稿末态片段：由作者未保存事务捕获，服务端无法独立验证。</Typography.Text>
        <Excerpt text={item.draft_excerpt} truncated={!!item.draft_excerpt_truncated} />
        <details><summary>查看原有片段</summary><Excerpt text={item.excerpt} truncated={item.excerpt_truncated} /></details>
      </> : <Excerpt text={item.excerpt} truncated={item.excerpt_truncated} />}
      {item.status === 'stale' && <div><Typography.Text type="secondary">原位置不可可靠定位，请重新关联或删除。</Typography.Text></div>}
      <Space wrap size={[8, 4]} style={{ marginTop: 8 }}>
        {item.status === 'active' && item.scene_active && <Button size="small" aria-label={`定位第 ${item.chapter_no} 章${item.scene_title || `场景 ${item.scene_id}`}的标记`} disabled={busy} onClick={() => onJump(item)}>定位</Button>}
        {item.status === 'stale' && <Button size="small" aria-label={`重新关联第 ${item.chapter_no} 章${item.scene_title || `场景 ${item.scene_id}`}的标记`} disabled={busy || !editable || !canRelink} onClick={() => onRelink(item)}>重新关联</Button>}
        <Popconfirm title="删除这条标记？" description="正文和区块内容不会被删除。" okText="删除" cancelText="取消"
          disabled={busy || !editable || !canDelete} onConfirm={() => onDelete(item)}>
          <Button size="small" aria-label={`删除第 ${item.chapter_no} 章${item.scene_title || `场景 ${item.scene_id}`}的标记`} disabled={busy || !editable || !canDelete}>删除标记</Button>
        </Popconfirm>
      </Space>
    </div>)}
  </div>
}

interface Query { offset: number; snapshot?: string; nonce: number }

function TimelineSession({ projectId, editable = false, onJump, onRelink, onDelete }: MarkerTimelineProps) {
  const [query, setQuery] = useState<Query>({ offset: 0, nonce: 0 })
  const [page, setPage] = useState<MarkerTimelinePage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const sequence = useRef(0)
  const actionBusy = useRef(false)
  const refresh = () => {
    setLoading(true)
    setError(null)
    setQuery((old) => ({ offset: 0, nonce: old.nonce + 1 }))
  }

  useEffect(() => {
    const generation = ++sequence.current
    const abort = new AbortController()
    richDocumentApi.timeline(projectId, { offset: query.offset, limit: PAGE_SIZE, snapshot: query.snapshot }, abort.signal)
      .then((result) => {
        if (sequence.current !== generation) return
        setPage(result)
        setLoading(false)
      }, (err: unknown) => {
        if (sequence.current !== generation || abort.signal.aborted) return
        if (err instanceof ApiError && [401, 403, 404].includes(err.status)) setPage(null)
        setError(errorMessage(err))
        setLoading(false)
      })
    return () => { abort.abort(); sequence.current += 1 }
  }, [projectId, query.offset, query.snapshot, query.nonce])

  const act = async (item: MarkerTimelineItem, action: (value: MarkerTimelineItem) => Promise<boolean>, reload: boolean) => {
    if (actionBusy.current || loading || error) return
    actionBusy.current = true
    setBusy(true)
    const generation = sequence.current
    try {
      const changed = await action(item)
      if (generation === sequence.current && changed && reload) refresh()
    } catch (err) {
      if (generation === sequence.current) {
        if (err instanceof ApiError && [401, 403, 404].includes(err.status)) setPage(null)
        setError(errorMessage(err))
      }
    } finally {
      actionBusy.current = false
      if (generation === sequence.current) setBusy(false)
    }
  }

  const move = (offset: number) => {
    if (!page || loading || busy || error) return
    setLoading(true)
    setError(null)
    setQuery((old) => ({ offset, snapshot: page.snapshot, nonce: old.nonce }))
  }

  return <section aria-label="全书标记时间线">
    <Typography.Paragraph type="secondary">按章节和场景顺序显示已保存的标记；当前草稿保存后才会进入目录。</Typography.Paragraph>
    {!editable && <Typography.Paragraph type="secondary">当前为只读模式，不能修改标记。</Typography.Paragraph>}
    {editable && (!onRelink || !onDelete) && <Typography.Paragraph type="secondary">当前界面尚未接入的标记操作不可用。</Typography.Paragraph>}
    <Space wrap style={{ marginBottom: 8 }}><Button onClick={refresh} disabled={loading || busy}>从第一页刷新</Button>
      {loading && <span role="status"><Spin size="small" /> 正在加载标记…</span>}
    </Space>
    {error && <Alert type="warning" showIcon message="标记目录未更新" description={error}
      action={<Button size="small" onClick={refresh}>重新加载第一页</Button>} />}
    {page && <MarkerTimelineList page={page} editable={editable} busy={busy || loading || !!error}
      canRelink={!!onRelink} canDelete={!!onDelete}
      onJump={(item) => { void act(item, onJump, false) }}
      onRelink={(item) => { if (editable && onRelink) void act(item, onRelink, true) }}
      onDelete={(item) => { if (editable && onDelete) void act(item, onDelete, true) }} />}
    {page && page.total > 0 && <Space wrap style={{ marginTop: 12 }}>
      <Button size="small" disabled={loading || busy || !!error || page.offset === 0} onClick={() => move(Math.max(0, page.offset - PAGE_SIZE))}>上一页</Button>
      <Typography.Text type="secondary">{`${page.offset + 1}–${Math.min(page.offset + page.items.length, page.total)} / ${page.total}`}</Typography.Text>
      <Button size="small" disabled={loading || busy || !!error || page.offset + page.items.length >= page.total} onClick={() => move(page.offset + PAGE_SIZE)}>下一页</Button>
    </Space>}
  </section>
}

/** 项目、登录身份、授权版本或外部保存发生变化时立即卸载旧页面与其在途请求。 */
export default function MarkerTimeline(props: MarkerTimelineProps) {
  const user = useAuthStore((state) => state.user)
  if (!user) return <Alert type="warning" message="请登录后查看标记" />
  return <TimelineSession key={`${props.projectId}:${user.id}:${user.authorization_revision}:${props.refreshKey ?? ''}`} {...props} />
}
