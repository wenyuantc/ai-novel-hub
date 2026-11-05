import { PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Checkbox, Empty, Input, List, Modal, Radio, Select, Space, Spin, Table, Tag, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  assignmentApi, CAREER_HISTORY_FIELD_LABELS, CAREER_KIND_LABELS, CAREER_ROLE_LABELS,
  CAREER_SOURCE_LABELS, careerApi, formatCareerChange, stageLabel,
} from '../../api/careers'
import { chapterApi } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import { progressionApi } from '../../api/progressions'
import type { Assignment, AssignmentInput, AssignmentUpdate, Career, CareerHistory, Character, ProgressionPositionInput, SettingTimeline } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { formatTime } from '../../utils/time'
import ChapterPositionInput from '../progressions/ChapterPositionInput'
import type { ChapterReference } from '../progressions/position'
import CareerProgressionPanel from './CareerProgressionPanel'
import { buildCareerCreate, buildCareerRemoval, buildCareerUpdate, isCareerHistoryTimelineRequired, isStaleCareerRevision, type CareerAssignmentDraft, type CareerBaseline } from './careerAssignmentForm'

interface Props {
  projectId: number
  character: Character
  onChanged?: () => void
}

const blankPosition = (): ProgressionPositionInput => ({ anchor_kind: 'chapter', chapter_id: null, chapter_no: null, boundary: 'start' })
const draft = (stageId: number | null = null, changeStage = false): CareerAssignmentDraft => ({
  edit_mode: 'progression', stage_id: stageId, note: '', change_stage: changeStage, change_note: false, position: blankPosition(), history_note: '',
})

function HistoryList({ items }: { items: CareerHistory[] }) {
  if (items.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史" />
  return <List size="small" dataSource={items} renderItem={(h) => <List.Item>
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      <Space wrap size={4}>
        <Typography.Text type="secondary">{formatTime(h.created_at)}</Typography.Text><Typography.Text strong>{h.career_name}</Typography.Text>
        <Tag>{CAREER_SOURCE_LABELS[h.source] ?? h.source}</Tag>{h.chapter_no !== null && <Tag color="blue">第 {h.chapter_no} 章</Tag>}{h.note && <Typography.Text>{h.note}</Typography.Text>}
      </Space>
      {Object.entries(h.changes).map(([field, pair]) => {
        const [oldV, newV] = Array.isArray(pair) ? pair : [undefined, pair]
        return <Typography.Text key={field} style={{ fontSize: 13 }}>{field === 'removed' ? `卸下职业：${formatCareerChange(field, oldV)}` : `${CAREER_HISTORY_FIELD_LABELS[field] ?? field}：${formatCareerChange(field, oldV)} → ${formatCareerChange(field, newV)}`}</Typography.Text>
      })}
    </Space>
  </List.Item>} />
}

interface EditProps {
  projectId: number
  characterId: number
  assignment: Assignment | null
  quickStage?: number
  careers: Career[]
  rows: Assignment[]
  timelines: SettingTimeline[]
  chapters: readonly ChapterReference[]
  visible: boolean
  onClose: () => void
  onChanged: () => void
  onOpenTimeline: (careerId: number) => void
}

function CareerEditModal({ projectId, characterId, assignment, quickStage, careers, rows, timelines, chapters, visible, onClose, onChanged, onOpenTimeline }: EditProps) {
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(projectId)
  const [careerId, setCareerId] = useState<number | null>(assignment?.career_id ?? null)
  const [timelineSelection, setTimelineSelection] = useState(() => ({
    careerId: assignment?.career_id ?? null,
    timeline: timelines.find((item) => item.career_id === assignment?.career_id),
  }))
  const career = careers.find((item) => item.id === careerId)
  const timeline = timelineSelection.careerId === careerId ? timelineSelection.timeline : undefined
  const timelineId = timeline?.id ?? null
  const [mode, setMode] = useState<'baseline' | 'progression'>('progression')
  const [progressDraft, setProgressDraft] = useState(() => draft(quickStage ?? null, quickStage !== undefined))
  const [baseDraft, setBaseDraft] = useState<CareerAssignmentDraft | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [timelineRequired, setTimelineRequired] = useState(false)
  const [conflict, setConflict] = useState(false)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const active = useRef(true)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const baselineLoader = useCallback(async () => {
    if (timelineId === null) return { timelineId: null, baseline: null }
    const baseline = await progressionApi.getBaseline(projectId, timelineId)
    if (!canReadNow()) throw new Error('当前没有读取权限')
    return { timelineId, baseline }
  }, [projectId, timelineId, canReadNow])
  const { data: loaded, loading, error: loadError, reload } = useLoader(baselineLoader)
  const current = loaded?.timelineId === (timeline?.id ?? null) ? loaded.baseline : null
  const pending = loading || (!!timeline && !current)
  const baseline: CareerBaseline = {
    exists: current?.baseline.exists === true,
    stage_id: typeof current?.baseline.stage_id === 'number' ? current.baseline.stage_id : null,
    note: typeof current?.baseline.note === 'string' ? current.baseline.note : '',
  }
  const selectedDraft = mode === 'baseline' ? baseDraft : progressDraft
  const updateDraft = (patch: Partial<CareerAssignmentDraft>) => {
    if (mode === 'baseline') setBaseDraft((value) => value ? { ...value, ...patch } : value)
    else setProgressDraft((value) => ({ ...value, ...patch }))
  }
  const selectMode = (next: 'baseline' | 'progression') => {
    if (next === 'baseline' && !baseDraft) setBaseDraft({ ...draft(baseline.stage_id ?? career?.stages[0]?.id ?? null), edit_mode: 'baseline', note: baseline.note })
    setMode(next)
  }
  const save = async () => {
    if (busyRef.current || !canEditNow() || !career || !selectedDraft || pending || conflict) return
    if (assignment && !current) { setError('缺少职业时间线，请刷新后重试'); return }
    if (!assignment && rows.some((item) => item.career_id === career.id)) { setError('该角色当前已挂接此职业，请编辑现有记录'); return }
    let body: AssignmentUpdate | AssignmentInput
    if (assignment) {
      const result = buildCareerUpdate(selectedDraft, baseline, current?.timeline_revision ?? null)
      if (!result.ok) { setError(result.error); return }
      body = result.value
    } else {
      const result = buildCareerCreate(career.id, selectedDraft, current?.timeline_revision ?? null)
      if (!result.ok) { setError(result.error); return }
      body = result.value
    }
    busyRef.current = true; setBusy(true); setError(null); setTimelineRequired(false)
    try {
      if (assignment) await assignmentApi.update(projectId, characterId, assignment.id, body)
      else await assignmentApi.create(projectId, characterId, body as Parameters<typeof assignmentApi.create>[2])
      if (!active.current || !canEditNow()) return
      onChanged(); onClose()
    } catch (err) {
      if (!active.current || !canEditNow()) return
      setError(errorMessage(err))
      if (isStaleCareerRevision(err)) setConflict(true)
      if (isCareerHistoryTimelineRequired(err)) setTimelineRequired(true)
    } finally {
      busyRef.current = false
      if (active.current) setBusy(false)
    }
  }
  const close = () => { if (!busyRef.current) onClose() }
  return <Modal title={assignment ? `编辑：${assignment.career_name}` : '挂接职业'} open={visible} onCancel={close} onOk={() => void save()} confirmLoading={busy}
    okButtonProps={{ disabled: !canEdit || !career || pending || conflict || !selectedDraft }} okText="保存" cancelText="取消" destroyOnHidden>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} description={conflict ? '时间线已变化或操作与历史冲突，草稿仍保留；请复制内容后关闭并刷新。' : undefined} />}
      {timelineRequired && career && <Alert type="info" showIcon message="请在职业时间线中补录早期挂接或校准初始状态。" description="挂接草稿会暂时保留，可返回继续查看。" action={<Button size="small" onClick={() => onOpenTimeline(career.id)}>前往时间线</Button>} />}
      {loadError && <Alert type="error" showIcon message={loadError} action={<Button size="small" onClick={reload}>重试</Button>} />}
      <Typography.Text>职业</Typography.Text>
      <Select aria-label="职业" disabled={!!assignment || !canEdit || busy} showSearch optionFilterProp="label" placeholder="选择职业" value={careerId ?? undefined}
        onChange={(value: number) => { setCareerId(value); setTimelineSelection({ careerId: value, timeline: timelines.find((item) => item.career_id === value) }); setProgressDraft(draft(careers.find((item) => item.id === value)?.stages[0]?.id ?? null)); setBaseDraft(null); setMode('progression'); setError(null); setTimelineRequired(false) }}
        options={careers.map((item) => ({ value: item.id, label: `${item.name}（${CAREER_KIND_LABELS[item.kind === 'sub' ? 'sub' : 'main']}）`, disabled: rows.some((row) => row.career_id === item.id) }))} style={{ width: '100%' }} />
      {career && <>
        {pending ? <Spin size="small" aria-label="正在读取职业初始状态" /> : <>
          {assignment && !current && <Alert type="warning" showIcon message="未找到职业时间线，请刷新后重试。" />}
          <Typography.Text>编辑意图</Typography.Text>
          <Radio.Group aria-label="职业编辑意图" value={mode} disabled={!canEdit || busy} onChange={(e) => selectMode(e.target.value as 'baseline' | 'progression')} options={[{ value: 'progression', label: '记录剧情变化' }, { value: 'baseline', label: '修正初始设定' }]} />
          <Typography.Text type="secondary">{mode === 'baseline' ? '修正故事开始时的职业状态，后续变化仍会生效。' : '只在指定章节记录变化，不改写更早的设定。'}</Typography.Text>
          {selectedDraft && <>
            {assignment && mode === 'progression' && <Checkbox checked={selectedDraft.change_stage} disabled={!canEdit || busy} onChange={(e) => updateDraft({ change_stage: e.target.checked })}>记录阶段变化</Checkbox>}
            {(!assignment || mode === 'baseline' || selectedDraft.change_stage) && <Select aria-label="职业阶段" placeholder="选择阶段" value={selectedDraft.stage_id ?? undefined} disabled={!canEdit || busy} options={career.stages.map((s) => ({ value: s.id, label: `${s.position}·${s.name}` }))} onChange={(stageId: number) => updateDraft({ stage_id: stageId })} style={{ width: '100%' }} />}
            {assignment && mode === 'progression' && <Checkbox checked={selectedDraft.change_note} disabled={!canEdit || busy} onChange={(e) => updateDraft({ change_note: e.target.checked })}>记录状态备注变化</Checkbox>}
            {(!assignment || mode === 'baseline' || selectedDraft.change_note) && <Input aria-label="职业状态备注" maxLength={200} placeholder="状态备注（可留空）" value={selectedDraft.note} disabled={!canEdit || busy} onChange={(e) => updateDraft({ note: e.target.value })} />}
            {mode === 'progression' && <><Typography.Text>生效位置</Typography.Text><ChapterPositionInput chapters={chapters} value={selectedDraft.position} onChange={(position) => updateDraft({ position })} disabled={!canEdit || busy} /></>}
            <Input aria-label="职业变化说明" maxLength={200} placeholder="变化说明（可选）" value={selectedDraft.history_note} disabled={!canEdit || busy} onChange={(e) => updateDraft({ history_note: e.target.value })} />
          </>}
        </>}
      </>}
    </Space>
  </Modal>
}

interface RemoveProps {
  projectId: number
  characterId: number
  assignment: Assignment
  timeline: SettingTimeline | undefined
  chapters: readonly ChapterReference[]
  onClose: () => void
  onChanged: () => void
}
export function CareerRemoveModal({ projectId, characterId, assignment, timeline, chapters, onClose, onChanged }: RemoveProps) {
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(projectId)
  const [mode, setMode] = useState<'progression' | 'baseline' | 'delete'>('progression')
  const [position, setPosition] = useState(blankPosition)
  const [confirmed, setConfirmed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const active = useRef(true)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const timelineId = timeline?.id ?? null
  const loader = useCallback(async () => {
    if (timelineId === null) throw new Error('未找到职业时间线，请刷新后重试')
    const baseline = await progressionApi.getBaseline(projectId, timelineId)
    if (!canReadNow()) throw new Error('当前没有读取权限')
    return baseline
  }, [projectId, timelineId, canReadNow])
  const { data, loading, error: loadError, reload } = useLoader(loader)
  const save = async () => {
    if (!canEditNow() || busyRef.current || conflict) return
    if (mode === 'delete' && !confirmed) { setError('请确认删除整份职业资料及其剧情历史'); return }
    const result = buildCareerRemoval(mode, position, data?.timeline_revision ?? null)
    if (!result.ok) { setError(result.error); return }
    busyRef.current = true; setBusy(true); setError(null)
    try {
      await assignmentApi.remove(projectId, characterId, assignment.id, result.value)
      if (!active.current || !canEditNow()) return
      onChanged(); onClose()
    } catch (err) {
      if (!active.current || !canEditNow()) return
      setError(errorMessage(err))
      if (isStaleCareerRevision(err)) setConflict(true)
    } finally {
      busyRef.current = false
      if (active.current) setBusy(false)
    }
  }
  return <Modal title={`处理职业：${assignment.career_name}`} open onCancel={() => { if (!busyRef.current) onClose() }} onOk={() => void save()} confirmLoading={busy} okButtonProps={{ disabled: !canEdit || loading || !!loadError || conflict || !data || (mode === 'delete' && !confirmed), danger: mode === 'delete' }} okText={mode === 'delete' ? '删除整份职业资料' : '确认'} cancelText="取消" destroyOnHidden>
    <Space direction="vertical" style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} description={conflict ? '当前记录已变化或与历史冲突，选择仍保留；请关闭并刷新后重试。' : undefined} />}
      {loadError && <Alert type="error" showIcon message={loadError} action={<Button size="small" onClick={reload}>重试</Button>} />}
      <Radio.Group aria-label="职业卸下意图" value={mode} disabled={!canEdit || busy} onChange={(e) => { setMode(e.target.value as typeof mode); setConfirmed(false) }}>
        <Space direction="vertical"><Radio value="progression">在剧情中卸下</Radio><Radio value="baseline">修正初始状态为未挂接</Radio><Radio value="delete">删除误建的整份职业资料</Radio></Space>
      </Radio.Group>
      {mode === 'progression' && <ChapterPositionInput chapters={chapters} value={position} onChange={setPosition} disabled={!canEdit || busy} />}
      {mode === 'baseline' && <Alert type="warning" showIcon message="初始状态将改为未挂接，已有剧情变化仍会保留并参与计算。" />}
      {mode === 'delete' && <><Alert type="warning" showIcon message="将删除该职业的整份时间线及全部剧情变化，无法恢复。" /><Checkbox checked={confirmed} disabled={!canEdit || busy} onChange={(e) => setConfirmed(e.target.checked)}>我确认删除整份职业资料及其剧情历史</Checkbox></>}
    </Space>
  </Modal>
}

function CharacterCareersContent({ projectId, character, onChanged }: Props) {
  const { canRead, canEdit, canReadNow } = useProjectAccess(projectId)
  const { message } = App.useApp()
  const loader = useCallback(() => assignmentApi.list(projectId, character.id), [projectId, character.id])
  const { data: rows = [], loading, error: rowsError, reload } = useLoader(loader, { projectId, resources: ['careers', 'characters'] })
  const historyLoader = useCallback(() => assignmentApi.history(projectId, character.id), [projectId, character.id])
  const { data: history = [], reload: reloadHistory } = useLoader(historyLoader, { projectId, resources: ['careers', 'characters'] })
  const careerLoader = useCallback(() => careerApi.list(projectId), [projectId])
  const { data: careers = [] } = useLoader(careerLoader, { projectId, resources: ['careers'] })
  const chapterLoader = useCallback(() => chapterApi.list(projectId), [projectId])
  const { data: chapters = [] } = useLoader(chapterLoader, { projectId, resources: ['chapters'] })
  const timelineLoader = useCallback(async () => {
    const result = await progressionApi.listTimelines(projectId, { subject_kind: 'career', character_id: character.id })
    if (!canReadNow()) throw new Error('当前没有读取权限')
    return result.items
  }, [projectId, character.id, canReadNow])
  const { data: timelines = [], loading: timelinesLoading, error: timelinesError, reload: reloadTimelines } = useLoader(timelineLoader, { projectId, resources: ['careers', 'characters'] })
  const [editing, setEditing] = useState<{ assignment: Assignment | null; quickStage?: number } | null>(null)
  const [editOpen, setEditOpen] = useState(false)
  const [removing, setRemoving] = useState<Assignment | null>(null)
  const [selectedTimeline, setSelectedTimeline] = useState<number | null>(null)
  const [showAll, setShowAll] = useState(false)
  const refresh = () => { reload(); reloadHistory(); reloadTimelines(); onChanged?.() }
  const changed = () => { message.success('已保存'); refresh() }
  const openEditor = (next: { assignment: Assignment | null; quickStage?: number }) => {
    if (editing) { message.warning('请先完成或关闭保留的职业草稿'); return }
    setEditing(next); setEditOpen(true)
  }
  const openTimeline = (careerId: number) => {
    const target = timelines.find((item) => item.career_id === careerId)
    if (!target) { message.warning('尚未找到职业时间线，请刷新后重试'); return }
    setSelectedTimeline(target.id); setEditOpen(false)
  }
  const selected = timelines.find((item) => item.id === selectedTimeline)
  const selectedCareer = careers.find((item) => item.id === selected?.career_id)
  if (!canRead) return null
  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Space wrap><Button icon={<PlusOutlined />} onClick={() => openEditor({ assignment: null })} disabled={!canEdit || careers.length === 0 || timelinesLoading || !!timelinesError || !!editing}>挂接职业</Button><Typography.Text type="secondary">最多 1 个主职业、3 个副职业；剧情变化按章节生效。</Typography.Text></Space>
    {editing && !editOpen && <Alert type="info" showIcon message="挂接草稿已保留，可继续查看或取消。" action={<Button size="small" onClick={() => setEditOpen(true)}>继续挂接草稿</Button>} />}
    {rowsError && <Alert type="error" showIcon message={rowsError} action={<Button size="small" onClick={reload}>重试</Button>} />}
    {timelinesError && <Alert type="error" showIcon message={`职业时间线加载失败：${timelinesError}`} action={<Button size="small" onClick={reloadTimelines}>重试</Button>} />}
    <Table<Assignment> size="small" rowKey="id" loading={loading} dataSource={rows} pagination={false} scroll={{ x: 620 }}
      locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={careers.length === 0 ? '项目中还没有职业，可在「职业」页添加' : '当前未挂接任何职业'} /> }}
      columns={[
        { title: '身份', width: 80, render: (_, item) => <Tag color={item.role === 'main' ? 'gold' : 'blue'}>{item.role === 'main' || item.role === 'sub' ? CAREER_ROLE_LABELS[item.role] : item.role}</Tag> },
        { title: '职业', dataIndex: 'career_name' },
        { title: '阶段', width: 180, render: (_, item) => {
          const career = careers.find((c) => c.id === item.career_id)
          return <Select size="small" aria-label={`${item.career_name}阶段`} style={{ width: '100%' }} value={item.stage_id} labelRender={() => stageLabel(item.stage_name, item.stage_position, item.stage_total)}
            options={(career?.stages ?? []).map((s) => ({ value: s.id, label: `${s.position}·${s.name}` }))} onChange={(value: number) => { if (value !== item.stage_id) openEditor({ assignment: item, quickStage: value }) }} disabled={!canEdit || timelinesLoading || !!timelinesError || !!editing} />
        } },
        { title: '状态备注', dataIndex: 'note', render: (value: string) => value || '-' },
        { title: '操作', width: 115, render: (_, item) => <Space size={0}><Button type="link" size="small" onClick={() => openEditor({ assignment: item })} disabled={!canEdit || timelinesLoading || !!timelinesError || !!editing}>编辑</Button><Button type="link" size="small" danger disabled={!canEdit || timelinesLoading || !!timelinesError || !!editing} onClick={() => setRemoving(item)}>卸下</Button></Space> },
      ]} />
    <Typography.Title level={5} style={{ marginBottom: 0 }}>职业时间线</Typography.Title>
    <Typography.Text type="secondary">选择职业查看初始状态、各章变化和章节预览；已卸下的职业也会保留在这里。</Typography.Text>
    <Select aria-label="选择职业时间线" placeholder={timelinesLoading ? '正在加载时间线' : '选择职业'} loading={timelinesLoading} value={selectedTimeline ?? undefined} onChange={(value: number) => setSelectedTimeline(value)} options={timelines.map((item) => {
      const career = careers.find((c) => c.id === item.career_id)
      const current = rows.some((row) => row.career_id === item.career_id)
      return { value: item.id, label: `${career?.name ?? '职业资料不可用'}${current ? '' : '（当前已卸下）'}` }
    })} style={{ width: '100%' }} />
    {!timelinesLoading && timelines.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无职业时间线" />}
    {selected && selectedCareer && <CareerProgressionPanel key={`${projectId}:${character.id}:${selected.id}`} projectId={projectId} timeline={selected} career={selectedCareer} chapters={chapters} onChanged={refresh} />}
    {selected && !selectedCareer && <Alert type="warning" showIcon message="该职业定义不可用，无法编辑时间线。" />}
    <Button type="link" size="small" style={{ paddingInline: 0 }} onClick={() => setShowAll((s) => !s)}>{showAll ? '收起操作历史' : `查看操作历史（${history.length}）`}</Button>{showAll && <HistoryList items={history} />}
    {editing && <CareerEditModal key={`${editing.assignment?.id ?? 'new'}:${editing.quickStage ?? ''}`} projectId={projectId} characterId={character.id} assignment={editing.assignment} quickStage={editing.quickStage} careers={careers} rows={rows} timelines={timelines} chapters={chapters} visible={editOpen} onClose={() => { setEditOpen(false); setEditing(null) }} onChanged={changed} onOpenTimeline={openTimeline} />}
    {removing && <CareerRemoveModal key={removing.id} projectId={projectId} characterId={character.id} assignment={removing} timeline={timelines.find((item) => item.career_id === removing.career_id)} chapters={chapters} onClose={() => setRemoving(null)} onChanged={changed} />}
  </Space>
}

export default function CharacterCareersTab(props: Props) {
  const { canRead } = useProjectAccess(props.projectId)
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  return canRead ? <CharacterCareersContent key={`${identity}:${props.projectId}:${props.character.id}`} {...props} /> : null
}
