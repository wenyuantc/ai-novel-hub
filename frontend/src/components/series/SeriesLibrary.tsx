import { PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Empty, Input, List, Modal, Pagination, Select, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { seriesApi, type SeriesBook, type SeriesEntry, type SeriesImpactQuery, type SeriesKind } from '../../api/series'
import { useLoader } from '../../hooks/useLoader'
import { invalidateProject } from '../../utils/projectRefresh'
import { WORLD_CATEGORY_LABELS } from '../../utils/world'
import { SETTING_AI_MODES } from '../settingAi/settingAiOptions'
import SeriesEntryEditor from './SeriesEntryEditor'
import SeriesTransitionDialog from './SeriesTransitionDialog'
import { projectReference, seriesEntryName, seriesNeedsReview, seriesSettingResources } from './seriesState'
import { useSeriesAction } from './useSeriesAction'

const PAGE_SIZE = 20
const KINDS = [{ label: '全部类型', value: 'all' }, { label: '角色', value: 'character' }, { label: '世界观', value: 'world' }] as const

type LibraryDialog =
  | { mode: 'edit'; kind: SeriesKind; entry: SeriesEntry | null; revision: number }
  | { mode: 'transition'; request: SeriesImpactQuery; entry: SeriesEntry }
  | { mode: 'move'; entry: SeriesEntry }
  | { mode: 'use'; entry: SeriesEntry; revision: number }

interface Props {
  seriesId: number
  /** 外部成员动作发生时刷新目录版本，但保留打开的草稿。 */
  seriesRevision: number
  kind?: SeriesKind
  book?: { id: number; title: string }
  books?: SeriesBook[]
  focusedEntryId?: number
  onClearFocus?: () => void
  onChange?: () => void
}

export default function SeriesLibrary({
  seriesId, seriesRevision, kind, book, books, focusedEntryId, onClearFocus, onChange,
}: Props) {
  const { message } = App.useApp()
  const [filter, setFilter] = useState<SeriesKind | 'all'>('all')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const [dialog, setDialog] = useState<LibraryDialog | null>(null)
  const selectedKind = kind ?? (filter === 'all' ? undefined : filter)
  const listKey = `${seriesId}:${seriesRevision}:${selectedKind ?? ''}:${q}:${page}:${focusedEntryId ?? ''}`
  const load = useCallback(async () => {
    const data = focusedEntryId
      ? { items: [await seriesApi.entry(seriesId, focusedEntryId)], total: 1, revision: seriesRevision }
      : await seriesApi.entries(seriesId, { kind: selectedKind, q, page, page_size: PAGE_SIZE })
    return { key: listKey, data }
  }, [focusedEntryId, listKey, page, q, selectedKind, seriesId, seriesRevision])
  const result = useLoader(load, book ? { projectId: book.id, resources: ['project', 'characters', 'world'] } : undefined)
  const current = result.data?.key === listKey ? result.data.data : undefined
  const entries = (current?.items ?? []).filter((entry) => !book || !projectReference(entry, book.id))
  const canMutate = !!current && !result.loading && !result.error
  const revision = current?.revision ?? seriesRevision
  const changed = (entryKind: SeriesKind, projectIds: number[], text: string, removedFocused = false) => {
    for (const id of new Set(projectIds)) invalidateProject(id, seriesSettingResources(entryKind))
    if (removedFocused && focusedEntryId) onClearFocus?.()
    if (entries.length === 1 && page > 1 && removedFocused) setPage((old) => old - 1)
    result.reload()
    onChange?.()
    setDialog(null)
    message.success(text)
  }

  return <Card size={book ? 'small' : 'default'} title={book ? '尚未在本书使用的共享条目' : '系列共享设定'}
    extra={book ? <Link to={`/series/${seriesId}`}>管理共享库</Link> : <Space wrap>
      <Button icon={<PlusOutlined />} disabled={!canMutate} onClick={() => setDialog({ mode: 'edit', kind: 'character', entry: null, revision })}>共享角色</Button>
      <Button icon={<PlusOutlined />} disabled={!canMutate} onClick={() => setDialog({ mode: 'edit', kind: 'world', entry: null, revision })}>共享世界观</Button>
    </Space>}>
    <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
      {book && <Typography.Text type="secondary">可浏览全系列的基础档案；只有选择「在本书使用」的条目才进入本书的出场、规划、提及和 AI 候选。</Typography.Text>}
      {focusedEntryId ? <Button onClick={onClearFocus}>浏览全部共享条目</Button> :
        <Space wrap>
          {!kind && <Select aria-label="共享条目类型" options={[...KINDS]} value={filter}
            onChange={(value) => { setFilter(value); setPage(1) }} style={{ minWidth: 125 }} />}
          <Input.Search aria-label="搜索共享条目" placeholder="搜索共享条目" allowClear maxLength={100}
            onSearch={(value) => { setQ(value.trim()); setPage(1) }} />
        </Space>}
      {result.error && <Alert type="error" showIcon message={result.error}
        action={<Button size="small" onClick={result.reload}>重试</Button>} />}
      <Spin spinning={result.loading || (!current && !result.error)}>
        {current && <List dataSource={entries} rowKey="id"
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={book && current.total ? '本页没有未采用的共享条目' : '暂无匹配的共享条目'} /> }}
          renderItem={(entry) => {
            const text = entry.kind === 'character' ? entry.data.profile : entry.data.content
            return <List.Item>
              <Space direction="vertical" size={6} style={{ width: '100%', minWidth: 0 }}>
                <Space size={4} wrap>
                  <Typography.Text strong>{seriesEntryName(entry)}</Typography.Text>
                  <Tag>{entry.kind === 'character' ? '角色' : WORLD_CATEGORY_LABELS[entry.data.category]}</Tag>
                  <Tag>{book ? '仅浏览' : `${entry.uses.length} 本作品采用`}</Tag>
                </Space>
                {entry.data.aliases.length > 0 && <Typography.Text type="secondary">{entry.data.aliases.join('、')}</Typography.Text>}
                {text && <Typography.Paragraph ellipsis={{ rows: 2, expandable: true, symbol: '展开' }} style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}>{text}</Typography.Paragraph>}
                <Typography.Text type="secondary">AI：{SETTING_AI_MODES[entry.ai_settings.ai_mode].label}</Typography.Text>
                <Space size={4} wrap>
                  {book ? <>
                    <Button type="primary" size="small" disabled={!canMutate} onClick={() => setDialog({ mode: 'use', entry, revision })}>在本书使用</Button>
                    <Link to={`/series/${seriesId}?entry=${entry.id}`}>查看共享基础</Link>
                  </> : <>
                    <Button size="small" disabled={!canMutate} onClick={() => setDialog({ mode: 'edit', kind: entry.kind, entry, revision })}>编辑共享基础</Button>
                    <Button size="small" disabled={!canMutate || !books?.some((item) => !item.recycled)} onClick={() => setDialog({ mode: 'move', entry })}>收回指定作品</Button>
                    <Button danger size="small" disabled={!canMutate} onClick={() => setDialog({ mode: 'transition', request: { action: 'delete', entry_id: entry.id }, entry })}>删除</Button>
                  </>}
                </Space>
              </Space>
            </List.Item>
          }} />}
      </Spin>
      {!focusedEntryId && current && current.total > PAGE_SIZE && <Pagination
        current={page} total={current.total} pageSize={PAGE_SIZE} showSizeChanger={false} onChange={setPage} size="small" />}
    </Space>
    {dialog?.mode === 'edit' && <SeriesEntryEditor seriesId={seriesId} kind={dialog.kind} entry={dialog.entry} revision={dialog.revision}
      onClose={() => setDialog(null)} onDone={(value, previous) => changed(dialog.kind,
        [...(previous?.uses ?? []), ...(value.item?.uses ?? [])].map((use) => use.project_id), '共享基础已保存')} />}
    {dialog?.mode === 'transition' && <SeriesTransitionDialog seriesId={seriesId} request={dialog.request}
      label={seriesEntryName(dialog.entry)} onClose={() => setDialog(null)} onDone={(ids) => {
        changed(dialog.entry.kind, ids, '归属已更新', dialog.request.action === 'delete' || dialog.request.action === 'move')
      }} />}
    {dialog?.mode === 'move' && <MoveTargetDialog entry={dialog.entry} books={books ?? []} onClose={() => setDialog(null)}
      onPick={(target) => setDialog({ mode: 'transition', entry: dialog.entry, request: { action: 'move', project_id: target, entry_id: dialog.entry.id } })} />}
    {dialog?.mode === 'use' && book && <UseDialog seriesId={seriesId} entry={dialog.entry} book={book} revision={dialog.revision}
      onClose={() => setDialog(null)} onRefresh={() => { setDialog(null); result.reload(); onChange?.() }}
      onDone={() => changed(dialog.entry.kind, [book.id], '已在本书使用')} />}
  </Card>
}

function MoveTargetDialog({ entry, books, onClose, onPick }: {
  entry: SeriesEntry; books: SeriesBook[]; onClose: () => void; onPick: (id: number) => void
}) {
  const [target, setTarget] = useState<number>()
  return <Modal open title={`收回「${seriesEntryName(entry)}」`} onCancel={onClose} onOk={() => { if (target) onPick(target) }}
    okText="检查影响" cancelText="取消" okButtonProps={{ disabled: !target }}>
    <Select<number> aria-label="收回到哪部作品" placeholder="选择目标作品" style={{ width: '100%' }} value={target}
      options={books.filter((book) => !book.recycled).map((book) => ({ value: book.id, label: book.title }))} onChange={setTarget} />
  </Modal>
}

function UseDialog({ seriesId, entry, book, revision, onClose, onRefresh, onDone }: {
  seriesId: number; entry: SeriesEntry; book: { id: number; title: string }; revision: number
  onClose: () => void; onRefresh: () => void; onDone: () => void
}) {
  const action = useSeriesAction()
  const stale = action.error !== null && seriesNeedsReview(action.error)
  return <Modal open title={`在《${book.title}》使用「${seriesEntryName(entry)}」`}
    onCancel={onClose} onOk={() => { if (!stale) void action.run(() => seriesApi.use(seriesId, entry.id, book.id, revision), onDone) }}
    okText="在本书使用" cancelText="取消" confirmLoading={action.busy} okButtonProps={{ disabled: stale }}
    maskClosable={!action.busy} closable={!action.busy} keyboard={!action.busy} cancelButtonProps={{ disabled: action.busy }}>
    <Typography.Paragraph>此条目将可用于本书的出场、规划与提及。AI 仍遵守既有条目和字段可见规则；采用后沿用本书现有读取权限，不会扩大读者对系列库或其他作品的访问。</Typography.Paragraph>
    {!!action.error && <Alert type="error" showIcon message={errorMessage(action.error)}
      action={stale ? <Button size="small" onClick={onRefresh}>重新读取共享库</Button> : undefined} />}
  </Modal>
}
