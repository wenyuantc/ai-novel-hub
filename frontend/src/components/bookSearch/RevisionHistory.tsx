import { Button, Empty, Pagination, Popconfirm, Skeleton, Tag } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { bookSearchApi, type RevisionBatch } from '../../api/bookSearch'
import type { EditorActorIdentity } from '../../api/client'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { invalidateProject } from '../../utils/projectRefresh'
import { formatTime } from '../../utils/time'
import { invalidateMismatchedEditorActor } from '../editorDrafts/verifyDraftScope'
import { OperationAlert } from './OperationAlert'
import { describeError, type OperationError } from './selection'

interface Props { projectId: number; actor: EditorActorIdentity; onBusy: (busy: boolean) => void }
export default function RevisionHistory({ projectId, actor, onBusy }: Props) {
  const { canReadNow, canEdit, canEditNow } = useProjectAccess(projectId)
  const [page, setPage] = useState(1)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<{ batch: number; reason: OperationError } | null>(null)
  const lock = useRef(false)
  const load = useCallback((signal: AbortSignal) => bookSearchApi.history(projectId, page, signal, canReadNow), [projectId, page, canReadNow])
  const history = useMentionQuery(projectId, `batches:${page}`, load)
  const undo = async (batch: RevisionBatch) => {
    if (lock.current || !canEditNow()) return
    lock.current = true
    onBusy(true)
    setError(null)
    setBusy(batch.id)
    try {
      await bookSearchApi.undo(projectId, batch.id, actor, canEditNow)
      if (!canEditNow()) return
      invalidateProject(projectId, ['chapters'])
      history.reload()
    } catch (err) {
      invalidateMismatchedEditorActor(err, canEditNow)
      if (canEditNow()) setError({ batch: batch.id, reason: describeError(err) })
    } finally {
      lock.current = false
      if (canEditNow()) {
        setBusy(null)
        onBusy(false)
      }
    }
  }
  return <section className="book-search-history" aria-label="修订记录">
    <div className="book-search-bar"><h2>修订记录</h2><Button disabled={busy !== null} onClick={() => { setError(null); history.reload() }}>刷新记录</Button></div>
    {history.loading && <Skeleton active />}
    {history.error && <div role="alert">{history.error}</div>}
    {history.data?.total === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无批量修订记录" />}
    {history.data?.items.map((batch) => <article key={batch.id} className="book-search-chapter book-search-batch">
      <div className="book-search-bar">
        <h3>修订 #{batch.id}</h3>
        <Tag color={batch.status === 'applied' ? 'green' : undefined}>{batch.status === 'applied' ? '已应用' : '已撤销'}</Tag>
      </div>
      <p className="book-search-summary"><del>{batch.query}</del><span aria-label="替换为"> → </span>
        {batch.replacement ? <ins>{batch.replacement}</ins> : <em>〔删除〕</em>}</p>
      <div className="book-search-bar"><div>{batch.chapter_count} 章 · {batch.hit_count} 处 · {formatTime(batch.created_at)}
        {batch.undone_at && ` · 撤销于 ${formatTime(batch.undone_at)}`}</div>
        {canEdit && batch.status === 'applied' && <Popconfirm title="撤销整批修订？"
          description="任一受影响章节已变更时，整批将保留现状并列出冲突章节。"
          okText="确认撤销" cancelText="取消" onConfirm={() => void undo(batch)} disabled={busy !== null}>
          <Button disabled={busy !== null} loading={busy === batch.id}>整批撤销</Button>
        </Popconfirm>}
      </div>
      {error?.batch === batch.id && <OperationAlert error={error.reason} uncertain="暂时无法确认撤销结果。可刷新记录，或重试撤销这一批次。" />}
    </article>)}
    {history.data && history.data.total > history.data.page_size && <Pagination disabled={busy !== null} current={page}
      total={history.data.total} pageSize={history.data.page_size} showSizeChanger={false}
      onChange={(next) => { setError(null); setPage(next) }} />}
  </section>
}
