import { Alert, Button, Checkbox, Input, InputNumber, List, Modal, Pagination, Radio, Select, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { asCareerKind } from '../../api/careers'
import { errorMessage } from '../../api/client'
import { baselineStatusLabel, progressionApi } from '../../api/progressions'
import type { Career, ProgressionJsonValue, ProgressionPositionInput, SettingProgression, SettingTimeline } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import ChapterPositionInput from '../progressions/ChapterPositionInput'
import { describeAnchor, toPositionInput, type ChapterReference } from '../progressions/position'
import { buildCareerBaselineChanges, buildCareerEventValue, isStaleCareerRevision, type CareerBaseline } from './careerAssignmentForm'
import { careerPreviewRequest, careerPreviewResponse, currentCareerPreview, initialCareerPreviewPosition, type CareerPreviewResult } from './careerPreview'

interface Props {
  projectId: number
  timeline: SettingTimeline
  career: Career
  chapters: readonly ChapterReference[]
  onChanged: () => void
}

type EventField = 'presence' | 'stage_id' | 'note'
interface EventDraft {
  event: SettingProgression | null
  expectedRevision: number
  field: EventField
  exists: boolean
  stageId: number | null
  note: string
  position: ProgressionPositionInput
  sortOrder: number
  historyNote: string
}

const blankPosition = (): ProgressionPositionInput => ({ anchor_kind: 'chapter', chapter_id: null, chapter_no: null, boundary: 'start' })
const sourceLabels: Record<SettingProgression['source'], string> = { manual: '手工记录', analysis: '章节分析', legacy_import: '旧资料导入' }
const protectionLabels: Record<SettingProgression['protection'], string> = { automatic: '自动', confirmed: '已确认', manual: '人工维护' }
const isRecord = (value: ProgressionJsonValue | undefined): value is { [key: string]: ProgressionJsonValue } =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

function stageName(career: Career, value: ProgressionJsonValue | undefined): string {
  const stage = career.stages.find((item) => item.id === value)
  return stage ? `${stage.position}·${stage.name}` : '阶段已不可用'
}

function eventDescription(event: SettingProgression, career: Career): string {
  if (event.field_key === 'stage_id') return `阶段变为 ${stageName(career, event.value)}`
  if (event.field_key === 'note') return `状态备注：${typeof event.value === 'string' && event.value ? event.value : '（空）'}`
  if (event.field_key === 'presence' && isRecord(event.value)) {
    if (event.value.exists === false) return '卸下职业'
    const state = event.value.state
    if (event.value.exists === true && isRecord(state)) return `挂接职业 · ${stageName(career, state.stage_id)}${typeof state.note === 'string' && state.note ? ` · ${state.note}` : ''}`
  }
  return '无法识别的职业变化'
}

function eventDraft(event: SettingProgression | null, career: Career, expectedRevision: number): EventDraft {
  const field: EventField = event?.field_key === 'stage_id' || event?.field_key === 'note' ? event.field_key : 'presence'
  const value = event?.value
  const object = isRecord(value) ? value : null
  const state = object && isRecord(object.state) ? object.state : null
  return {
    event, expectedRevision, field, exists: object?.exists !== false,
    stageId: typeof value === 'number' ? value : typeof state?.stage_id === 'number' ? state.stage_id : career.stages[0]?.id ?? null,
    note: typeof value === 'string' ? value : typeof state?.note === 'string' ? state.note : '',
    position: event ? { anchor_kind: event.anchor_kind, chapter_id: event.chapter_id, chapter_no: event.chapter_no, boundary: event.boundary } : blankPosition(),
    sortOrder: event?.sort_order ?? 0,
    historyNote: event?.note ?? '',
  }
}

function CareerPreview({ projectId, timeline, career, chapters, onChanged }: Props) {
  const { canRead, canReadNow } = useProjectAccess(projectId)
  const identity = useAuthStore((s) => `${s.status}:${s.user?.id ?? ''}:${authorizationKey(s.user)}`)
  const [position, setPosition] = useState<ProgressionPositionInput>(() => initialCareerPreviewPosition(chapters))
  const [result, setResult] = useState<CareerPreviewResult | null>(null)
  const request = useMemo(
    () => careerPreviewRequest(identity, projectId, timeline.id, timeline.revision, position, chapters),
    [identity, projectId, timeline.id, timeline.revision, position, chapters],
  )
  const displayedPosition = position.anchor_kind === 'chapter' && !chapters.some((item) => item.id === position.chapter_id)
    ? { ...position, chapter_id: null } : position
  const current = currentCareerPreview(request, result)
  useEffect(() => {
    if (!canRead || !canReadNow() || request === null) return
    let active = true
    const controller = new AbortController()
    progressionApi.effectiveAtPosition(projectId, timeline.id, request.position, controller.signal).then((data) => {
      if (!active || !canReadNow()) return
      setResult(careerPreviewResponse(request, data))
    }, (error: unknown) => {
      if (active && !controller.signal.aborted && canReadNow()) setResult({ key: request.key, data: null, error: errorMessage(error) })
    })
    return () => { active = false; controller.abort() }
  }, [projectId, timeline.id, request, canRead, canReadNow])
  if (!canRead) return null
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      <Typography.Text strong>按章节查看职业</Typography.Text>
      <ChapterPositionInput chapters={chapters} value={displayedPosition} onChange={setPosition} purpose="preview" />
      {request === null && <Typography.Text type="secondary">请选择已有章节或输入计划章节号。</Typography.Text>}
      {request !== null && !current && <Spin size="small" aria-label="正在读取职业设定" />}
      {current?.error && <Alert type="warning" showIcon message={current.error} action={<Button size="small" onClick={onChanged}>刷新</Button>} />}
      {current?.data && <>
        {current.data.orphaned_event_ids.length > 0 && <Alert type="warning" showIcon message={`${current.data.orphaned_event_ids.length} 条变化的关联章节已删除，未计入结果。`} />}
        <Typography.Text>{current.data.fields.exists === true ? `${career.name} · ${stageName(career, current.data.fields.stage_id)}${typeof current.data.fields.note === 'string' && current.data.fields.note ? ` · ${current.data.fields.note}` : ''}` : '此时尚未挂接或已卸下该职业'}</Typography.Text>
      </>}
    </Space>
  )
}

/** 职业主体按角色与职业保留；当前已卸下的主体也可查看和维护。 */
export default function CareerProgressionPanel({ projectId, timeline, career, chapters, onChanged }: Props) {
  const { canRead, canEdit, canReadNow, canEditNow } = useProjectAccess(projectId)
  const [offset, setOffset] = useState(0)
  const [event, setEvent] = useState<EventDraft | null>(null)
  const [baselineDraft, setBaselineDraft] = useState<{ exists: boolean; stageId: number | null; note: string; confirm: boolean; expectedRevision: number; original: CareerBaseline } | null>(null)
  const [baselineOpen, setBaselineOpen] = useState(false)
  const [deleting, setDeleting] = useState<{ event: SettingProgression; expectedRevision: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const activeRef = useRef(true)
  useEffect(() => { activeRef.current = true; return () => { activeRef.current = false } }, [])
  const loader = useCallback(async () => {
    const [baseline, list] = await Promise.all([
      progressionApi.getBaseline(projectId, timeline.id),
      progressionApi.listProgressions(projectId, timeline.id, { offset, limit: 20 }),
    ])
    if (!canReadNow()) throw new Error('当前没有读取权限')
    return { baseline, list }
  }, [projectId, timeline.id, offset, canReadNow])
  const { data, loading, error: loadError, reload } = useLoader(loader, { projectId, resources: ['careers', 'characters'] })
  const revision = data && data.baseline.timeline_revision === data.list.timeline_revision ? data.list.timeline_revision : null
  const refresh = () => { reload(); onChanged() }
  const close = () => { if (!busyRef.current) { setEvent(null); setBaselineOpen(false); setDeleting(null); setError(null); setConflict(false) } }
  const fail = (err: unknown) => {
    setError(errorMessage(err))
    if (isStaleCareerRevision(err)) setConflict(true)
  }
  const run = async (work: () => Promise<unknown>, savedBaseline = false) => {
    if (busyRef.current || !canEditNow() || revision === null) return
    busyRef.current = true; setBusy(true); setError(null)
    try {
      await work()
      if (!activeRef.current || !canEditNow()) return
      setEvent(null); setBaselineOpen(false); if (savedBaseline) setBaselineDraft(null)
      setDeleting(null); setConflict(false); refresh()
    } catch (err) {
      if (activeRef.current && canEditNow()) fail(err)
    } finally {
      busyRef.current = false
      if (activeRef.current) setBusy(false)
    }
  }
  const openBaseline = () => {
    if (!data || revision === null) return
    const base = data.baseline.baseline
    setError(null); setConflict(false)
    if (baselineDraft) { setBaselineOpen(true); return }
    const original: CareerBaseline = { exists: base.exists === true, stage_id: typeof base.stage_id === 'number' ? base.stage_id : null, note: typeof base.note === 'string' ? base.note : '' }
    setBaselineDraft({ exists: original.exists, stageId: original.stage_id ?? career.stages[0]?.id ?? null, note: original.note, confirm: data.baseline.baseline_status === 'legacy_unverified', expectedRevision: revision, original })
    setBaselineOpen(true)
  }
  const saveBaseline = () => {
    if (!baselineDraft || revision === null) return
    if (baselineDraft.exists && baselineDraft.stageId === null) { setError('请选择初始阶段'); return }
    const changes = buildCareerBaselineChanges(baselineDraft.original, baselineDraft.exists, baselineDraft.stageId, baselineDraft.note, asCareerKind(career.kind))
    if (Object.keys(changes).length === 0 && !baselineDraft.confirm) { setError('初始状态没有修改'); return }
    void run(() => progressionApi.updateBaseline(projectId, timeline.id, { expected_revision: baselineDraft.expectedRevision, changes, confirm: baselineDraft.confirm }), true)
  }
  const saveEvent = () => {
    if (!event || revision === null) return
    const position = toPositionInput(event.position)
    if (!position) { setError('请选择有效的生效章节'); return }
    const value = buildCareerEventValue(event.field, event.exists, event.stageId, event.note, asCareerKind(career.kind))
    if (value === null) { setError('请选择阶段'); return }
    const body = { ...position, value, sort_order: event.sortOrder, note: event.historyNote.trim(), expected_revision: event.expectedRevision }
    void run(() => event.event
      ? progressionApi.updateProgression(projectId, timeline.id, event.event.id, body)
      : progressionApi.createProgression(projectId, timeline.id, { ...body, field_key: event.field, custom_field_id: null, operation: 'replace' }))
  }
  if (!canRead) return null
  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    {loadError && <Alert type="error" showIcon message={loadError} action={<Button size="small" onClick={reload}>重试</Button>} />}
    {data && <>
      <Space wrap><Tag color={data.baseline.baseline_status === 'legacy_unverified' ? 'orange' : 'default'}>{baselineStatusLabel(data.baseline.baseline_status)}</Tag>
        <Typography.Text>初始：{data.baseline.baseline.exists === true ? `${stageName(career, data.baseline.baseline.stage_id)}${data.baseline.baseline.note ? ` · ${data.baseline.baseline.note}` : ''}` : '未挂接'}</Typography.Text>
        <Button size="small" disabled={!canEdit || revision === null} onClick={openBaseline}>{baselineDraft ? '继续编辑初始草稿' : '校准初始状态'}</Button>
        {baselineDraft && <Tag color="blue">有未保存的初始草稿</Tag>}
      </Space>
      {data.baseline.baseline_status === 'legacy_unverified' && <Alert type="warning" showIcon message="旧资料尚未校准，已有变化之前的职业状态可能不完整。" />}
      {revision === null && <Alert type="warning" showIcon message="时间线正在更新，请刷新后再编辑。" action={<Button size="small" onClick={reload}>刷新</Button>} />}
      <CareerPreview projectId={projectId} timeline={{ ...timeline, revision: data.baseline.timeline_revision }} career={career} chapters={chapters} onChanged={refresh} />
      <Space wrap><Typography.Text strong>剧情变化</Typography.Text><Button size="small" disabled={!canEdit || revision === null || loading} onClick={() => { if (revision !== null) { setError(null); setConflict(false); setEvent(eventDraft(null, career, revision)) } }}>新增变化</Button></Space>
      {error && !event && !baselineOpen && !deleting && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={reload}>刷新</Button>} />}
      <List loading={loading} size="small" dataSource={data.list.items} locale={{ emptyText: '尚无剧情变化' }} renderItem={(item) => <List.Item actions={canEdit ? [
        <Button key="edit" type="link" size="small" disabled={revision === null || busy || loading} onClick={() => { if (revision !== null) { setError(null); setConflict(false); setEvent(eventDraft(item, career, revision)) } }}>编辑</Button>,
        <Button key="delete" type="link" size="small" danger disabled={revision === null || busy || loading} onClick={() => { if (revision !== null) { setError(null); setConflict(false); setDeleting({ event: item, expectedRevision: revision }) } }}>删除</Button>,
      ] : []}>
        <Space direction="vertical" size={2}><Space wrap><Typography.Text>{describeAnchor(item, chapters)} · {item.boundary === 'start' ? '章初' : '章末'}</Typography.Text><Tag>{sourceLabels[item.source]}</Tag><Tag>{protectionLabels[item.protection]}</Tag>{item.orphaned && <Tag color="orange">章节已删除，不生效</Tag>}</Space><Typography.Text>{eventDescription(item, career)}</Typography.Text>{item.note && <Typography.Text type="secondary">{item.note}</Typography.Text>}</Space>
      </List.Item>} />
      {data.list.total > 20 && <Pagination size="small" current={Math.floor(offset / 20) + 1} pageSize={20} total={data.list.total} onChange={(page) => setOffset((page - 1) * 20)} showSizeChanger={false} />}
    </>}
    <Modal title="删除这条职业变化？" open={deleting !== null} onCancel={close} onOk={() => { if (deleting) void run(() => progressionApi.removeProgression(projectId, timeline.id, deleting.event.id, deleting.expectedRevision)) }} confirmLoading={busy} okButtonProps={{ danger: true, disabled: !canEdit || conflict || revision === null }} okText="删除" cancelText="取消" destroyOnHidden>
      <Typography.Text>删除后会重新计算之后各章的职业状态。</Typography.Text>
      {error && <Alert type="error" showIcon message={error} description={conflict ? '时间线已变化，请关闭后刷新。' : undefined} />}
    </Modal>
    <Modal title="校准初始职业" open={baselineOpen} onCancel={close} onOk={saveBaseline} confirmLoading={busy} okButtonProps={{ disabled: !canEdit || conflict || revision === null }} okText="保存初始状态" cancelText="关闭并保留草稿" destroyOnHidden>
      {baselineDraft && <Space direction="vertical" style={{ width: '100%' }}>
        <Typography.Text type="secondary">初始状态从故事开始生效，之后的剧情变化仍会依次应用。</Typography.Text>
        {error && <Alert type="error" showIcon message={error} description={conflict ? '时间线已变化，草稿仍保留；请复制内容、放弃此草稿并重新校准。' : undefined} />}
        <Radio.Group aria-label="初始是否挂接" disabled={!canEdit || busy} value={baselineDraft.exists} onChange={(e) => setBaselineDraft({ ...baselineDraft, exists: e.target.value as boolean })} options={[{ value: true, label: '初始已挂接' }, { value: false, label: '初始未挂接' }]} />
        {baselineDraft.exists && <><Select aria-label="初始阶段" value={baselineDraft.stageId ?? undefined} disabled={!canEdit || busy} options={career.stages.map((s) => ({ value: s.id, label: `${s.position}·${s.name}` }))} onChange={(stageId: number) => setBaselineDraft({ ...baselineDraft, stageId })} style={{ width: '100%' }} /><Input aria-label="初始备注" maxLength={200} disabled={!canEdit || busy} value={baselineDraft.note} onChange={(e) => setBaselineDraft({ ...baselineDraft, note: e.target.value })} placeholder="初始状态备注" /></>}
        {data?.baseline.baseline_status === 'legacy_unverified' && <Checkbox checked={baselineDraft.confirm} disabled={!canEdit || busy} onChange={(e) => setBaselineDraft({ ...baselineDraft, confirm: e.target.checked })}>确认已核对旧资料的初始状态</Checkbox>}
        <Button size="small" disabled={busy} onClick={() => { setBaselineDraft(null); setBaselineOpen(false); setError(null); setConflict(false) }}>放弃初始草稿</Button>
      </Space>}
    </Modal>
    <Modal title={event?.event ? '编辑职业变化' : '新增职业变化'} open={event !== null} onCancel={close} onOk={saveEvent} confirmLoading={busy} okButtonProps={{ disabled: !canEdit || conflict || revision === null }} okText="保存变化" cancelText="取消" destroyOnHidden>
      {event && <Space direction="vertical" style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon message={error} description={conflict ? '时间线已变化，草稿仍保留；请复制内容后关闭并刷新。' : undefined} />}
        <Typography.Text>变化类型</Typography.Text>
        <Select aria-label="职业变化类型" value={event.field} disabled={!canEdit || busy || !!event.event} options={[{ value: 'presence', label: '挂接或卸下' }, { value: 'stage_id', label: '阶段变化' }, { value: 'note', label: '状态备注变化' }]} onChange={(field: EventField) => setEvent({ ...event, field })} style={{ width: '100%' }} />
        {event.field === 'presence' && <Radio.Group aria-label="职业挂接状态" disabled={!canEdit || busy} value={event.exists} onChange={(e) => setEvent({ ...event, exists: e.target.value as boolean })} options={[{ value: true, label: '挂接' }, { value: false, label: '卸下' }]} />}
        {(event.field === 'stage_id' || (event.field === 'presence' && event.exists)) && <Select aria-label="变化后的阶段" placeholder="选择阶段" value={event.stageId ?? undefined} disabled={!canEdit || busy} options={career.stages.map((s) => ({ value: s.id, label: `${s.position}·${s.name}` }))} onChange={(stageId: number) => setEvent({ ...event, stageId })} style={{ width: '100%' }} />}
        {(event.field === 'note' || (event.field === 'presence' && event.exists)) && <Input aria-label="变化后的状态备注" maxLength={200} disabled={!canEdit || busy} value={event.note} onChange={(e) => setEvent({ ...event, note: e.target.value })} placeholder="状态备注（可留空）" />}
        <Typography.Text>生效位置</Typography.Text><ChapterPositionInput chapters={chapters} value={event.position} onChange={(position) => setEvent({ ...event, position })} disabled={!canEdit || busy} />
        <Space wrap><Typography.Text>同一时点的顺序</Typography.Text><InputNumber min={0} precision={0} value={event.sortOrder} onChange={(value) => setEvent({ ...event, sortOrder: value ?? 0 })} disabled={!canEdit || busy} /></Space>
        <Input aria-label="变化说明" maxLength={200} value={event.historyNote} disabled={!canEdit || busy} onChange={(e) => setEvent({ ...event, historyNote: e.target.value })} placeholder="变化说明（可选）" />
      </Space>}
    </Modal>
  </Space>
}
