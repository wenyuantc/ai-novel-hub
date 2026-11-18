import { SearchOutlined } from '@ant-design/icons'
import { Alert, Button, Checkbox, Input, Popconfirm, Select, Skeleton, Space, Spin, Tabs } from 'antd'
import { useCallback, useState } from 'react'
import { chapterApi } from '../../api/chapters'
import type { EditorActorIdentity } from '../../api/client'
import HitList from '../../components/bookSearch/HitList'
import { OperationAlert } from '../../components/bookSearch/OperationAlert'
import RevisionHistory from '../../components/bookSearch/RevisionHistory'
import { useBookSearch } from '../../components/bookSearch/useBookSearch'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import './BookSearchPage.css'

export default function BookSearchPage() {
  const projectId = useProjectId()
  const { ready, canRead } = useProjectAccess(projectId)
  const user = useAuthStore((s) => s.user)
  const scope = useProjectStore((s) => s.scopeVersion)
  if (!ready || !user) return <Skeleton active />
  if (!canRead) return <Alert type="warning" showIcon message="没有读取此作品的权限" />
  return <Workspace key={`${projectId}:${user.id}:${scope}`} projectId={projectId} actor={{ id: user.id, createdAt: user.created_at }} />
}

function Workspace({ projectId, actor }: { projectId: number; actor: EditorActorIdentity }) {
  const { canEdit, canReadNow } = useProjectAccess(projectId)
  const work = useBookSearch(projectId, actor)
  const [tab, setTab] = useState('search')
  const [historyBusy, setHistoryBusy] = useState(false)
  const [query, setQuery] = useState('')
  const [replacement, setReplacement] = useState('')
  const [range, setRange] = useState(false)
  const [start, setStart] = useState<number>()
  const [end, setEnd] = useState<number>()
  const loadChapters = useCallback((signal: AbortSignal) => chapterApi.list(projectId, signal, canReadNow), [projectId, canReadNow])
  const chapters = useMentionQuery(projectId, 'search-range', loadChapters, range)
  useProjectRefresh(projectId, ['chapters'], chapters.reload)
  const options = (chapters.data ?? []).map((ch) => ({ value: ch.id, label: `第 ${ch.chapter_no} 章 · ${ch.title}` }))
  // 目录已按章号、ID 排序。旧作品可能有重号章节，按两个端点在目录中的位置判定。
  const first = options.findIndex((o) => o.value === start)
  const last = options.findIndex((o) => o.value === end)
  const reversed = last >= 0 && first > last
  const missingEndpoint = !!chapters.data && ((start !== undefined && first < 0) || (end !== undefined && last < 0))
  const invalidRange = range && (first < 0 || last < 0 || reversed)
  const immutable = work.saving || !!work.retry
  const searchDisabled = immutable || invalidRange || !query || Array.from(query).length > 512
  const previewDisabled = searchDisabled || replacement === query || Array.from(replacement).length > 2048
  const begin = (preview: boolean) => {
    if (preview ? (!canEdit || previewDisabled) : searchDisabled) return
    void work.load({ query, ...(range ? { start_chapter_id: start, end_chapter_id: end } : {}), ...(preview ? { replacement } : {}) })
  }
  const selectCount = work.selected.size
  const chapterCount = new Set([...work.selected.values()].map((hit) => hit.chapter_id)).size

  return <section className="book-search" aria-labelledby="book-search-title">
    <h1 id="book-search-title">全书搜索</h1>
    <Tabs activeKey={tab} onChange={setTab} items={[
      { key: 'search', label: '搜索正文', disabled: historyBusy }, { key: 'history', label: '修订记录', disabled: work.saving },
    ]} />
    {tab === 'history' ? <RevisionHistory projectId={projectId} actor={actor} onBusy={setHistoryBusy} /> : <>
      <form className="book-search-form" onSubmit={(e) => { e.preventDefault(); begin(false) }}>
        <div className="book-search-field"><label htmlFor="book-search-query">查找原文</label>
          <Input.TextArea id="book-search-query" placeholder="输入人名、地名或原文片段" autoSize={{ minRows: 1, maxRows: 4 }}
            value={query} disabled={immutable} onChange={(e) => { work.reset(); setQuery(e.target.value) }} />
        </div>
        {canEdit && <div className="book-search-field"><label htmlFor="book-search-replacement">替换为</label>
          <Input.TextArea id="book-search-replacement" placeholder="输入新文本，留空表示删除" autoSize={{ minRows: 1, maxRows: 4 }}
            value={replacement} disabled={immutable} onChange={(e) => { if (work.mode?.replacement !== undefined) work.reset(); setReplacement(e.target.value) }} />
        </div>}
        <div className="book-search-scope">
          <Checkbox checked={range} disabled={immutable} onChange={(e) => { work.reset(); setRange(e.target.checked) }}>限定章节范围</Checkbox>
          {range && <Space wrap>
            <Select aria-label="起始章节" placeholder="起始章节" showSearch optionFilterProp="label" value={start}
              disabled={immutable} loading={chapters.loading} options={options} onChange={(v) => { work.reset(); setStart(v) }} />
            <span>至</span>
            <Select aria-label="结束章节" placeholder="结束章节" showSearch optionFilterProp="label" value={end}
              disabled={immutable} loading={chapters.loading} options={options} onChange={(v) => { work.reset(); setEnd(v) }} />
          </Space>}
        </div>
        {range && chapters.error && <Alert type="error" showIcon message={chapters.error} action={<Button onClick={chapters.reload}>重试</Button>} />}
        {range && missingEndpoint && <Alert type="warning" showIcon message="所选章节已不存在，请重新选择范围。" />}
        {reversed && range && <Alert type="warning" showIcon message="结束章节须在起始章节之后，或与起始章节相同。" />}
        {Array.from(query).length > 512 && <Alert type="warning" showIcon message="查找原文最多 512 个字符" />}
        {canEdit && Array.from(replacement).length > 2048 && <Alert type="warning" showIcon message="替换文本最多 2048 个字符" />}
        <Space wrap><Button type="primary" htmlType="submit" icon={<SearchOutlined />} disabled={searchDisabled}>搜索正文</Button>
          {canEdit && <Button disabled={previewDisabled} onClick={() => begin(true)}>预览替换</Button>}
        </Space>
      </form>
      {work.error && <OperationAlert error={work.error} uncertain={work.retry ? '暂时无法确认提交结果。重试会核对同一次申请，也可到修订记录查看。' : undefined} />}
      {work.retry && <Space wrap><Button type="primary" loading={work.saving} onClick={() => void work.apply()}>重试同一批次</Button>
        <Button disabled={work.saving} onClick={work.reset}>重新查找</Button></Space>}
      {work.loading && <Spin aria-label="正在查找命中" />}
      {work.mode && work.result && <HitList projectId={projectId} mode={work.mode} result={work.result}
        selected={work.selected} disabled={immutable || work.loading} onSelect={work.select} onClear={work.clearSelection}
        onPage={(page) => { if (work.mode && work.result) void work.load(work.mode, page, work.result.snapshot) }} />}
      {work.mode?.replacement !== undefined && work.result && canEdit && !work.retry &&
        <div className="book-search-confirm book-search-bar"><strong>已选 {chapterCount} 章、{selectCount} 处</strong>
          <Popconfirm title="确认批量修订？" description={`将${work.mode.replacement === '' ? '删除' : '替换'}勾选的 ${selectCount} 处原文。`}
            okText="确认修订" cancelText="取消" disabled={!selectCount || work.saving || work.loading} onConfirm={() => void work.apply()}>
            <Button type="primary" disabled={!selectCount || work.loading} loading={work.saving}>应用所选替换</Button>
          </Popconfirm></div>}
    </>}
  </section>
}
