import { ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Input, InputNumber, Pagination, Result, Segmented, Select, Skeleton, Space, Tag, Typography } from 'antd'
import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { planningApi } from '../../api/planning'
import type { PlanningMatrixQuery, PlanningWeaveQuery } from '../../api/planning'
import type { MatrixCell, MatrixColumn, MatrixColumnRef, PlanningRow, SubplotCrossing } from '../../api/types'
import { StudioPage } from '../../components/StudioUI'
import PlanningBatchModal from '../../components/planning/PlanningBatchModal'
import PlanningCatalog from '../../components/planning/PlanningCatalog'
import PlanningCellDrawer from '../../components/planning/PlanningCellDrawer'
import PlanningColumnPicker from '../../components/planning/PlanningColumnPicker'
import PlanningCrossingList from '../../components/planning/PlanningCrossingList'
import PlanningCrossingModal from '../../components/planning/PlanningCrossingModal'
import PlanningDetachedDrawer from '../../components/planning/PlanningDetachedDrawer'
import { PlanningSubplotSelect, PlanningTagSelect } from '../../components/planning/PlanningFields'
import PlanningMatrixTable from '../../components/planning/PlanningMatrixTable'
import PlanningMergeModal from '../../components/planning/PlanningMergeModal'
import PlanningNodeModal from '../../components/planning/PlanningNodeModal'
import PlanningPreviewDrawer from '../../components/planning/PlanningPreviewDrawer'
import PlanningWeaveTable from '../../components/planning/PlanningWeaveTable'
import SceneGrid from '../../components/scenes/SceneGrid'
import { useMentionQuery as usePlanningQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { PLANNING_REFERENCE_LABELS } from '../../utils/planning'
import type { PlanningMergeConflict } from '../../utils/planning'
import './PlanningPanel.css'

const resources = ['planning', 'chapters', 'outlines', 'characters', 'world', 'organizations', 'relationships', 'careers', 'setting-ai-visibility'] as const
interface CrossingEditor { row?: PlanningRow; crossing?: SubplotCrossing; initialSubplotIds?: number[]; revision: number }
/** 作品与授权变更时重建局部工作台，旧选择和草稿不跨身份复用。 */
export default function PlanningPanel() {
  const pid = useProjectId()
  const access = useProjectAccess(pid)
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  if (!access.ready) return <Skeleton active />
  if (!access.canRead) return <Result status="403" title="没有查看本作品规划的权限" />
  return <PlanningWorkspace key={`${pid}:${identity}`} projectId={pid} />
}
function PlanningWorkspace({ projectId }: { projectId: number }) {
  const access = useProjectAccess(projectId)
  const { message } = App.useApp()
  const [view, setView] = useState<'matrix' | 'weave' | 'scenes'>('matrix')
  const [query, setQuery] = useState<PlanningMatrixQuery>({ page: 1, page_size: 50, column_page: 1, column_mode: 'settings' })
  const [weaveQuery, setWeaveQuery] = useState<PlanningWeaveQuery>({ axis: 'chapter', page: 1, page_size: 30, subplot_page: 1, subplot_page_size: 20, gap_threshold: 5 })
  const [selection, setSelection] = useState<{ scope: string; keys: string[] }>({ scope: '', keys: [] })
  const [batch, setBatch] = useState<{ rows: PlanningRow[]; revision: number }>()
  const [node, setNode] = useState<{ row: PlanningRow; subplotId?: number }>()
  const [crossing, setCrossing] = useState<CrossingEditor>()
  const [cell, setCell] = useState<{ row: PlanningRow; column: MatrixColumn; cell: MatrixCell; revision: number }>()
  const [preview, setPreview] = useState<PlanningRow>()
  const [catalog, setCatalog] = useState<'subplots' | 'tags'>()
  const [columnsOpen, setColumnsOpen] = useState(false)
  const [detachedOpen, setDetachedOpen] = useState(false)
  const [crossingsList, setCrossingsList] = useState<{ row?: PlanningRow }>()
  const [merge, setMerge] = useState<PlanningMergeConflict>()
  const matrixLoad = useCallback((signal: AbortSignal) => planningApi.matrix(projectId, query, signal), [projectId, query])
  const matrixKey = JSON.stringify(query)
  const matrix = usePlanningQuery(projectId, `matrix:${matrixKey}`, matrixLoad, view === 'matrix', 120)
  const weaveLoad = useCallback((signal: AbortSignal) => planningApi.weave(projectId, weaveQuery, signal), [projectId, weaveQuery])
  const weave = usePlanningQuery(projectId, `weave:${JSON.stringify(weaveQuery)}`, weaveLoad, view === 'weave', 120)
  const refresh = () => { matrix.reload(); weave.reload(); setSelection({ scope: '', keys: [] }) }
  useProjectRefresh(projectId, resources, refresh)
  const currentRevision = view === 'matrix' ? matrix.data?.revision : weave.data?.revision
  const scope = `${matrixKey}:${matrix.data?.revision ?? ''}`
  const selectedKeys = selection.scope === scope ? selection.keys.filter((key) => matrix.data?.rows.some((row) => row.key === key)) : []
  const selectedRows = useMemo(() => matrix.data?.rows.filter((row) => selection.scope === `${matrixKey}:${matrix.data?.revision ?? ''}` && selection.keys.includes(row.key)) ?? [], [matrix.data, matrixKey, selection])
  const updateQuery = (patch: Partial<PlanningMatrixQuery>) => { setQuery((old) => ({ ...old, ...patch, page: 1, column_page: 1 })); setSelection({ scope: '', keys: [] }) }
  const saved = () => { if (access.canReadNow()) { refresh(); message.success('规划已保存') } }
  const editRow = (row: PlanningRow, revision = currentRevision) => { if (revision !== undefined) setBatch({ rows: [row], revision }) }
  const openNode = (row: PlanningRow, subplotId?: number) => { setCell(undefined); setNode({ row, subplotId }) }
  const openCrossing = (event: SubplotCrossing, revision = currentRevision) => { if (revision !== undefined) { setCell(undefined); setCrossing({ crossing: event, revision }) } }
  const newCrossing = (row?: PlanningRow, subplotIds: number[] = []) => { if (currentRevision !== undefined) { setCell(undefined); setCrossing({ row, initialSubplotIds: subplotIds, revision: currentRevision }) } }
  const applyColumns = (column_refs: MatrixColumnRef[]) => updateQuery({ column_mode: 'custom', column_refs })
  return <StudioPage title="规划矩阵与支线" hint="安排视角与设定，让每条支线在合适的位置推进、交汇。" action={<Space wrap>
    <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button><Link to={`/projects/${projectId}/outlines`}><Button>打开大纲</Button></Link>
  </Space>}>
    <div className="planning-workspace"><Space direction="vertical" size={16} style={{ width: '100%' }}>
      {!access.canEdit && <Alert type="info" showIcon message="当前为只读查看，规划和 AI 内容预览仍可使用。" />}
      <div className="planning-toolbar"><Segmented aria-label="规划视图" value={view} onChange={(next) => { setView(next as 'matrix' | 'weave' | 'scenes'); setSelection({ scope: '', keys: [] }) }} options={[{ value: 'matrix', label: '规划矩阵' }, { value: 'scenes', label: '场景卡片' }, { value: 'weave', label: '支线交织' }]} />
        <Button onClick={() => setCatalog('subplots')}>支线管理</Button><Button onClick={() => setCatalog('tags')}>标签管理</Button><Button onClick={() => setCrossingsList({})}>明确交汇</Button><Button onClick={() => setDetachedOpen(true)}>待重新关联</Button>
        <Button type="primary" disabled={!access.canEdit || currentRevision === undefined} onClick={() => newCrossing()}>新建交汇事件</Button>
      </div>
      {view === 'matrix' ? <>
        <Card size="small"><div className="planning-filters">
          <div className="planning-filter"><label htmlFor="planning-source-filter">规划位置</label><Select id="planning-source-filter" style={{ width: '100%' }} value={query.source_kind ?? 'all'} options={[{ value: 'all', label: '全部章节与细纲' }, { value: 'chapter', label: '已有章节' }, { value: 'plan', label: '未建章细纲' }, { value: 'scene', label: '实际场景（按章分组）' }]} onChange={(value: 'all' | 'chapter' | 'plan' | 'scene') => updateQuery({ source_kind: value === 'all' ? undefined : value, granularity: value === 'scene' ? 'scene' : 'chapter' })} /></div>
          <div className="planning-filter"><label htmlFor="planning-row-search">章节／细纲标题</label><Input id="planning-row-search" value={query.q ?? ''} placeholder="搜索规划位置" maxLength={200} allowClear onChange={(event) => updateQuery({ q: event.target.value || undefined })} /></div>
          <div className="planning-filter"><label htmlFor="planning-volume-filter">分卷</label><InputNumber id="planning-volume-filter" min={1} precision={0} style={{ width: '100%' }} placeholder="全部分卷" value={query.volume_no} onChange={(value) => updateQuery({ volume_no: value ?? undefined })} /></div>
          <div className="planning-filter"><label htmlFor="planning-tag-filter">标签</label><PlanningTagSelect projectId={projectId} id="planning-tag-filter" single label="全部标签" value={query.tag_id ? [query.tag_id] : []} onChange={(ids) => updateQuery({ tag_id: ids[0] })} /></div>
        </div><div className="planning-toolbar" style={{ marginTop: 16 }}>
          <Select aria-label="矩阵列模式" value={query.column_mode} options={[{ value: 'settings', label: '设定分布' }, { value: 'subplots', label: '支线分布' }, { value: 'custom', label: '自选混合列' }]} onChange={(column_mode: 'settings' | 'subplots' | 'custom') => { updateQuery({ column_mode, column_refs: column_mode === 'custom' ? query.column_refs : undefined }); if (column_mode === 'custom') setColumnsOpen(true) }} />
          {query.column_mode === 'settings' && <><Select aria-label="设定列类型" value={query.entity_kind} allowClear placeholder="全部设定类型" options={Object.entries(PLANNING_REFERENCE_LABELS).map(([value, label]) => ({ value, label }))} onChange={(entity_kind) => updateQuery({ entity_kind })} /><Input aria-label="搜索展示条目" style={{ width: 200 }} value={query.entry_q ?? ''} allowClear maxLength={200} placeholder="搜索设定名称" onChange={(event) => updateQuery({ entry_q: event.target.value || undefined })} /></>}
          <Button onClick={() => setColumnsOpen(true)}>选择展示列</Button><Button onClick={() => { setQuery({ page: 1, page_size: 50, column_page: 1, column_mode: 'settings' }); setSelection({ scope: '', keys: [] }) }}>重置筛选</Button>
        </div></Card>
        <div className="planning-toolbar"><Button type="primary" disabled={!access.canEdit || !selectedRows.length || !matrix.data} onClick={() => matrix.data && setBatch({ rows: selectedRows, revision: matrix.data.revision })}>批量调整{selectedRows.length ? `（${selectedRows.length}）` : ''}</Button>
          <Typography.Text type="secondary">仅作用于当前页勾选的位置；每次最多 100 个。</Typography.Text><Space wrap><Tag color="blue">手动</Tag><Tag color="gold">POV</Tag><Tag>正文次数</Tag><Tag color="warning">? 歧义</Tag></Space>
        </div>
        {matrix.loading && <Skeleton active paragraph={{ rows: 5 }} />}
        {matrix.error && <Alert type="error" showIcon message={matrix.error} action={<Button onClick={matrix.reload}>重试</Button>} />}
        {matrix.data && <>
          {matrix.data.warnings.map((warning) => <Alert key={warning} type="warning" message={warning} />)}
          <PlanningMatrixTable projectId={projectId} data={matrix.data} selected={selectedKeys} canEdit={access.canEdit} onSelection={(keys) => setSelection({ scope, keys })} onCell={(row, column, selectedCell) => setCell({ row, column, cell: selectedCell, revision: matrix.data!.revision })} onEdit={editRow} onNode={openNode} onCrossing={newCrossing} onPreview={setPreview} />
          <div className="planning-pagination"><Pagination current={matrix.data.page} pageSize={matrix.data.page_size} total={matrix.data.total} showSizeChanger={false} onChange={(page) => { setQuery((old) => ({ ...old, page })); setSelection({ scope: '', keys: [] }) }} showTotal={(total) => `共 ${total} 个规划位置`} />
            {query.column_mode !== 'custom' && <Space wrap><Typography.Text type="secondary">展示列</Typography.Text><Pagination simple current={matrix.data.column_page} pageSize={30} total={matrix.data.column_total} onChange={(column_page) => setQuery((old) => ({ ...old, column_page }))} /></Space>}
          </div>
        </>}
      </> : view === 'weave' ? <>
        <Card size="small"><Space direction="vertical" size={12} style={{ width: '100%' }}><div className="planning-toolbar">
          <Segmented aria-label="支线顺序轴" value={weaveQuery.axis} options={[{ value: 'chapter', label: '已建章顺序' }, { value: 'plan', label: '未建章细纲顺序' }, { value: 'scene', label: '实际场景独立轴' }]} onChange={(axis) => setWeaveQuery((old) => ({ ...old, axis: axis as 'chapter' | 'plan' | 'scene', page: 1 }))} />
          <Typography.Text>间隔提醒阈值</Typography.Text><InputNumber aria-label="间隔提醒阈值" min={1} max={100} precision={0} value={weaveQuery.gap_threshold} onChange={(value) => setWeaveQuery((old) => ({ ...old, gap_threshold: value ?? 5 }))} />
        </div><PlanningSubplotSelect projectId={projectId} label="筛选支线（不选则显示全部）" value={weaveQuery.subplot_ids ?? []} known={weave.data?.tracks.map((track) => track.subplot)} onChange={(subplot_ids) => { if (subplot_ids.length > 30) { message.warning('最多同时筛选 30 条支线'); return } setWeaveQuery((old) => ({ ...old, subplot_ids: subplot_ids.length ? subplot_ids : undefined, subplot_page: 1 })) }} /></Space></Card>
        {weave.loading && <Skeleton active paragraph={{ rows: 5 }} />}
        {weave.error && <Alert type="error" message={weave.error} action={<Button onClick={weave.reload}>重试</Button>} />}
        {weave.data && <><PlanningWeaveTable data={weave.data} canEdit={access.canEdit} onNode={openNode} onCrossing={openCrossing} onCreateCrossing={newCrossing} />
          <div className="planning-pagination"><Pagination current={weave.data.page} pageSize={weave.data.page_size} total={weave.data.total} showSizeChanger={false} onChange={(page) => setWeaveQuery((old) => ({ ...old, page }))} showTotal={(total) => `共 ${total} 个${weave.data!.axis === 'chapter' ? '已建章' : weave.data!.axis === 'scene' ? '实际场景' : '未建章细纲'}`} />
            <Space wrap><Typography.Text type="secondary">支线窗口</Typography.Text><Pagination simple current={weave.data.subplot_page} pageSize={20} total={weave.data.subplot_total} onChange={(subplot_page) => setWeaveQuery((old) => ({ ...old, subplot_page }))} /></Space>
          </div></>}
      </> : <SceneGrid projectId={projectId} />}
    </Space></div>
    {batch && <PlanningBatchModal projectId={projectId} {...batch} onClose={() => setBatch(undefined)} onSaved={saved} />}
    {node && <PlanningNodeModal projectId={projectId} {...node} onClose={() => setNode(undefined)} onSaved={saved} />}
    {crossing && <PlanningCrossingModal projectId={projectId} {...crossing} onClose={() => setCrossing(undefined)} onSaved={saved} />}
    {cell && <PlanningCellDrawer projectId={projectId} {...cell} onClose={() => setCell(undefined)} onSaved={saved} onNode={openNode} onCrossing={openCrossing} onCreateCrossing={newCrossing} />}
    {preview && <PlanningPreviewDrawer projectId={projectId} row={preview} onClose={() => setPreview(undefined)} />}
    {catalog && <PlanningCatalog projectId={projectId} kind={catalog} onClose={() => setCatalog(undefined)} onSaved={saved} />}
    {columnsOpen && <PlanningColumnPicker projectId={projectId} value={query.column_refs ?? []} onClose={() => setColumnsOpen(false)} onApply={applyColumns} />}
    {detachedOpen && <PlanningDetachedDrawer projectId={projectId} onClose={() => setDetachedOpen(false)} onSaved={saved} onEdit={editRow} onNode={openNode} onCrossings={(row) => setCrossingsList({ row })} onMerge={setMerge} />}
    {crossingsList && <PlanningCrossingList projectId={projectId} row={crossingsList.row} onClose={() => setCrossingsList(undefined)} onOpen={openCrossing} />}
    {merge && <PlanningMergeModal projectId={projectId} conflict={merge} onClose={() => setMerge(undefined)} onSaved={saved} />}
  </StudioPage>
}
