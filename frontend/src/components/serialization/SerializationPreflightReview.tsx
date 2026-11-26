import { Alert, Button, Pagination, Space } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { PreflightHit, PreflightPage } from '../../api/serialization'
import SerializationManuscript, { type Manuscript } from './SerializationManuscript'

interface Props {
  projectId: number
  chapterId: number
  manuscript: Manuscript
  target: string
  actionLabel: string
  page: PreflightPage
  stale: boolean
  busy: boolean
  onPage: (offset: number) => void
  onConfirm: () => void
  onCancel: () => void
}

export default function SerializationPreflightReview({ projectId, chapterId, manuscript, actionLabel, target, page, stale, busy,
  onPage, onConfirm, onCancel }: Props) {
  const [hit, setHit] = useState<PreflightHit>()
  return <section aria-label="发布前用词核对" className="serialization-preflight">
    <Alert type="warning" showIcon role="status" message={`${stale ? '检查结果已更新，请重新核对。' : ''}${target}，命中 ${page.total} 处`} />
    <SerializationManuscript projectId={projectId} manuscript={manuscript} hit={hit} />
    {page.hits.length > 0 && <ol className="serialization-preflight-hits" start={page.offset + 1}>
      {page.hits.map((item, index) => <li key={index}>
        <button type="button" className="serialization-row" onClick={() => setHit({ ...item })}>
          <span className="serialization-item-main"><strong>{item.field === 'title' ? '章名' : '正文'} · {item.term}</strong>
            <small>{item.context}</small></span>
          <span>定位原文</span>
        </button>
      </li>)}
    </ol>}
    {page.total > page.limit && <Pagination size="small" simple showSizeChanger={false} disabled={busy}
      current={Math.floor(page.offset / page.limit) + 1} total={page.total} pageSize={page.limit}
      onChange={(number) => onPage((number - 1) * page.limit)} />}
    <Space wrap>
      <Button type="primary" loading={busy} onClick={onConfirm}>已核对，继续{actionLabel}</Button>
      <Button disabled={busy} onClick={onCancel}>取消核对</Button>
      {!busy && <Link to={`/projects/${projectId}/chapters/${chapterId}`}>返回修改草稿</Link>}
    </Space>
  </section>
}
