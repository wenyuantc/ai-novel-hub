import { Alert, Button, Pagination, Spin, Tag } from 'antd'
import type { SerializationChapter, SerializationPublication } from '../../api/serialization'
import { EDITORIAL_LABEL, SERIALIZATION_PAGE_SIZE } from './calendarModel'
import { beijingMinute } from './serializationForm'

export function QueryFeedback({ loading, error, onReload }: { loading: boolean; error?: string; onReload: () => void }) {
  if (error) return <Alert type="error" showIcon message={error} action={<Button size="small" onClick={onReload}>重试</Button>} />
  if (loading) return <div className="serialization-loading" role="status"><Spin size="small" /> 正在加载</div>
  return null
}

export function PageNav({ total, page, onChange }: { total: number; page: number; onChange: (page: number) => void }) {
  const lastPage = Math.max(1, Math.ceil(total / SERIALIZATION_PAGE_SIZE))
  if (page > lastPage) return <Button size="small" onClick={() => onChange(lastPage)}>返回有记录的一页</Button>
  if (total <= SERIALIZATION_PAGE_SIZE) return null
  return <Pagination size="small" aria-label="列表分页" current={page} pageSize={SERIALIZATION_PAGE_SIZE}
    total={total} showSizeChanger={false} onChange={onChange} />
}

export function ChapterList({ items, empty, onOpen }: {
  items: SerializationChapter[]; empty: string; onOpen: (id: number) => void
}) {
  if (!items.length) return <p className="serialization-empty">{empty}</p>
  return <ul className="serialization-list">{items.map((row) =>
    <li key={row.chapter_id}>
      <button type="button" className="serialization-row" onClick={() => onOpen(row.chapter_id)}>
        <span className="serialization-item-main">
          <strong>第 {row.chapter_no} 章 · {row.title || '未命名章节'}</strong>
          <small>{row.planned_date ? `计划 ${row.planned_date}` : '未排期'}</small>
        </span>
        <span className="serialization-tags">
          <Tag>{EDITORIAL_LABEL[row.stage]}</Tag>{row.is_published && <Tag color="success">已发布</Tag>}
        </span>
      </button>
    </li>)}</ul>
}

export function PublicationList({ items, empty, onOpen }: {
  items: SerializationPublication[]; empty: string; onOpen: (id: number) => void
}) {
  if (!items.length) return <p className="serialization-empty">{empty}</p>
  return <ul className="serialization-list">{items.map((row) =>
    <li key={row.id}>
      <button type="button" className="serialization-row" onClick={() => onOpen(row.id)}>
        <span className="serialization-item-main">
          <strong>第 {row.chapter_no} 章 · {row.title || '未命名章节'}</strong>
          <small>{beijingMinute(row.published_at).replace('T', ' ')} · 定稿 {row.edition_seq}
            {row.chapter_id === null && ' · 源章节已删除'}</small>
        </span>
        <span className="serialization-channel">{row.channel}</span>
      </button>
    </li>)}</ul>
}
