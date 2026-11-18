import { Button, Input, Select, Tabs } from 'antd'
import { useCallback, useState } from 'react'
import { serializationApi, type SerializationStage } from '../../api/serialization'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { EDITORIAL_LABEL, SERIALIZATION_PAGE_SIZE } from './calendarModel'
import { ChapterList, PageNav, PublicationList, QueryFeedback } from './SerializationLists'

interface ListProps {
  projectId: number
  version: number
  onOpen: (id: number) => void
}

function Chapters({ projectId, version, onOpen }: ListProps) {
  const { canReadNow } = useProjectAccess(projectId)
  const [page, setPage] = useState(1)
  const [filter, setFilter] = useState<'all' | 'stock' | SerializationStage>('all')
  const load = useCallback((signal: AbortSignal) => serializationApi.chapters(projectId, {
    stage: filter === 'all' || filter === 'stock' ? undefined : filter, stock_only: filter === 'stock' || undefined,
    offset: (page - 1) * SERIALIZATION_PAGE_SIZE, limit: SERIALIZATION_PAGE_SIZE,
  }, { signal, isCurrent: canReadNow }), [projectId, filter, page, canReadNow])
  const result = useMentionQuery(projectId, `chapters:${filter}:${page}:${version}`, load)
  return <>
    <Select aria-label="筛选章节" value={filter} onChange={(value) => { setFilter(value); setPage(1) }}
      options={[{ value: 'all', label: '全部章节' }, { value: 'stock', label: '可发布存稿' },
        ...Object.entries(EDITORIAL_LABEL).map(([value, label]) => ({ value, label }))]} />
    <QueryFeedback {...result} onReload={result.reload} />
    {result.data && <><ChapterList items={result.data.items} empty={filter === 'all' ? '作品还没有章节。' : '没有符合条件的章节。'} onOpen={onOpen} />
      <PageNav total={result.data.total} page={page} onChange={setPage} /></>}
  </>
}

function History({ projectId, version, onOpen }: ListProps) {
  const { canReadNow } = useProjectAccess(projectId)
  const [page, setPage] = useState(1)
  const [draft, setDraft] = useState('')
  const [channel, setChannel] = useState('')
  const load = useCallback((signal: AbortSignal) => serializationApi.publications(projectId, {
    channel: channel || undefined, offset: (page - 1) * SERIALIZATION_PAGE_SIZE, limit: SERIALIZATION_PAGE_SIZE,
  }, { signal, isCurrent: canReadNow }), [projectId, channel, page, canReadNow])
  const result = useMentionQuery(projectId, `history:${channel}:${page}:${version}`, load)
  return <>
    <form className="serialization-controls" onSubmit={(e) => { e.preventDefault(); setChannel(draft.trim()); setPage(1) }}>
      <Input aria-label="发布平台" placeholder="按发布平台筛选" allowClear value={draft} onChange={(e) => {
        setDraft(e.target.value)
        if (!e.target.value) { setChannel(''); setPage(1) }
      }} />
      <Button htmlType="submit">筛选</Button><span className="serialization-muted">按实际发布时间排序（北京时间）</span>
    </form>
    <QueryFeedback {...result} onReload={result.reload} />
    {result.data && <><PublicationList items={result.data.items} empty={channel ? '该平台还没有发布记录。' : '还没有发布记录。'} onOpen={onOpen} />
      <PageNav total={result.data.total} page={page} onChange={setPage} /></>}
  </>
}

export default function WorkbenchLibrary({ projectId, version, onChapter, onPublication }: {
  projectId: number; version: number; onChapter: (id: number) => void; onPublication: (id: number) => void
}) {
  return <section className="serialization-panel serialization-library" aria-label="章节与发布历史">
    <Tabs destroyOnHidden items={[
      { key: 'chapters', label: '章节清单', children: <Chapters projectId={projectId} version={version} onOpen={onChapter} /> },
      { key: 'history', label: '发布记录', children: <History projectId={projectId} version={version} onOpen={onPublication} /> },
    ]} />
  </section>
}
