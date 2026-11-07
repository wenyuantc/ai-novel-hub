import { Alert, Button, Input, Pagination, Select, Space, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { planningApi } from '../../api/planning'
import { sceneApi } from '../../api/scenes'
import { scenePlanningRow } from '../scenes/scenePresentation'
import type { PlanningReferenceKind, PlanningRow, PlanningSource, PlanningTag, Subplot } from '../../api/types'
import { useMentionQuery as usePlanningQuery } from '../../hooks/useMentionQuery'
import { planningReferenceId, planningRowLabel, planningSourceKey } from '../../utils/planning'
import EntrySelect from '../entities/EntrySelect'
import type { EntrySelectQuery } from '../entities/entrySelectState'

interface EntryProps {
  projectId: number; kind: PlanningReferenceKind; value?: number[]; onChange?: (value: number[]) => void
  disabled?: boolean; single?: boolean; label: string
}
/** 分页搜索设定，不一次加载全书档案；关系值对应时间线。 */
export function PlanningEntrySelect({ projectId, kind, value = [], onChange, disabled, single, label }: EntryProps) {
  const loadEntries = useCallback(async (query: EntrySelectQuery, signal: AbortSignal) => {
    const result = await planningApi.entries(projectId, { kind, ...query }, signal)
    return {
      items: result.items.map((item) => ({
        value: planningReferenceId(item.reference),
        label: `${item.name}${item.series_shared ? '（系列共享）' : item.is_present === false ? '（历史关系）' : ''}`,
      })),
      total: result.total,
    }
  }, [projectId, kind])
  return <EntrySelect projectId={projectId} queryKey={`planning-entry:${kind}`} loadEntries={loadEntries} value={value} onChange={onChange} disabled={disabled} single={single} label={label} />
}

interface SubplotProps {
  projectId: number; value?: number[]; onChange?: (value: number[]) => void; disabled?: boolean; single?: boolean
  label?: string; known?: Subplot[]
}
export function PlanningSubplotSelect({ projectId, value = [], onChange, disabled, single, label = '选择参与支线', known = [] }: SubplotProps) {
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const load = useCallback((signal: AbortSignal) => planningApi.subplots(projectId, { q, page, page_size: 30 }, signal), [projectId, q, page])
  const result = usePlanningQuery(projectId, `subplot-select:${q}:${page}`, load, true, 120)
  const choices = new Map([...known, ...(result.data?.items ?? [])].map((item) => [item.id, { value: item.id, label: item.name }]))
  for (const id of value) if (!choices.has(id)) choices.set(id, { value: id, label: `支线 #${id}` })
  return <Space direction="vertical" size={4} style={{ width: '100%' }}>
    <Select<number | number[]> aria-label={label} style={{ width: '100%' }} mode={single ? undefined : 'multiple'} allowClear showSearch value={single ? value[0] : value}
      placeholder={label} disabled={disabled} filterOption={false} loading={result.loading} options={[...choices.values()]}
      onSearch={(next) => { setQ(next); setPage(1) }} onChange={(next) => onChange?.(Array.isArray(next) ? next : next === undefined ? [] : [next])}
      popupRender={(menu) => <>{menu}<div style={{ padding: 8 }}><Pagination simple size="small" current={page} pageSize={30} total={result.data?.total ?? 0} onChange={setPage} /></div></>} />
    {result.error && <Alert type="error" message={result.error} action={<Button size="small" onClick={result.reload}>重试</Button>} />}
  </Space>
}

interface TagProps {
  projectId: number; value?: number[]; onChange?: (value: number[]) => void; disabled?: boolean
  single?: boolean; label?: string; id?: string; known?: PlanningTag[]
}
export function PlanningTagSelect({ projectId, value = [], onChange, disabled, single, label = '选择标签', id, known = [] }: TagProps) {
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const load = useCallback((signal: AbortSignal) => planningApi.tags(projectId, { q, page, page_size: 30 }, signal), [projectId, q, page])
  const result = usePlanningQuery(projectId, `tag-select:${q}:${page}`, load, true, 120)
  const choices = new Map([...known.filter((tag) => value.includes(tag.id)), ...(result.data?.items ?? [])].map((tag) => [tag.id, { value: tag.id, label: tag.name }]))
  for (const tagId of value) if (!choices.has(tagId)) choices.set(tagId, { value: tagId, label: `标签 #${tagId}` })
  return <Space direction="vertical" size={4} style={{ width: '100%' }}>
    <Select<number | number[]> id={id} aria-label={label} style={{ width: '100%' }} mode={single ? undefined : 'multiple'} allowClear showSearch value={single ? value[0] : value}
      placeholder={label} disabled={disabled} filterOption={false} loading={result.loading} options={[...choices.values()]}
      onSearch={(next) => { setQ(next); setPage(1) }} onChange={(next) => onChange?.(Array.isArray(next) ? next : next === undefined ? [] : [next])}
      popupRender={(menu) => <>{menu}<div style={{ padding: 8 }}><Pagination simple size="small" current={page} pageSize={30} total={result.data?.total ?? 0} onChange={setPage} /></div></>} />
    {result.error && <Alert type="error" message={result.error} action={<Button size="small" onClick={result.reload}>重试</Button>} />}
  </Space>
}

interface SourceProps { projectId: number; value?: PlanningSource; onChange?: (source: PlanningSource | undefined) => void; disabled?: boolean; initialRow?: PlanningRow; label?: string; allowedKinds?: ('chapter' | 'plan' | 'scene')[] }
export function PlanningSourceSelect({ projectId, value, onChange, disabled, initialRow, label = '目标章节、场景或细纲', allowedKinds = ['chapter', 'plan', 'scene'] }: SourceProps) {
  const [kind, setKind] = useState<'chapter' | 'plan' | 'scene'>(value?.kind === 'scene' ? 'scene' : value?.kind === 'plan' ? 'plan' : 'chapter')
  const [q, setQ] = useState('')
  const [page, setPage] = useState(1)
  const load = useCallback((signal: AbortSignal) => planningApi.matrix(projectId, { source_kind: kind, granularity: kind === 'scene' ? 'scene' : 'chapter', q, page, page_size: 20, column_mode: 'custom', column_refs: [] }, signal), [projectId, kind, q, page])
  const result = usePlanningQuery(projectId, `source:${kind}:${q}:${page}`, load, true, 120)
  const rows = result.data?.rows ?? []
  const selectedKey = value ? planningSourceKey(value) : undefined
  const options = rows.map((row) => ({ value: planningSourceKey(row.source), label: planningRowLabel(row) }))
  if (value && !options.some((option) => option.value === selectedKey)) options.push({ value: selectedKey!, label: initialRow && initialRow.key === selectedKey ? planningRowLabel(initialRow) : `${value.kind === 'chapter' ? '章节' : value.kind === 'plan' ? '细纲' : value.kind === 'scene' ? '场景' : '待关联规划'}记录 #${value.id}` })
  const choose = (key: string | undefined) => {
    if (key === undefined) { onChange?.(undefined); return }
    const row = rows.find((item) => planningSourceKey(item.source) === key)
    if (row) onChange?.(row.source)
  }
  return <Space direction="vertical" size={8} style={{ width: '100%' }}>
    <Space wrap>
      <Select aria-label="目标来源" value={kind} disabled={disabled} onChange={(next) => { setKind(next); setPage(1) }} options={[{ value: 'chapter', label: '已有章节' }, { value: 'plan', label: '未建章细纲' }, { value: 'scene', label: '实际场景' }].filter((item) => allowedKinds.some((kind) => kind === item.value))} />
      <Input aria-label="搜索目标标题" allowClear placeholder="搜索标题" value={q} disabled={disabled} onChange={(e) => { setQ(e.target.value); setPage(1) }} maxLength={200} />
    </Space>
    <Select aria-label={label} style={{ width: '100%' }} value={selectedKey} options={options} onChange={choose} disabled={disabled} allowClear loading={result.loading} placeholder={label} />
    <Space wrap><Pagination simple size="small" current={page} pageSize={20} total={result.data?.total ?? 0} onChange={setPage} /><Typography.Text type="secondary">{result.data?.total ?? 0} 个可选位置</Typography.Text></Space>
    {result.error && <Alert type="error" message={result.error} action={<Button size="small" onClick={result.reload}>重试</Button>} />}
  </Space>
}

/** 场景待关联仅列原章的活动 Scene；不出现跨章或章级目标。 */
export function SameChapterSceneSelect({ projectId, chapterId, value, onChange, disabled }: { projectId: number; chapterId: number; value?: PlanningSource; onChange: (row: PlanningRow) => void; disabled?: boolean }) {
  const [page, setPage] = useState(1)
  const load = useCallback((signal: AbortSignal) => sceneApi.list(projectId, chapterId, { offset: (page - 1) * 30, limit: 30 }, signal), [projectId, chapterId, page])
  const result = usePlanningQuery(projectId, `same-chapter-scene:${chapterId}:${page}`, load)
  return <Space direction="vertical" style={{ width: '100%' }}><Select aria-label="选择原章场景" style={{ width: '100%' }} value={value?.id} disabled={disabled} loading={result.loading} placeholder="选择同一章的活动场景" options={result.data?.items.map((scene) => ({ value: scene.id, label: `场景 ${scene.sort + 1} · ${scene.title || '未命名'}` }))} onChange={(id) => { const scene = result.data?.items.find((item) => item.id === id); if (scene) onChange(scenePlanningRow(scene)) }} /><Pagination simple current={page} pageSize={30} total={result.data?.total ?? 0} onChange={setPage} />{result.error && <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}</Space>
}
