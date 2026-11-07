import { Alert, App, Button, Checkbox, Empty, Input, InputNumber, List, Modal, Pagination, Popconfirm, Radio, Select, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { chapterApi } from '../../api/chapters'
import { ApiError, errorMessage } from '../../api/client'
import { progressionApi } from '../../api/progressions'
import { isSymmetric, parseTypeOptionValue, RELATION_STATUS, relationshipTypeApi, typeOptions, typeOptionValue } from '../../api/relationships'
import { characterApi } from '../../api/story'
import type { Character, ChapterBrief, ProgressionPositionInput, RelationshipType, RelationStatus, SettingBaseline, SettingProgression, SettingTimeline } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { invalidateProject } from '../../utils/projectRefresh'
import ChapterPositionInput from '../progressions/ChapterPositionInput'
import { describeAnchor, describeBoundary, toPositionInput } from '../progressions/position'
import { buildRelationEvent, createRelationRequestScope, readRelationSnapshot, relationSubject, type RelationEventDraft, type RelationEventField, type RelationSnapshot } from './relationForm'
import { applyRelationRefresh, refreshRelationData } from './relationRefresh'

interface Props { projectId: number; pair: readonly [number, number] }
interface Data {
  timeline: SettingTimeline
  baseline: SettingBaseline
  items: SettingProgression[]
  total: number
  chapters: ChapterBrief[]
  characters: Character[]
  types: RelationshipType[]
}
const PAGE_SIZE = 20
const FIELD_LABELS: Record<RelationEventField, string> = { presence: '关系建立 / 结束', identity: '关系类型和方向', intimacy: '亲密度', status: '状态', description: '描述' }
const statusOptions = (Object.keys(RELATION_STATUS) as RelationStatus[]).map((value) => ({ value, label: RELATION_STATUS[value].label }))
const blankPosition = (chapters: ChapterBrief[]): ProgressionPositionInput => ({ anchor_kind: chapters.length ? 'chapter' : 'planned', chapter_id: null, chapter_no: null, boundary: 'start' })
function object(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value) }
function valueText(value: unknown): string { return typeof value === 'string' ? value || '（空）' : JSON.stringify(value) }
function snapshotText(snapshot: RelationSnapshot, names: Character[], types: RelationshipType[]): string {
  if (!snapshot.exists) return '关系尚未建立，或已结束'
  const a = names.find((c) => c.id === snapshot.a_id)?.name ?? `角色 ${snapshot.a_id}`
  const b = names.find((c) => c.id === snapshot.b_id)?.name ?? `角色 ${snapshot.b_id}`
  const type = types.find((t) => t.id === snapshot.type_id)
  const relation = type ? (isSymmetric(type) ? `${a} 与 ${b} 是${type.role_a}` : `${a} 是 ${b} 的${type.role_a}`) : `关系类型 ${snapshot.type_id}`
  return `${relation}；亲密度 ${snapshot.intimacy}；状态 ${RELATION_STATUS[snapshot.status].label}；描述：${snapshot.description || '（空）'}`
}
function eventText(event: SettingProgression, pair: readonly [number, number], characters: Character[], types: RelationshipType[]): string {
  try {
    if (event.field_key === 'presence' && object(event.value)) {
      if (event.value.exists === false) return '关系结束'
      if (event.value.exists === true && object(event.value.state)) {
        return `关系建立：${snapshotText(readRelationSnapshot({ exists: true, ...event.value.state }, pair), characters, types)}`
      }
    }
    if (event.field_key === 'identity') {
      const snapshot = readRelationSnapshot({ exists: true, identity: event.value, intimacy: 0, status: 'normal', description: '' }, pair)
      return snapshotText(snapshot, characters, types).split('；')[0]
    }
    if (event.field_key === 'status' && typeof event.value === 'string' && Object.hasOwn(RELATION_STATUS, event.value)) {
      return RELATION_STATUS[event.value as RelationStatus].label
    }
    return valueText(event.value)
  } catch { return '变化内容不完整，请检查数据' }
}
function draftFor(event: SettingProgression | null, pair: readonly [number, number], chapters: ChapterBrief[]): RelationEventDraft {
  const draft: RelationEventDraft = { field: 'presence', presence: false, values: { a_id: pair[0], b_id: pair[1], type_id: 0, intimacy: 0, status: 'normal', description: '' },
    position: blankPosition(chapters), operation: 'replace', sortOrder: 0, note: '' }
  if (!event) return draft
  if (!event.field_key || !Object.hasOwn(FIELD_LABELS, event.field_key) || event.custom_field_id !== null) throw new Error('此变化的字段无法在关系编辑器中安全修改')
  draft.field = event.field_key as RelationEventField
  draft.position = { anchor_kind: event.anchor_kind, ...(event.anchor_kind === 'scene' ? { scene_id: event.scene_id } : {}), chapter_id: event.chapter_id, chapter_no: event.chapter_no, boundary: event.boundary }
  draft.operation = event.operation; draft.sortOrder = event.sort_order; draft.note = event.note
  if (draft.field === 'presence') {
    if (!object(event.value) || typeof event.value.exists !== 'boolean') throw new Error('关系建立变化格式不完整')
    draft.presence = event.value.exists
    if (draft.presence) {
      if (!object(event.value.state)) throw new Error('关系建立变化缺少状态')
      const snapshot = readRelationSnapshot({ exists: true, ...event.value.state }, pair)
      if (!snapshot.exists) throw new Error('关系建立变化缺少状态')
      draft.values = snapshot
    }
  } else if (draft.field === 'identity') {
    const snapshot = readRelationSnapshot({ exists: true, identity: event.value, intimacy: 0, status: 'normal', description: '' }, pair)
    if (!snapshot.exists) throw new Error('关系身份不完整')
    draft.values = snapshot
  } else if (draft.field === 'intimacy' && typeof event.value === 'number') draft.values.intimacy = event.value
  else if (draft.field === 'status' && typeof event.value === 'string' && Object.hasOwn(RELATION_STATUS, event.value)) draft.values.status = event.value as RelationStatus
  else if (draft.field === 'description' && typeof event.value === 'string') draft.values.description = event.value
  else throw new Error('关系变化内容不完整，无法安全编辑')
  return draft
}

/** 读取和维护一对人物的时间线；已结束的关系也能通过人物对打开。 */
export default function RelationProgressionPanel(props: Props) {
  const identity = useAuthStore((s) => `${s.status}:${s.user?.id ?? ''}:${authorizationKey(s.user)}`)
  const key = `${props.projectId}:${Math.min(...props.pair)}:${Math.max(...props.pair)}:${identity}`
  return <RelationProgressionSession key={key} {...props} />
}

function RelationProgressionSession({ projectId, pair }: Props) {
  const { canRead, canReadNow, canEdit, canEditNow } = useProjectAccess(projectId)
  const identity = useAuthStore((s) => `${s.status}:${s.user?.id ?? ''}:${authorizationKey(s.user)}`)
  const { message, modal } = App.useApp()
  const [state, setState] = useState<{ key: string; data: Data } | null>(null)
  const [loadedStamp, setLoadedStamp] = useState('')
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [refresh, setRefresh] = useState(0)
  const [editor, setEditor] = useState<{ event: SettingProgression | null } | null>(null)
  const [previewPosition, setPreviewPosition] = useState<ProgressionPositionInput>({ anchor_kind: 'chapter', chapter_id: null, chapter_no: null, boundary: 'start' })
  const [preview, setPreview] = useState<RelationSnapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const scope = useRef(createRelationRequestScope())
  const activeScope = useRef('')
  const previewEpoch = useRef(0)
  const busyRef = useRef(false)
  const a = Math.min(...pair); const b = Math.max(...pair)
  const key = `${projectId}:${a}:${b}:${identity}`
  const data = canRead && state?.key === key ? state.data : null
  const stamp = `${key}:${page}:${refresh}`
  const loading = canRead && loadedStamp !== stamp
  useLayoutEffect(() => {
    const gate = scope.current
    activeScope.current = key; gate.invalidate(); previewEpoch.current += 1; busyRef.current = false
    return () => { activeScope.current = ''; gate.invalidate(); previewEpoch.current += 1 }
  }, [key])
  useEffect(() => {
    if (!canRead || !canReadNow()) return
    const ticket = scope.current.capture()
    const controller = new AbortController()
    let active = true
    const current = () => active && scope.current.isCurrent(ticket, canReadNow())
    const load = async () => {
      const result = await refreshRelationData(async (): Promise<Data> => {
        const [timeline, chapters, characters, types] = await Promise.all([
          progressionApi.findTimeline(projectId, relationSubject(a, b), controller.signal), chapterApi.list(projectId), characterApi.list(projectId), relationshipTypeApi.list(projectId),
        ])
        if (!current()) throw new Error('当前关系已切换')
        if (!timeline) throw new Error('这两位角色还没有关系历史')
        const [baseline, result] = await Promise.all([
          progressionApi.getBaseline(projectId, timeline.id, controller.signal),
          progressionApi.listProgressions(projectId, timeline.id, { offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE }, controller.signal),
        ])
        if (!current()) throw new Error('当前关系已切换')
        if (baseline.timeline_revision !== result.timeline_revision) throw new Error('设定刚刚发生变化，请刷新后重试')
        return { timeline, baseline, items: result.items, total: result.total, chapters, characters, types }
      })
      if (!current()) return
      setState((previous) => applyRelationRefresh(previous, key, result))
      setError(result.error)
      setLoadedStamp(stamp)
    }
    void load()
    return () => { active = false; controller.abort() }
  }, [projectId, a, b, key, stamp, page, canRead, canReadNow])
  const current = (ticket: number) => scope.current.isCurrent(ticket, canReadNow())
  const writable = () => activeScope.current === key && canEditNow() && !busyRef.current && !loading && !error && !!data
  const reload = () => { previewEpoch.current += 1; setPreview(null); setRefresh((n) => n + 1) }
  useProjectRefresh(projectId, ['relationships', 'characters', 'chapters'], reload)
  let baselineText = '基础设定不完整，请检查数据'
  let baselineValid = false
  if (data) { try { baselineText = snapshotText(readRelationSnapshot(data.baseline.baseline, [a, b]), data.characters, data.types); baselineValid = true } catch { /* 保留错误提示 */ } }
  const changed = () => { invalidateProject(projectId, ['relationships', 'characters']) }
  const readPreview = async () => {
    if (!data || !canReadNow() || !toPositionInput(previewPosition)) { setError('请选择有效的章节位置'); return }
    const ticket = scope.current.capture(); const request = ++previewEpoch.current
    try {
      const result = await progressionApi.effectiveAtPosition(projectId, data.timeline.id, previewPosition)
      if (!current(ticket) || request !== previewEpoch.current) return
      setPreview(readRelationSnapshot(result.fields, [a, b])); setError('')
    } catch (err) { if (current(ticket) && request === previewEpoch.current) setError(errorMessage(err)) }
  }
  const confirmBaseline = () => {
    if (!writable() || !data) return
    modal.confirm({ title: '确认基础关系已校准？', content: '旧资料未保存的历史状态无法自动恢复。确认后，当前基础设定将作为已核对的起点。', okText: '确认校准', cancelText: '取消', onOk: async () => {
      if (!writable()) return
      const ticket = scope.current.capture(); busyRef.current = true; setBusy(true)
      try {
        await progressionApi.updateBaseline(projectId, data.timeline.id, { expected_revision: data.baseline.timeline_revision, changes: {}, confirm: true })
        if (!current(ticket) || !canEditNow()) return
        message.success('基础设定已确认'); changed()
      } catch (err) { if (current(ticket) && canEditNow()) message.error(errorMessage(err)) }
      finally { if (current(ticket)) { busyRef.current = false; setBusy(false) } }
    } })
  }
  const removeEvent = async (event: SettingProgression) => {
    if (!writable() || !data) return
    const ticket = scope.current.capture(); busyRef.current = true; setBusy(true)
    try {
      const latest = await progressionApi.getProgression(projectId, data.timeline.id, event.id)
      if (!current(ticket) || !canEditNow()) return
      if (!latest.item.active || latest.item.revision !== event.revision || latest.timeline_revision !== data.baseline.timeline_revision) throw new Error('变化已更新，请刷新并重新核对后再删除')
      await progressionApi.removeProgression(projectId, data.timeline.id, event.id, latest.timeline_revision)
      if (!current(ticket) || !canEditNow()) return
      if (page > 1 && data.items.length === 1) setPage(page - 1)
      message.success('变化已删除'); changed()
    } catch (err) { if (current(ticket) && canEditNow()) message.error(errorMessage(err)) }
    finally { if (current(ticket)) { busyRef.current = false; setBusy(false) } }
  }
  return <Spin spinning={loading}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {error && <Alert type="warning" showIcon message={error} description={data ? '上次读取的关系资料和正在编辑的草稿仍保留，请刷新后继续。' : undefined} action={<Button size="small" onClick={reload}>刷新</Button>} />}
      {data && <>
        {data.baseline.baseline_status === 'legacy_unverified' && <Alert type="warning" showIcon message="旧资料待校准" description="旧关系的历史可能不完整；请先核对基础关系。" action={canEdit && <Button disabled={busy || !!editor || !baselineValid} onClick={confirmBaseline}>确认校准</Button>} />}
        <Typography.Text strong>基础关系</Typography.Text>
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{baselineText}</Typography.Paragraph>
        <Typography.Text strong>按章节查看关系</Typography.Text>
        <ChapterPositionInput chapters={data.chapters} value={previewPosition} onChange={(p) => { previewEpoch.current += 1; setPreviewPosition(p); setPreview(null) }} />
        <Button onClick={() => void readPreview()}>查看该时点</Button>
        {preview && <Alert type="info" showIcon message={snapshotText(preview, data.characters, data.types)} />}
        <Space wrap><Typography.Text strong>剧情变化</Typography.Text><Button disabled={!canEdit || busy} onClick={() => setEditor({ event: null })}>新增变化</Button><Button onClick={reload}>刷新</Button></Space>
        {data.items.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无剧情变化" /> : <List dataSource={data.items} renderItem={(event) => <List.Item actions={canEdit ? [
          <Button key="edit" type="link" disabled={busy} onClick={() => setEditor({ event })}>编辑</Button>,
          <Popconfirm key="delete" title="删除这条剧情变化？" description="删除后会重新计算该关系的后续状态。" okText="删除" cancelText="取消" onConfirm={() => void removeEvent(event)}><Button type="link" danger disabled={busy}>删除</Button></Popconfirm>,
        ] : []}>
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            <Space wrap><Tag color={event.orphaned ? 'orange' : 'blue'}>{describeAnchor(event, data.chapters)}</Tag><Tag>{describeBoundary(event)}</Tag>
              <Tag>{event.source === 'analysis' ? '章节分析' : event.source === 'legacy_import' ? '旧资料导入' : '手工记录'}</Tag></Space>
            <Typography.Text strong>{FIELD_LABELS[event.field_key as RelationEventField] ?? event.field_key ?? '未知字段'} · {event.operation === 'append' ? '追加' : '替换'}</Typography.Text>
            <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{eventText(event, [a, b], data.characters, data.types)}</Typography.Paragraph>
            {event.note && <Typography.Text type="secondary">{event.note}</Typography.Text>}
          </Space>
        </List.Item>} />}
        {data.total > PAGE_SIZE && <Pagination current={page} pageSize={PAGE_SIZE} total={data.total} onChange={setPage} showSizeChanger={false} />}
        {editor && <RelationEventModal key={`${key}:${editor.event?.id ?? 'new'}`} projectId={projectId} data={data} pair={[a, b]} event={editor.event}
          canEdit={canEdit && !loading && !error} canEditNow={canEditNow} isCurrent={() => activeScope.current === key && canReadNow() && !loading && !error}
          parentError={error} onRefreshParent={reload} onClose={() => setEditor(null)} onSaved={() => { setEditor(null); changed() }} />}
      </>}
    </Space>
  </Spin>
}

interface EditorProps {
  projectId: number; data: Data; pair: readonly [number, number]; event: SettingProgression | null; canEdit: boolean; canEditNow: () => boolean
  isCurrent: () => boolean; parentError: string; onRefreshParent: () => void; onClose: () => void; onSaved: () => void
}
function RelationEventModal({ projectId, data, pair, event, canEdit, canEditNow, isCurrent, parentError, onRefreshParent, onClose, onSaved }: EditorProps) {
  const [draft, setDraft] = useState<RelationEventDraft | null>(() => { try { return draftFor(event, pair, data.chapters) } catch { return null } })
  const [revision, setRevision] = useState(data.baseline.timeline_revision)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(draft ? '' : '此变化内容不完整，无法安全编辑；请联系管理员检查数据。')
  const [conflict, setConflict] = useState(false)
  const [comparison, setComparison] = useState<'unchanged' | 'changed' | 'deleted' | null>(null)
  const [latest, setLatest] = useState<SettingProgression | null>(null)
  const [ack, setAck] = useState(false)
  const mounted = useRef(true); const saving = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const current = () => mounted.current && isCurrent() && canEditNow()
  const update = (changes: Partial<RelationEventDraft>) => setDraft((d) => d ? { ...d, ...changes } : d)
  const updateValues = (changes: Partial<RelationEventDraft['values']>) => setDraft((d) => d ? { ...d, values: { ...d.values, ...changes } } : d)
  const save = async () => {
    if (!draft || saving.current || !current() || conflict || comparison === 'deleted' || comparison === 'changed' && !ack) return
    const result = buildRelationEvent(draft, pair, revision)
    if (!result.ok) { setError(result.error); return }
    saving.current = true; setBusy(true); setError('')
    try {
      if (event) await progressionApi.updateProgression(projectId, data.timeline.id, event.id, result.value)
      else await progressionApi.createProgression(projectId, data.timeline.id, result.value)
      if (current()) onSaved()
    } catch (err) { if (current()) { setError(errorMessage(err)); if (err instanceof ApiError && err.status === 409) setConflict(true) } }
    finally { saving.current = false; if (mounted.current) setBusy(false) }
  }
  const refresh = async () => {
    if (saving.current || !current()) return
    saving.current = true; setBusy(true)
    try {
      const [base, detail] = await Promise.all([progressionApi.getBaseline(projectId, data.timeline.id), event ? progressionApi.getProgression(projectId, data.timeline.id, event.id) : Promise.resolve(null)])
      if (!current()) return
      if (detail && base.timeline_revision !== detail.timeline_revision) throw new Error('设定再次发生变化，请重新读取')
      setRevision(base.timeline_revision); setLatest(detail?.item ?? null); setAck(false)
      setComparison(!event ? 'unchanged' : !detail?.item.active ? 'deleted' : detail.item.revision === event.revision ? 'unchanged' : 'changed')
      setConflict(false); setError('')
    } catch (err) { if (current()) setError(errorMessage(err)) }
    finally { saving.current = false; if (mounted.current) setBusy(false) }
  }
  const names = pair.map((id) => data.characters.find((c) => c.id === id)?.name ?? `角色 ${id}`)
  const options = typeOptions(data.types, names[0], names[1]).map((o) => ({ value: o.value, label: o.label }))
  const selectedType = draft && draft.values.type_id > 0 ? data.types.find((t) => t.id === draft.values.type_id) : null
  const typeValue = draft && selectedType ? typeOptionValue(draft.values.type_id, isSymmetric(selectedType) || draft.values.a_id === pair[0]) : undefined
  return <Modal open title={event ? '编辑剧情变化' : '新增剧情变化'} onCancel={onClose} onOk={() => void save()} okText="保存变化" cancelText="取消" confirmLoading={busy}
    okButtonProps={{ disabled: !canEdit || !draft || conflict || comparison === 'deleted' || comparison === 'changed' && !ack }}
    cancelButtonProps={{ disabled: busy }} closable={!busy} maskClosable={!busy} keyboard={!busy} width={620}>
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {parentError && <Alert type="warning" showIcon message={parentError} description="草稿仍保留，请刷新关系资料后继续。" action={<Button size="small" onClick={onRefreshParent}>重试刷新</Button>} />}
      {event?.orphaned && <Alert type="warning" showIcon message={event.anchor_kind === 'scene' ? '原场景已退场或失联，请选择新的生效位置。' : '原章节已删除，请选择新的生效位置。'} />}
      {error && <Alert type={conflict ? 'warning' : 'error'} showIcon message={error} description={conflict ? '草稿已保留，请先读取最新版本并核对。' : undefined} />}
      {conflict && <Button onClick={() => void refresh()} loading={busy}>读取最新版本</Button>}
      {comparison === 'deleted' && <Alert type="warning" showIcon message="这条变化已被删除；草稿仍保留，请复制后按需新增。" />}
      {comparison === 'changed' && latest && <Alert type="warning" showIcon message="这条变化已被其他操作修改" description={<Space direction="vertical">
        <Typography.Text>当前记录：{describeAnchor(latest, data.chapters)} · {describeBoundary(latest)} · {FIELD_LABELS[latest.field_key as RelationEventField] ?? latest.field_key}</Typography.Text>
        <Typography.Text>{eventText(latest, pair, data.characters, data.types)}</Typography.Text><Checkbox checked={ack} onChange={(e) => setAck(e.target.checked)}>我已比较，确认用当前草稿覆盖</Checkbox>
      </Space>} />}
      {comparison === 'unchanged' && <Alert type="info" showIcon message="已读取最新版本，草稿仍保留。" />}
      {draft && <>
        <label>变化内容</label><Select aria-label="变化内容" value={draft.field} disabled={!canEdit || busy} options={Object.entries(FIELD_LABELS).map(([value, label]) => ({ value, label }))}
          onChange={(field: RelationEventField) => update({ field, operation: 'replace' })} />
        {draft.field === 'presence' && <Radio.Group aria-label="关系建立或结束" disabled={!canEdit || busy} value={draft.presence} onChange={(e) => update({ presence: e.target.value as boolean })}
          options={[{ value: true, label: '建立关系' }, { value: false, label: '结束关系' }]} />}
        {(draft.field === 'identity' || draft.field === 'presence' && draft.presence) && <><label>关系类型和方向</label><Select aria-label="关系类型和方向" showSearch optionFilterProp="label" placeholder="请选择关系类型" value={typeValue} options={options} disabled={!canEdit || busy}
          onChange={(value: string) => { const choice = parseTypeOptionValue(value); if (choice) updateValues({ type_id: choice.typeId, a_id: choice.viewerIsA ? pair[0] : pair[1], b_id: choice.viewerIsA ? pair[1] : pair[0] }) }} /></>}
        {(draft.field === 'intimacy' || draft.field === 'presence' && draft.presence) && <><label>亲密度</label><InputNumber aria-label="亲密度" min={-100} max={100} precision={0} value={draft.values.intimacy} disabled={!canEdit || busy} onChange={(v) => updateValues({ intimacy: v ?? 0 })} /></>}
        {(draft.field === 'status' || draft.field === 'presence' && draft.presence) && <><label>状态</label><Select aria-label="状态" options={statusOptions} value={draft.values.status} disabled={!canEdit || busy} onChange={(status: RelationStatus) => updateValues({ status })} /></>}
        {(draft.field === 'description' || draft.field === 'presence' && draft.presence) && <><label>描述</label>{draft.field === 'description' && <Radio.Group aria-label="描述操作" value={draft.operation} disabled={!canEdit || busy} onChange={(e) => update({ operation: e.target.value as 'replace' | 'append' })}
          options={[{ value: 'replace', label: '替换' }, { value: 'append', label: '追加' }]} />}<Input.TextArea aria-label="描述" value={draft.values.description} disabled={!canEdit || busy} onChange={(e) => updateValues({ description: e.target.value })} autoSize={{ minRows: 2, maxRows: 6 }} /></>}
        <Typography.Text strong>生效位置</Typography.Text><ChapterPositionInput chapters={data.chapters} value={draft.position} onChange={(position) => update({ position })} disabled={!canEdit || busy} />
        <label>同位置排序</label><InputNumber aria-label="同位置排序" min={0} precision={0} value={draft.sortOrder} onChange={(v) => update({ sortOrder: v ?? 0 })} disabled={!canEdit || busy} />
        <label>备注（可选）</label><Input aria-label="备注" maxLength={200} value={draft.note} onChange={(e) => update({ note: e.target.value })} disabled={!canEdit || busy} />
      </>}
    </Space>
  </Modal>
}
