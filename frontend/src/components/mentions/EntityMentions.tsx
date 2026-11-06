import { Alert, Button, Empty, List, Pagination, Select, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { mentionApi, MENTION_SOURCE_LABELS, type MentionEntity, type MentionSourceType } from '../../api/mentions'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { MENTION_DICTIONARY_RESOURCES, mentionHref } from '../../utils/mentions'

export interface EntityMentionsProps { projectId: number; entity?: Pick<MentionEntity, 'kind' | 'id'>; chapterFrom?: number; chapterTo?: number; sourceType?: MentionSourceType }
export default function EntityMentions(props: EntityMentionsProps) {
  return <MentionList key={`${props.projectId}:${props.entity?.kind}:${props.entity?.id}:${props.chapterFrom}:${props.chapterTo}:${props.sourceType}`} {...props} />
}
function MentionList({ projectId, entity, chapterFrom, chapterTo, sourceType }: EntityMentionsProps) {
  const [source, setSource] = useState<MentionSourceType | undefined>(sourceType)
  const [page, setPage] = useState(1)
  const catalogLoad = useCallback((signal: AbortSignal) => mentionApi.catalog(projectId, signal), [projectId])
  const catalog = useMentionQuery(projectId, 'catalog', catalogLoad)
  const kind = entity?.kind, id = entity?.id
  const load = useCallback((signal: AbortSignal) => mentionApi.list(projectId, { entity_kind: kind, entity_id: id, source_type: source, chapter_from: chapterFrom, chapter_to: chapterTo, page, page_size: 20 }, signal), [projectId, kind, id, source, chapterFrom, chapterTo, page])
  const result = useMentionQuery(projectId, `${kind}:${id}:${source}:${chapterFrom}:${chapterTo}:${page}`, load)
  const refresh = () => { catalog.reload(); result.reload() }
  useProjectRefresh(projectId, MENTION_DICTIONARY_RESOURCES, refresh)
  useProjectRefresh(projectId, ['chapters', 'notes', 'knowledge', 'chat'], result.reload)
  const notTracked = !!entity && !!catalog.data && !catalog.data.entities.some((item) => item.kind === kind && item.id === id)
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Space wrap>{sourceType ? <Tag>{MENTION_SOURCE_LABELS[sourceType]}</Tag> : <Select aria-label="提及来源" style={{ minWidth: 140 }} value={source ?? 'all'} onChange={(value: MentionSourceType | 'all') => { setSource(value === 'all' ? undefined : value); setPage(1) }} options={[{ value: 'all', label: '全部来源' }, ...(catalog.data?.available_source_types ?? []).map((value) => ({ value, label: MENTION_SOURCE_LABELS[value] }))]} />}<Button onClick={refresh}>刷新提及</Button></Space>
    <Typography.Text type="secondary">按当前已保存文本识别；有歧义的称呼会列出候选，不代表确定出场。</Typography.Text>
    {(result.error || catalog.error) && <Alert type="error" showIcon message={result.error || catalog.error} action={<Button onClick={refresh}>重试</Button>} />}
    {result.loading ? <Spin /> : result.data && <>
      {!result.data.items.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={notTracked ? '该条目未开启提及追踪，可在「AI 与提及」中调整。' : '此范围内没有提及'} /> : <List dataSource={result.data.items} renderItem={(hit) => <List.Item key={`${hit.source.type}:${hit.source.id}:${hit.start}`} style={{ display: 'block' }}>
        <Space wrap><Tag>{MENTION_SOURCE_LABELS[hit.source.type]}</Tag><Link to={mentionHref(projectId, hit.source, hit)}>{hit.source.chapter_no !== null ? `第 ${hit.source.chapter_no} 章 · ` : ''}{hit.source.title || '未命名'}</Link>{hit.entities.length > 1 && <Tag color="warning">归属有歧义</Tag>}</Space>
        <Typography.Paragraph style={{ margin: '8px 0', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{hit.context_before}<Typography.Text mark strong>{hit.text}</Typography.Text>{hit.context_after}</Typography.Paragraph>
        {hit.entities.length > 1 && <Typography.Text type="secondary">候选：{hit.entities.map((e) => e.name).join('、')}</Typography.Text>}
      </List.Item>} />}
      <Pagination current={page} total={result.data.total} pageSize={20} showSizeChanger={false} onChange={setPage} showTotal={(total) => `共 ${total} 处提及`} />
    </>}
  </Space>
}
