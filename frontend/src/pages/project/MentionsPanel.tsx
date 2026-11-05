import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Drawer, Empty, Form, Input, InputNumber, Pagination, Select, Skeleton, Space, Table, Tabs, Tag, Timeline, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { mentionApi, MENTION_CATEGORY_LABELS } from '../../api/mentions'
import type { MentionCategory, MentionChapter, MentionEntity, MentionSummaryEntry } from '../../api/mentions'
import AutoGrid from '../../components/AutoGrid'
import EntityMentions from '../../components/mentions/EntityMentions'
import MentionHeatmap from '../../components/mentions/MentionHeatmap'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import {
  chapterWindow,
  MENTION_CHAPTER_WINDOW,
  MENTION_ENTITY_PAGE_SIZE,
  mentionChapterRows,
  mentionEntityKey,
  mentionRangeError,
  mentionScopeLabel,
  mentionSummaryQuery,
  mentionTimeline,
} from '../../utils/mentionStats'
import type { MentionChapterRow, MentionStatsFilter } from '../../utils/mentionStats'

interface FilterValues {
  category?: MentionCategory
  entityKey?: string
  q?: string
  chapterFrom?: number | null
  chapterTo?: number | null
}

function StatsFilters({ entities, loading, onApply }: {
  entities: MentionEntity[]
  loading: boolean
  onApply: (filter: MentionStatsFilter) => void
}) {
  const [form] = Form.useForm<FilterValues>()
  const category: MentionCategory | undefined = Form.useWatch('category', form)
  const [error, setError] = useState('')
  const submit = (values: FilterValues) => {
    const chapterFrom = values.chapterFrom ?? undefined
    const chapterTo = values.chapterTo ?? undefined
    const issue = mentionRangeError(chapterFrom, chapterTo)
    const entity = values.entityKey ? entities.find((item) => mentionEntityKey(item) === values.entityKey) : undefined
    if (issue || (values.entityKey && !entity)) {
      setError(issue || '这个条目已不存在，请重新选择')
      return
    }
    setError('')
    onApply({ category: values.category, entity, q: values.q, chapterFrom, chapterTo })
  }
  return <Form<FilterValues> form={form} layout="vertical" onFinish={submit}
    onValuesChange={(changed: Partial<FilterValues>) => {
      if ('category' in changed) form.setFieldValue('entityKey', undefined)
    }}>
    <AutoGrid minItemWidth={180} gap={12}>
      <Form.Item name="category" label="条目类型" style={{ marginBottom: 0 }}>
        <Select allowClear placeholder="全部类型" options={Object.entries(MENTION_CATEGORY_LABELS).map(([value, label]) => ({ value, label }))} />
      </Form.Item>
      <Form.Item name="entityKey" label="指定条目" style={{ marginBottom: 0 }}>
        <Select allowClear showSearch optionFilterProp="label" placeholder="全部条目" loading={loading}
          options={entities.filter((entity) => !category || entity.category === category).map((entity) => ({
            value: mentionEntityKey(entity), label: `${entity.name} · ${MENTION_CATEGORY_LABELS[entity.category]} #${entity.id}`,
          }))} />
      </Form.Item>
      <Form.Item name="q" label="名称或别名" style={{ marginBottom: 0 }}><Input allowClear placeholder="筛选条目" maxLength={200} /></Form.Item>
      <Form.Item name="chapterFrom" label="起始章" style={{ marginBottom: 0 }}><InputNumber min={1} precision={0} placeholder="不限" style={{ width: '100%' }} /></Form.Item>
      <Form.Item name="chapterTo" label="结束章" style={{ marginBottom: 0 }}><InputNumber min={1} precision={0} placeholder="不限" style={{ width: '100%' }} /></Form.Item>
    </AutoGrid>
    <Space wrap style={{ marginTop: 16 }}>
      <Button type="primary" htmlType="submit">应用筛选</Button>
      <Button onClick={() => { form.resetFields(); setError(''); onApply({}) }}>重置</Button>
    </Space>
    {error && <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />}
  </Form>
}

function ChapterData({ rows, onSelect }: { rows: MentionChapterRow[]; onSelect: (entity: MentionEntity, chapter?: MentionChapter) => void }) {
  const columns: TableColumnsType<MentionChapterRow> = [
    { title: '条目', key: 'entity', width: 160, render: (_, row) => <span>{row.entity.name} · {MENTION_CATEGORY_LABELS[row.entity.category]}</span> },
    { title: '章节', key: 'chapter', width: 180, render: (_, row) => `第 ${row.chapter.chapter_no} 章 · ${row.chapter.title}` },
    { title: '确定提及', dataIndex: 'count', width: 90, render: (count: number, row) => row.chapter.has_content ? count : '—' },
    { title: '有歧义', dataIndex: 'ambiguous_count', width: 90, render: (count: number, row) => row.chapter.has_content ? count : '—' },
    { title: '状态', key: 'state', width: 120, render: (_, row) => !row.chapter.has_content ? '未写' : row.count || row.ambiguous_count ? '有提及' : '未提及' },
    { title: '原文', key: 'open', width: 100, render: (_, row) => <Button type="link" size="small"
      disabled={!row.chapter.has_content || (!row.count && !row.ambiguous_count)} onClick={() => onSelect(row.entity, row.chapter)}>查看提及</Button> },
  ]
  return <Table<MentionChapterRow> aria-label="逐章提及数据表" size="small" rowKey="key" columns={columns} dataSource={rows}
    scroll={{ x: 740 }} pagination={{ pageSize: 20, showSizeChanger: false, responsive: true, showTotal: (total) => `当前窗口共 ${total} 个条目章节组合` }} />
}

/** 项目正文分布统计；四类来源的具体位置由统一提及列表承接。 */
export default function MentionsPanel() {
  const pid = useProjectId()
  const { ready, canRead } = useProjectAccess(pid)
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  if (!ready) return <Skeleton active />
  if (!canRead) return <Alert type="warning" showIcon message="当前无权查看此作品的提及" />
  return <MentionsContent key={`${pid}:${identity}`} projectId={pid} />
}

function MentionsContent({ projectId }: { projectId: number }) {
  const { canReadNow } = useProjectAccess(projectId)
  const drawerWidth = useModalWidth(760)
  const [filter, setFilter] = useState<MentionStatsFilter>({})
  const [page, setPage] = useState(1)
  const [chapterPage, setChapterPage] = useState(1)
  const [timelineEntityKey, setTimelineEntityKey] = useState('')
  const [selected, setSelected] = useState<{ entity: MentionEntity; chapter?: MentionChapter } | null>(null)
  const query = useMemo(() => mentionSummaryQuery(filter, page), [filter, page])
  const catalogLoad = useCallback((signal: AbortSignal) => mentionApi.catalog(projectId, signal), [projectId])
  const summaryLoad = useCallback((signal: AbortSignal) => mentionApi.summary(projectId, query, signal), [projectId, query])
  const catalog = useMentionQuery(projectId, 'catalog', catalogLoad)
  const summary = useMentionQuery(projectId, JSON.stringify(query), summaryLoad)
  const { reload: reloadCatalog } = catalog
  const { reload: reloadSummary } = summary
  const refresh = useCallback(() => { reloadCatalog(); reloadSummary() }, [reloadCatalog, reloadSummary])
  useProjectRefresh(projectId, ['chapters', 'characters', 'world'], refresh)
  const chapters = useMemo(() => chapterWindow(summary.data?.chapters ?? [], chapterPage), [summary.data?.chapters, chapterPage])
  const entries = useMemo(() => summary.data?.entries ?? [], [summary.data?.entries])
  const rows = useMemo(() => mentionChapterRows(entries, chapters.items), [entries, chapters.items])
  const timelineEntry = entries.find((entry) => mentionEntityKey(entry.entity) === timelineEntityKey) ?? entries[0]
  const timelineRows = mentionTimeline(rows.filter((row) => timelineEntry && mentionEntityKey(row.entity) === mentionEntityKey(timelineEntry.entity)))
  const select = (entity: MentionEntity, chapter?: MentionChapter) => {
    if (canReadNow()) setSelected({ entity, chapter })
  }
  const columns: TableColumnsType<MentionSummaryEntry> = [
    { title: '条目', key: 'name', width: 180, render: (_, entry) => <Space direction="vertical" size={0}>
      <Button type="link" size="small" onClick={() => select(entry.entity)} style={{ padding: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left' }}>{entry.entity.name}</Button>
      <Typography.Text type="secondary">{MENTION_CATEGORY_LABELS[entry.entity.category]}</Typography.Text>
    </Space> },
    { title: '确定提及', dataIndex: 'mention_count', width: 100 },
    { title: '有歧义', dataIndex: 'ambiguous_count', width: 90 },
    { title: '提及章节', dataIndex: 'chapter_count', width: 100 },
    { title: '最近确定提及', dataIndex: 'last_chapter_no', width: 140, render: (value: number | null) => value === null ? '未提及' : `第 ${value} 章` },
    { title: '连续未提及', dataIndex: 'gap_chapters', width: 150, render: (value: number | null) => value === null ? '从未确定提及' : `${value} 个已写章节` },
  ]
  const apply = (next: MentionStatsFilter) => {
    setFilter(next); setPage(1); setChapterPage(1); setSelected(null)
  }

  return <Space direction="vertical" size="large" style={{ width: '100%', minWidth: 0 }}>
    <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 12 }}>
      <div>
        <Typography.Title level={2} style={{ margin: 0 }}>提及与出场分布</Typography.Title>
        <Typography.Text type="secondary">查看设定条目在正文中的分布，定位长期未提及或集中出现的位置。</Typography.Text>
      </div>
      <Button icon={<ReloadOutlined />} onClick={refresh} loading={summary.loading || catalog.loading}>刷新统计</Button>
    </div>
    <Card size="small" title="筛选统计范围"><StatsFilters entities={catalog.data?.entities ?? []} loading={catalog.loading} onApply={apply} /></Card>
    <Alert type="info" showIcon message={mentionScopeLabel(filter.chapterFrom, filter.chapterTo)}
      description="统计名称与别名的文本提及，不推断实际剧情在场。聊天、笔记、资料和未保存草稿不计入本页；未写章节不算缺席，有歧义的称呼单独计数。" />
    {(catalog.error || summary.error) && <Alert type="error" showIcon message="提及统计加载失败" description={catalog.error || summary.error}
      action={<Button onClick={refresh}>重试</Button>} />}
    {summary.loading ? <Skeleton active paragraph={{ rows: 6 }} /> : summary.data && <>
      {!summary.data.total ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合条件的角色、地点或物品">
        <Space wrap><Link to={`/projects/${projectId}/characters`}>管理角色</Link><Link to={`/projects/${projectId}/world`}>管理地点与物品</Link></Space>
      </Empty> : <>
        <Card size="small" title="提及频次" extra={<Typography.Text type="secondary">范围内共 {summary.data.total} 个条目</Typography.Text>}>
          <Table<MentionSummaryEntry> size="small" aria-label="提及频次统计表" rowKey={(entry) => mentionEntityKey(entry.entity)} columns={columns}
            dataSource={entries} scroll={{ x: 760 }} pagination={false} />
          {!entries.length && page > 1 && <Button onClick={() => setPage(1)} style={{ marginTop: 12 }}>返回第一页</Button>}
          <Pagination current={page} total={summary.data.total} pageSize={MENTION_ENTITY_PAGE_SIZE} showSizeChanger={false} responsive
            onChange={(next) => { setPage(next); setSelected(null) }} style={{ marginTop: 16 }} />
        </Card>
        <Card size="small" title="章节分布">
          <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
            <Space wrap>
              <Typography.Text>章节窗口：{chapters.items.length ? `第 ${chapters.items[0].chapter_no}–${chapters.items[chapters.items.length - 1].chapter_no} 章` : '暂无章节'}</Typography.Text>
              <Typography.Text type="secondary">每次最多展示 {MENTION_CHAPTER_WINDOW} 章；上方频次按完整筛选范围统计。</Typography.Text>
            </Space>
            {chapters.pages > 1 && <Pagination simple current={chapters.page} total={chapters.total} pageSize={MENTION_CHAPTER_WINDOW} showSizeChanger={false}
              onChange={setChapterPage} aria-label="章节窗口分页" />}
            <Tabs items={[
              { key: 'heatmap', label: '热力图', children: <MentionHeatmap entries={entries} chapters={chapters.items} onSelect={select} /> },
              { key: 'timeline', label: '提及时间线', children: <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <Select aria-label="时间线条目" style={{ width: 'min(320px, 100%)' }} value={timelineEntry ? mentionEntityKey(timelineEntry.entity) : undefined}
                  onChange={setTimelineEntityKey} options={entries.map((entry) => ({ value: mentionEntityKey(entry.entity), label: `${entry.entity.name} · ${MENTION_CATEGORY_LABELS[entry.entity.category]}` }))} />
                {!timelineRows.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此条目在当前章节窗口中没有提及" /> :
                  <Timeline items={timelineRows.map((row) => ({
                    key: row.key,
                    children: <Space direction="vertical" size={4}>
                      <Button type="link" onClick={() => select(row.entity, row.chapter)} style={{ padding: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left' }}>
                        第 {row.chapter.chapter_no} 章 · {row.chapter.title}
                      </Button>
                      <Space wrap><span>{row.count} 次确定提及</span>{row.ambiguous_count > 0 && <Tag color="warning">{row.ambiguous_count} 次有歧义</Tag>}</Space>
                    </Space>,
                  }))} />}
              </Space> },
              { key: 'data', label: '逐章数值表', children: <ChapterData key={`${JSON.stringify(query)}:${chapters.page}`} rows={rows} onSelect={select} /> },
            ]} />
          </Space>
        </Card>
      </>}
    </>}
    <Drawer open={selected !== null} title={selected ? `${selected.entity.name} · ${selected.chapter ? `第 ${selected.chapter.chapter_no} 章提及` : '正文提及'}` : '正文提及'}
      width={drawerWidth} destroyOnHidden onClose={() => setSelected(null)}>
      {selected && <EntityMentions projectId={projectId} entity={selected.entity} sourceType="chapter"
        chapterFrom={selected.chapter?.chapter_no ?? filter.chapterFrom} chapterTo={selected.chapter?.chapter_no ?? filter.chapterTo} />}
    </Drawer>
  </Space>
}
