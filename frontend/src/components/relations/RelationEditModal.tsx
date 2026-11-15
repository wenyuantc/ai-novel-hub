import { Alert, App, Button, Checkbox, Col, Form, Input, Modal, Radio, Row, Select, Slider, Space, Spin, Switch } from 'antd'
import { useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { chapterApi } from '../../api/chapters'
import { ApiError, currentRequestGeneration, errorMessage, watchRequestGeneration } from '../../api/client'
import { progressionApi } from '../../api/progressions'
import { intimacyColor, isSymmetric, parseTypeOptionValue, RELATION_CATEGORY, RELATION_STATUS, relationshipApi, typeOptions, typeOptionValue } from '../../api/relationships'
import type { Character, ChapterBrief, ProgressionPositionInput, RelationCategory, Relationship, RelationshipCreate, RelationshipType, RelationStatus, SettingBaseline, SettingTimeline } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { useProjectStore } from '../../stores/project'
import { invalidateProject } from '../../utils/projectRefresh'
import ChapterPositionInput from '../progressions/ChapterPositionInput'
import { toPositionInput } from '../progressions/position'
import { buildRelationUpdate, createRelationRequestScope, readRelationSnapshot, relationSaveGuard, relationSubject, type RelationSnapshot } from './relationForm'

interface Props {
  projectId: number
  open: boolean
  characters: Pick<Character, 'id' | 'name'>[]
  types: RelationshipType[]
  viewerId?: number
  editing?: Relationship | null
  onClose: () => void
  onSaved: (r: Relationship) => void
}
interface FormValues {
  self_id: number
  other_id: number
  type_option?: string
  custom: boolean
  self_role?: string
  other_role?: string
  category: RelationCategory
  intimacy: number
  status: RelationStatus
  description: string
  note?: string
}
type Mode = 'baseline' | 'progression'
interface LoadedSnapshot { snapshot: RelationSnapshot; revision: number; mode: Mode; positionKey: string }
const emptyPosition: ProgressionPositionInput = { anchor_kind: 'chapter', chapter_id: null, chapter_no: null, boundary: 'start' }
const STATUS_OPTIONS = (Object.keys(RELATION_STATUS) as RelationStatus[]).map((k) => ({ value: k, label: RELATION_STATUS[k].label }))
const CATEGORY_OPTIONS = (Object.keys(RELATION_CATEGORY) as RelationCategory[]).map((k) => ({ value: k, label: RELATION_CATEGORY[k].label }))
const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

/** 基础值和所选章节的有效值各自读取，避免用当前状态覆盖早期剧情。 */
export default function RelationEditModal(props: Props) {
  const status = useAuthStore((s) => s.status)
  const actorId = useAuthStore((s) => s.user?.id)
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const { canRead, canEdit } = useProjectAccess(props.projectId)
  if (!props.open || !canRead) return null
  const identity = `${status}:${actorId ?? ''}:${generation}`
  // 失去写权限时卸载草稿；仍可编辑的授权变化不重建表单。
  const key = `${props.projectId}:${props.editing?.id ?? 'new'}:${props.viewerId ?? ''}:${identity}:${canEdit}`
  return <RelationEditSession key={key} {...props} identity={identity} />
}

function RelationEditSession({ projectId, open, characters, types, viewerId, editing, onClose, onSaved, identity }: Props & { identity: string }) {
  const { canEdit, canRead, canReadNow, canEditNow } = useProjectAccess(projectId)
  const scopeVersion = useProjectStore((s) => s.scopeVersion)
  const { message, modal } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const saveGuard = useMemo(() => relationSaveGuard(`${identity}:${projectId}:${editing?.id ?? 'new'}:${viewerId ?? ''}`),
    [identity, projectId, editing?.id, viewerId])
  const saveState = useSyncExternalStore(saveGuard.subscribe, saveGuard.getSnapshot, saveGuard.getSnapshot)
  const saving = saveState.saving
  const [loadingState, setLoadingState] = useState({ version: scopeVersion, value: true })
  const loading = loadingState.version === scopeVersion && loadingState.value
  const setLoading = (value: boolean) => setLoadingState({ version: scopeVersion, value })
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [comparison, setComparison] = useState<'unchanged' | 'changed' | 'missing' | null>(null)
  const [latestSnapshot, setLatestSnapshot] = useState<RelationSnapshot | null>(null)
  const [ack, setAck] = useState(false)
  const [mode, setMode] = useState<Mode>('baseline')
  const [position, setPosition] = useState<ProgressionPositionInput>(emptyPosition)
  const [chapters, setChapters] = useState<ChapterBrief[]>([])
  const [timeline, setTimeline] = useState<SettingTimeline | null>(null)
  const [baseline, setBaseline] = useState<SettingBaseline | null>(null)
  const [loaded, setLoaded] = useState<LoadedSnapshot | null>(null)
  const scope = useRef(createRelationRequestScope())
  const request = useRef(0)
  const initialStarted = useRef(false)
  const initialComplete = useRef(false)
  const selfId = Form.useWatch('self_id', form)
  const otherId = Form.useWatch('other_id', form)
  const custom = Form.useWatch('custom', form)
  const intimacy = Form.useWatch('intimacy', form) ?? 0
  const nameOf = (id: number | undefined) => characters.find((c) => c.id === id)?.name
  const selfName = viewerId !== undefined ? '我' : (nameOf(selfId) ?? '甲')
  const options = useMemo(() => typeOptions(types, selfName, characters.find((c) => c.id === otherId)?.name ?? '对方'), [types, selfName, otherId, characters])
  const positionKey = JSON.stringify(toPositionInput(position))
  const currentLoaded = loaded && loaded.mode === mode && (mode === 'baseline' || loaded.positionKey === positionKey) ? loaded : null

  useLayoutEffect(() => {
    const activeScope = scope.current
    activeScope.invalidate()
    request.current += 1
    return () => { activeScope.invalidate(); request.current += 1 }
  }, [projectId, open, editing?.id, viewerId, identity, canReadNow, canEditNow])

  useEffect(() => {
    if (!open || !canRead || !canReadNow() || initialComplete.current) return
    const ticket = scope.current.capture()
    const readTicket = ++request.current
    const current = () => scope.current.isCurrent(ticket, canReadNow()) && request.current === readTicket
    if (!initialStarted.current) {
      form.resetFields()
      form.setFieldsValue({ self_id: viewerId, custom: false, category: 'other', intimacy: 0, status: 'normal', description: '', note: '' })
      initialStarted.current = true
    }
    const run = async () => {
      setLoading(true)
      try {
        const [chapterList, result] = await Promise.all([
          chapterApi.list(projectId),
          editing ? progressionApi.findTimeline(projectId, relationSubject(editing.a_id, editing.b_id)) : Promise.resolve(null),
        ])
        if (!current()) return
        setChapters(chapterList)
        if (!editing) { initialComplete.current = true; return }
        if (!result) throw new Error('关系时间线不存在，无法安全编辑；请检查数据后重试')
        const base = await progressionApi.getBaseline(projectId, result.id)
        if (!current()) return
        const snapshot = readRelationSnapshot(base.baseline, [editing.a_id, editing.b_id])
        setTimeline(result); setBaseline(base)
        setLoaded({ snapshot, revision: base.timeline_revision, mode: 'baseline', positionKey: '' })
        if (snapshot.exists) {
          const self = viewerId ?? editing.a_id
          const other = self === editing.a_id ? editing.b_id : editing.a_id
          const chosen = types.find((t) => t.id === snapshot.type_id)
          form.setFieldsValue({ self_id: self, other_id: other, type_option: typeOptionValue(snapshot.type_id, !!chosen && isSymmetric(chosen) || self === snapshot.a_id),
            intimacy: snapshot.intimacy, status: snapshot.status, description: snapshot.description })
        }
        initialComplete.current = true
      } catch (err) { if (current()) { initialComplete.current = true; setError(errorMessage(err)) } }
      finally { if (current()) setLoading(false) }
    }
    void run()
  // types may update independently; opening a new relation, changing project or actor resets the request scope.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, projectId, editing?.id, viewerId, identity, canRead, canReadNow, form])

  const readSnapshot = async (compareOnly = false) => {
    if (!editing || !timeline || !canReadNow() || saveGuard.getSnapshot().saving) return
    if (mode === 'progression' && !toPositionInput(position)) { setError('请选择有效的章节位置'); return }
    const ticket = scope.current.capture()
    const readTicket = ++request.current
    const current = () => scope.current.isCurrent(ticket, canReadNow()) && request.current === readTicket
    setLoading(true); setError('')
    try {
      const result = mode === 'baseline'
        ? await progressionApi.getBaseline(projectId, timeline.id)
        : await progressionApi.effectiveAtPosition(projectId, timeline.id, position)
      if (!current()) return
      const fields = 'baseline' in result ? result.baseline : result.fields
      const snapshot = readRelationSnapshot(fields, [editing.a_id, editing.b_id])
      if ('baseline' in result) setBaseline(result)
      if (compareOnly && currentLoaded) {
        setLatestSnapshot(snapshot)
        setComparison(!snapshot.exists ? 'missing' : JSON.stringify(snapshot) === JSON.stringify(currentLoaded.snapshot) ? 'unchanged' : 'changed')
        setLoaded({ ...currentLoaded, revision: result.timeline_revision }); setAck(false); setConflict(false)
        saveGuard.acknowledge()
      } else {
        setLoaded({ snapshot, revision: result.timeline_revision, mode, positionKey: mode === 'baseline' ? '' : positionKey })
        setConflict(false); setComparison(null); setAck(false)
        if (snapshot.exists) {
          const self = viewerId ?? editing.a_id
          const other = self === editing.a_id ? editing.b_id : editing.a_id
          const chosen = types.find((t) => t.id === snapshot.type_id)
          form.setFieldsValue({ self_id: self, other_id: other, type_option: typeOptionValue(snapshot.type_id, !!chosen && isSymmetric(chosen) || self === snapshot.a_id),
            intimacy: snapshot.intimacy, status: snapshot.status, description: snapshot.description, note: '' })
        }
      }
    } catch (err) { if (current()) setError(errorMessage(err)) }
    finally { if (current()) setLoading(false) }
  }

  const requestSnapshot = () => {
    if (loaded && form.isFieldsTouched()) {
      modal.confirm({ title: '读取其他状态？', content: '当前表单中的未保存修改会被替换。', okText: '读取', cancelText: '保留草稿', onOk: () => readSnapshot() })
    } else void readSnapshot()
  }
  const reviewCreation = async () => {
    if (saving || !saveState.uncertain || !canReadNow()) return
    const { self_id: self, other_id: other } = form.getFieldsValue()
    if (!Number.isInteger(self) || !Number.isInteger(other) || self === other) {
      setError('请先选择两位不同的角色，再核对创建结果。')
      return
    }
    const ticket = scope.current.capture()
    const readTicket = ++request.current
    const current = () => scope.current.isCurrent(ticket, canReadNow()) && request.current === readTicket
    setLoading(true)
    try {
      const existing = await progressionApi.findTimeline(projectId, relationSubject(self, other))
      if (!current()) return
      if (existing) {
        setError('已找到这两位角色的关系，请关闭窗口后核对；本地草稿仍保留。')
        return
      }
      saveGuard.acknowledge()
      setError('未找到已创建的关系，已核对最新状态；可继续使用本地草稿。')
    } catch (err) { if (current()) setError(errorMessage(err)) }
    finally { if (current()) setLoading(false) }
  }
  const save = async () => {
    if (!canEditNow() || loading || saveGuard.getSnapshot().uncertain || conflict || comparison === 'missing' || (comparison === 'changed' && !ack)) return
    if (editing && (!currentLoaded || !currentLoaded.snapshot.exists)) { setError('请先读取要修改的基础设定或章节状态'); return }
    const ticket = scope.current.capture()
    const current = () => scope.current.isCurrent(ticket, canEditNow())
    const run = saveGuard.begin(scopeVersion)
    if (run === null) return
    let writeStarted = false
    let failure: { started: boolean; error: unknown } | undefined
    setError('')
    try {
      const v = await form.validateFields()
      if (!current()) return
      const opt = parseTypeOptionValue(v.type_option ?? '')
      let saved: Relationship
      if (editing && currentLoaded) {
        if (!opt) throw new Error('请选择关系类型')
        const chosen = types.find((t) => t.id === opt.typeId)
        const aId = chosen && isSymmetric(chosen) && currentLoaded.snapshot.exists ? currentLoaded.snapshot.a_id : opt.viewerIsA ? v.self_id : v.other_id
        const result = buildRelationUpdate(currentLoaded.snapshot, { a_id: aId, b_id: aId === v.self_id ? v.other_id : v.self_id,
          type_id: opt.typeId, intimacy: v.intimacy, status: v.status, description: v.description ?? '' },
        { mode, revision: currentLoaded.revision, position: mode === 'progression' ? position : null, note: v.note ?? '' })
        if (!result.ok) throw new Error(result.error)
        if (!result.body) { message.info('没有需要保存的修改'); return }
        writeStarted = true
        saved = await relationshipApi.update(projectId, editing.id, result.body)
      } else {
        if (v.self_id === v.other_id) throw new Error('请选择两位不同的角色')
        const pos = mode === 'progression' ? toPositionInput(position) : null
        if (mode === 'progression' && !pos) throw new Error('请选择有效的生效章节')
        const existing = await progressionApi.findTimeline(projectId, relationSubject(v.self_id, v.other_id))
        if (!current()) return
        const common: RelationshipCreate = { a_id: v.self_id, b_id: v.other_id, intimacy: v.intimacy, status: v.status,
          description: v.description ?? '', source: 'manual', note: v.note?.trim() ?? '',
          expected_revision: existing?.revision ?? 0,
          ...(pos ? { anchor_kind: pos.anchor_kind, ...(pos.anchor_kind === 'scene' ? { scene_id: pos.scene_id } : {}), chapter_id: pos.chapter_id, chapter_no: pos.chapter_no, boundary: pos.boundary } : {}) }
        if (v.custom) {
          writeStarted = true
          saved = await relationshipApi.create(projectId, { ...common, role_a: v.self_role?.trim(), role_b: v.other_role?.trim(), category: v.category })
        }
        else {
          if (!opt) throw new Error('请选择关系类型')
          writeStarted = true
          saved = await relationshipApi.create(projectId, { ...common, a_id: opt.viewerIsA ? v.self_id : v.other_id,
            b_id: opt.viewerIsA ? v.other_id : v.self_id, type_id: opt.typeId })
        }
      }
      if (!current()) return
      invalidateProject(projectId, ['relationships', 'characters'])
      message.success('已保存'); onSaved(saved)
    } catch (err) {
      failure = { started: writeStarted, error: err }
      if (!current()) return
      // Ant Form validation already displays field errors.
      if (typeof err === 'object' && err !== null && 'errorFields' in err) return
      setError(errorMessage(err))
      if (err instanceof ApiError && err.status === 409 && editing) { setConflict(true); setComparison(null) }
    } finally {
      saveGuard.finish(run, current(), failure)
    }
  }
  const formatSnapshot = (snapshot: RelationSnapshot) => {
    if (!snapshot.exists) return '此时关系不存在'
    const a = nameOf(snapshot.a_id) ?? `角色 ${snapshot.a_id}`
    const b = nameOf(snapshot.b_id) ?? `角色 ${snapshot.b_id}`
    const type = types.find((t) => t.id === snapshot.type_id)
    const label = type ? (isSymmetric(type) ? `${a} 与 ${b} 是${type.role_a}` : `${a} 是 ${b} 的${type.role_a}`) : `关系类型 ${snapshot.type_id}`
    return `${label}；亲密度 ${snapshot.intimacy}；状态 ${RELATION_STATUS[snapshot.status].label}；描述：${snapshot.description || '（空）'}`
  }
  const charOptions = (exclude?: number) => characters.filter((c) => c.id !== exclude).map((c) => ({ value: c.id, label: c.name }))
  const unavailable = !!editing && (!currentLoaded || !currentLoaded.snapshot.exists)
  return (
    <Modal title={editing ? '编辑关系' : '新增关系'} open={open} onOk={() => void save()} onCancel={onClose} confirmLoading={saving}
      okButtonProps={{ disabled: !canEdit || loading || unavailable || saveState.uncertain || conflict || comparison === 'missing' || (comparison === 'changed' && !ack) }}
      okText="保存" cancelText="取消" width={640} closable={!saving} maskClosable={!saving} keyboard={!saving} cancelButtonProps={{ disabled: saving }}>
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {baseline?.baseline_status === 'legacy_unverified' && <Alert type="warning" showIcon message="旧关系待校准" description="旧作品没有完整的历史状态。请核对基础设定，并在剧情变化中确认校准。" />}
        {saving && saveState.scopeVersion !== scopeVersion && <Alert type="warning" showIcon message="权限已变化，正在等待先前保存结束" description="本地草稿已保留；请求结束后须核对结果，不能自动重发。" />}
        {saveState.uncertain && <Alert type="warning" showIcon message="保存结果尚未确认" description={editing ? '请读取最新状态并核对后再提交。' : '请先检查两位角色的关系是否已创建。'} />}
        {error && <Alert type={conflict ? 'warning' : 'error'} showIcon message={error} description={conflict ? '草稿已保留，请读取最新状态并核对。' : undefined} />}
        {(conflict || saveState.uncertain) && currentLoaded && <Button disabled={loading || saving} onClick={() => void readSnapshot(true)}>读取最新状态并比较</Button>}
        {saveState.uncertain && !editing && <Button disabled={loading || saving} onClick={() => void reviewCreation()}>检查是否已创建</Button>}
        {comparison === 'unchanged' && <Alert type="info" showIcon message="当前状态未变，草稿仍保留，可以继续保存。" />}
        {comparison === 'changed' && latestSnapshot && <Alert type="warning" showIcon message="此位置的关系状态已改变，草稿仍保留。" description={<Space direction="vertical">
          {currentLoaded && <span>读取时：{formatSnapshot(currentLoaded.snapshot)}</span>}<span>最新状态：{formatSnapshot(latestSnapshot)}</span>
          <Checkbox checked={ack} onChange={(e) => setAck(e.target.checked)}>我已比较，确认提交我的修改</Checkbox></Space>} />}
        {comparison === 'missing' && <Alert type="warning" showIcon message="此位置的关系已不存在；请复制草稿并重新查看历史。" />}
        <Spin spinning={loading}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <span>{editing ? '修改方式' : '建立方式'}</span>
              <Radio.Group disabled={loading || saving} value={mode} onChange={(e) => setMode(e.target.value as Mode)}
                options={editing ? [{ value: 'baseline', label: '修订基础设定' }, { value: 'progression', label: '记录剧情变化' }]
                  : [{ value: 'baseline', label: '基础关系' }, { value: 'progression', label: '从指定章节建立' }]} />
            {mode === 'progression' && <><span>生效位置</span><ChapterPositionInput chapters={chapters} value={position} onChange={setPosition} disabled={loading || saving} /></>}
            {editing && <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }}>
              <Button onClick={requestSnapshot} disabled={loading || saving || !timeline}>{mode === 'baseline' ? '读取基础设定' : '读取该章节状态'}</Button>
              {!currentLoaded && <Alert type="info" showIcon message="选择位置后读取状态，再进行修改。现有草稿会保留到确认读取。" />}
              {currentLoaded && !currentLoaded.snapshot.exists && <Alert type="info" showIcon message="此时关系尚未建立，请在剧情变化中记录建立。" />}
            </Space>}
          </Space>
          <Form form={form} layout="vertical" disabled={!canEdit || saving || loading || !!editing && !currentLoaded?.snapshot.exists}>
            <Row gutter={16}>
              <Col span={12}><Form.Item name="self_id" label="角色" rules={[{ required: true, message: '请选择角色' }]} hidden={viewerId !== undefined}>
                <Select showSearch optionFilterProp="label" options={charOptions(otherId)} disabled={!!editing} /></Form.Item></Col>
              <Col span={viewerId !== undefined ? 24 : 12}><Form.Item name="other_id" label="对方" rules={[{ required: true, message: '请选择对方' }]}>
                <Select showSearch optionFilterProp="label" options={charOptions(selfId)} disabled={!!editing} /></Form.Item></Col>
            </Row>
            {!editing && <Form.Item name="custom" label="使用自定义称呼" valuePropName="checked"><Switch /></Form.Item>}
            {custom && !editing ? <Row gutter={16}>
              <Col span={8}><Form.Item name="other_role" label={`对方是${selfName}的`} rules={[{ required: true, whitespace: true, message: '请输入称呼' }, { max: 20 }]}><Input /></Form.Item></Col>
              <Col span={8}><Form.Item name="self_role" label={`${selfName}是对方的`} rules={[{ required: true, whitespace: true, message: '请输入称呼' }, { max: 20 }]}><Input /></Form.Item></Col>
              <Col span={8}><Form.Item name="category" label="类别"><Select options={CATEGORY_OPTIONS} /></Form.Item></Col>
            </Row> : <Form.Item name="type_option" label="关系类型" rules={[{ required: true, message: '请选择关系类型' }]}>
              <Select showSearch optionFilterProp="label" options={(Object.keys(RELATION_CATEGORY) as RelationCategory[]).map((cat) => ({ label: RELATION_CATEGORY[cat].label,
                options: options.filter((o) => o.category === cat).map((o) => ({ value: o.value, label: o.label })) })).filter((g) => g.options.length > 0)} />
            </Form.Item>}
            <Form.Item name="intimacy" label={<span style={{ color: intimacyColor(intimacy) }}>亲密度：{intimacy}</span>}><Slider min={-100} max={100} marks={{ '-100': '-100', 0: '0', 100: '100' }} /></Form.Item>
            <Form.Item name="status" label="状态"><Select options={STATUS_OPTIONS} /></Form.Item>
            <Form.Item name="description" label="描述"><Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} /></Form.Item>
            <Form.Item name="note" label="变更备注（可选）" rules={[{ max: 200, message: '不超过 200 字' }]}><Input /></Form.Item>
          </Form>
        </Spin>
      </Space>
    </Modal>
  )
}
