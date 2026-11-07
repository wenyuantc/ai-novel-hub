import { PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Empty, Form, Input, List, Modal, Popconfirm, Radio, Select, Space, Spin, Table, Tag, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { characterExtraApi } from '../../api/characters'
import { chapterApi } from '../../api/chapters'
import { ApiError, errorMessage } from '../../api/client'
import { MEMBER_STATUS, memberApi, memberChanges, memberPositionIntent, memberSnapshot, memberStatusLabel, organizationApi, type MemberSnapshot, type MemberState } from '../../api/organizations'
import { progressionApi } from '../../api/progressions'
import { characterApi } from '../../api/story'
import type { ChapterBrief, Character, Member, ProgressionPositionInput, SettingProgression, SettingTimeline } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { invalidateProject } from '../../utils/projectRefresh'
import ChapterPositionInput from '../progressions/ChapterPositionInput'
import { describeAnchor, describeBoundary, toPositionInput } from '../progressions/position'

const STATUS_OPTIONS = Object.entries(MEMBER_STATUS).map(([value, item]) => ({ value, label: item.label }))
const EMPTY_STATE: MemberState = { position: '', status: 'active', note: '' }
const EMPTY_POSITION: ProgressionPositionInput = { anchor_kind: 'chapter', chapter_id: null, chapter_no: null, boundary: 'start' }
const EVENT_LABELS: Record<string, string> = { presence: '成员归属', position: '职位', status: '状态', note: '备注' }

type Scope = { kind: 'character'; id: number } | { kind: 'organization'; id: number }
interface Target { characterId: number; organizationId: number; label: string; member: Member | null; timeline: SettingTimeline | null }
interface TargetList { scope: string; members: Member[]; timelines: SettingTimeline[]; choices: { id: number; name: string }[]; chapters: ChapterBrief[] }
interface LoadedSnapshot { key: string; snapshot: MemberSnapshot; revision: number | null }
interface Draft { snapshot: MemberSnapshot; values: MemberState }

function snapshotEqual(a: MemberSnapshot, b: MemberSnapshot): boolean {
  return a.exists === b.exists && (!a.exists || (b.exists && a.state.position === b.state.position && a.state.status === b.state.status && a.state.note === b.state.note))
}

function draftChanged(draft: Draft): boolean {
  const initial = draft.snapshot.exists ? draft.snapshot.state : EMPTY_STATE
  return Object.keys(memberChanges(initial, draft.values)).length > 0
}

function positionKey(mode: 'baseline' | 'progression', position: ProgressionPositionInput): string {
  return mode === 'baseline' ? 'baseline' : `${position.anchor_kind}:${position.scene_id ?? ''}:${position.chapter_id ?? ''}:${position.chapter_no ?? ''}:${position.boundary}`
}

function snapshotText(snapshot: MemberSnapshot): string {
  return snapshot.exists ? `${snapshot.state.position || '未设职位'} · ${memberStatusLabel(snapshot.state.status).label}` : '该时点尚未加入'
}

/** 角色与组织入口共用成员历史；目标以角色与组织的稳定 ID 标识。 */
function Memberships({ projectId, scope, onChanged }: { projectId: number; scope: Scope; onChanged?: () => void }) {
  const { canRead, canEdit, canReadNow } = useProjectAccess(projectId)
  const user = useAuthStore((state) => state.user)
  const authStatus = useAuthStore((state) => state.status)
  const [data, setData] = useState<TargetList | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [version, setVersion] = useState(0)
  const [choosing, setChoosing] = useState<string | null>(null)
  const [choice, setChoice] = useState<number | null>(null)
  const [selected, setSelected] = useState<{ id: number; scope: string } | null>(null)
  const scopeKey = `${projectId}:${scope.kind}:${scope.id}:${user?.id ?? 'none'}:${authStatus}:${authorizationKey(user)}`
  const refresh = useCallback(() => setVersion((value) => value + 1), [])
  useProjectRefresh(projectId, ['organizations', 'characters'], refresh)

  useEffect(() => {
    if (!canRead || !canReadNow()) return
    let active = true
    const load: Promise<[Member[], { items: SettingTimeline[] }, { id: number; name: string }[], ChapterBrief[]]> = scope.kind === 'character'
      ? Promise.all([
          characterExtraApi.memberships(projectId, scope.id),
          progressionApi.listTimelines(projectId, { subject_kind: 'membership', character_id: scope.id }),
          organizationApi.list(projectId), chapterApi.list(projectId),
        ])
      : Promise.all([
          memberApi.list(projectId, scope.id),
          progressionApi.listTimelines(projectId, { subject_kind: 'membership', organization_id: scope.id }),
          characterApi.list(projectId), chapterApi.list(projectId),
        ])
    void Promise.resolve().then(() => {
      if (active && canReadNow()) { setLoading(true); setError('') }
      return load
    }).then(([members, timelines, choices, chapters]) => {
      if (active && canReadNow()) setData({ scope: scopeKey, members, timelines: timelines.items, choices, chapters })
    }, (err: unknown) => {
      if (active && canReadNow()) setError(errorMessage(err))
    }).finally(() => { if (active && canReadNow()) setLoading(false) })
    return () => { active = false }
  }, [projectId, scope.kind, scope.id, scopeKey, version, canRead, canReadNow])

  const visible = data?.scope === scopeKey ? data : null
  const targets = useMemo(() => {
    if (!visible) return []
    return visible.choices.map((choice): Target => {
      const characterId = scope.kind === 'character' ? scope.id : choice.id
      const organizationId = scope.kind === 'organization' ? scope.id : choice.id
      return {
        characterId, organizationId, label: choice.name,
        member: visible.members.find((item) => item.character_id === characterId && item.organization_id === organizationId) ?? null,
        timeline: visible.timelines.find((item) => item.character_id === characterId && item.organization_id === organizationId) ?? null,
      }
    })
  }, [visible, scope.kind, scope.id])
  const targetId = (target: Target) => scope.kind === 'character' ? target.organizationId : target.characterId
  const selectedTarget = selected?.scope === scopeKey ? targets.find((target) => targetId(target) === selected.id) ?? null : null
  const rows = targets.filter((target) => target.member || target.timeline)
  const changed = () => {
    invalidateProject(projectId, ['organizations', 'characters'])
    onChanged?.()
  }

  if (!canRead) return null
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={refresh}>重试</Button>} />}
      <Button icon={<PlusOutlined />} disabled={!canEdit || !visible || !targets.some((target) => !target.member)} onClick={() => { setChoice(null); setChoosing(scopeKey) }}>
        {scope.kind === 'character' ? '加入组织' : '添加成员'}
      </Button>
      <Table<Target>
        size="small" rowKey={(item) => `${item.characterId}:${item.organizationId}`} loading={loading} dataSource={rows} pagination={false}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无成员记录" /> }}
        columns={[
          { title: scope.kind === 'character' ? '组织' : '角色', dataIndex: 'label' },
          { title: '当前职位', render: (_, item) => item.member?.position || '-' },
          { title: '当前状态', render: (_, item) => item.member ? <Tag color={memberStatusLabel(item.member.status).color}>{memberStatusLabel(item.member.status).label}</Tag> : <Tag>已退出，保留历史</Tag> },
          { title: '', render: (_, item) => <Button type="link" size="small" onClick={() => setSelected({ id: targetId(item), scope: scopeKey })}>{canEdit ? '维护' : '查看'}</Button> },
        ]}
      />
      <Modal title={scope.kind === 'character' ? '选择组织' : '选择角色'} open={choosing === scopeKey} okText="继续" cancelText="取消" okButtonProps={{ disabled: choice === null }}
        onOk={() => { setChoosing(null); if (choice !== null) setSelected({ id: choice, scope: scopeKey }) }} onCancel={() => setChoosing(null)}>
        <Select aria-label={scope.kind === 'character' ? '选择组织' : '选择角色'} style={{ width: '100%' }} showSearch optionFilterProp="label" value={choice ?? undefined}
          onChange={(value: number) => setChoice(value)} options={targets.map((item) => ({ value: targetId(item), label: `${item.label}${item.timeline && !item.member ? '（已有历史，可重新加入）' : ''}`, disabled: !!item.member }))} />
      </Modal>
      {selectedTarget && visible && <MembershipEditor key={`${scopeKey}:${selectedTarget.characterId}:${selectedTarget.organizationId}`} projectId={projectId} target={selectedTarget} chapters={visible.chapters} onClose={() => setSelected(null)} onChanged={changed} />}
    </Space>
  )
}

interface EditorProps { projectId: number; target: Target; chapters: ChapterBrief[]; onClose: () => void; onChanged: () => void }

export function MembershipEditor({ projectId, target, chapters, onClose, onChanged }: EditorProps) {
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(projectId)
  const { message, modal } = App.useApp()
  const [form] = Form.useForm<MemberState>()
  const [mode, setMode] = useState<'baseline' | 'progression'>('baseline')
  const [position, setPosition] = useState<ProgressionPositionInput>({ ...EMPTY_POSITION })
  const [loaded, setLoaded] = useState<LoadedSnapshot | null>(null)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [reload, setReload] = useState(0)
  const [events, setEvents] = useState<{ items: SettingProgression[]; revision: number; total: number } | null>(null)
  const [page, setPage] = useState(1)
  const [eventEditing, setEventEditing] = useState<SettingProgression | null>(null)
  const [awaitTimeline, setAwaitTimeline] = useState(false)
  const drafts = useRef(new Map<string, Draft>())
  const request = useRef(0)
  const eventsRequest = useRef(0)
  const current = useRef(true)
  const savingRef = useRef(false)
  const readingRef = useRef(true)
  const hadTimeline = useRef(target.timeline !== null)
  const key = positionKey(mode, position)
  const normalized = useMemo(() => toPositionInput(position), [position])
  const timelineId = target.timeline?.id ?? null
  const readCurrent = () => current.current && canReadNow()
  const writeCurrent = () => readCurrent() && canEditNow()
  useEffect(() => { current.current = true; return () => { current.current = false; request.current += 1; eventsRequest.current += 1 } }, [])

  useEffect(() => {
    const generation = ++request.current
    readingRef.current = true
    if (timelineId !== null) hadTimeline.current = true
    const controller = new AbortController()
    const valid = () => current.current && generation === request.current && canReadNow()
    void Promise.resolve().then<{ snapshot: MemberSnapshot; revision: number | null } | null>(() => {
      if (!valid()) return null
      setError('')
      setConflict(false)
      if (timelineId === null && hadTimeline.current) {
        setError('该成员资料已被删除，请关闭后重新选择；未保存的输入仍保留在当前窗口。')
        setLoading(false)
        return null
      }
      setLoaded(null)
      if (mode === 'progression' && !normalized) { readingRef.current = false; setLoading(false); return null }
      setLoading(true)
      return timelineId === null
        ? { snapshot: { exists: false } as MemberSnapshot, revision: null }
        : mode === 'baseline'
          ? progressionApi.getBaseline(projectId, timelineId, controller.signal).then((result) => ({ snapshot: memberSnapshot(result.baseline), revision: result.timeline_revision }))
          : progressionApi.effectiveAtPosition(projectId, timelineId, normalized!, controller.signal).then((result) => ({ snapshot: memberSnapshot(result.fields), revision: result.timeline_revision }))
    }).then((result) => {
      if (!result) return
      const { snapshot, revision } = result
      if (!valid()) return
      const draft = drafts.current.get(key)
      const differs = !!draft && !snapshotEqual(draft.snapshot, snapshot)
      setLoaded({ key, snapshot, revision })
      setConflict(differs)
      setError(differs ? '该时点的成员设定已变化，输入已保留。请复制需要的内容，再重新载入。' : '')
      form.setFieldsValue(draft?.values ?? (snapshot.exists ? snapshot.state : EMPTY_STATE))
    }, (err: unknown) => {
      if (valid() && !controller.signal.aborted) {
        const draft = drafts.current.get(key)
        if (draft) {
          setLoaded({ key, snapshot: draft.snapshot, revision: null })
          form.setFieldsValue(draft.values)
        }
        setError(errorMessage(err))
      }
    })
      .finally(() => { if (valid()) { readingRef.current = false; setLoading(false) } })
    return () => controller.abort()
  }, [projectId, timelineId, mode, key, normalized, reload, form, canReadNow])

  const loadEvents = useCallback(async () => {
    if (timelineId === null) return
    const generation = ++eventsRequest.current
    try {
      const result = await progressionApi.listProgressions(projectId, timelineId, { offset: (page - 1) * 50, limit: 50 })
      if (current.current && generation === eventsRequest.current && canReadNow()) setEvents({ items: result.items, revision: result.timeline_revision, total: result.total })
    } catch (err) {
      if (current.current && generation === eventsRequest.current && canReadNow()) setError(errorMessage(err))
    }
  }, [projectId, timelineId, page, canReadNow])
  useEffect(() => { void Promise.resolve().then(loadEvents) }, [loadEvents, reload])

  const changed = () => { readingRef.current = true; setLoading(true); setReload((value) => value + 1); onChanged() }
  const hasDraft = () => [...drafts.current.values()].some(draftChanged)
  const close = () => {
    if (!hasDraft()) { onClose(); return }
    modal.confirm({ title: '放弃未保存的成员修改？', content: '关闭后这些输入不会保存。', okText: '放弃修改', cancelText: '继续编辑', onOk: onClose })
  }
  const onValuesChange = () => {
    if (!conflict && loaded?.key === key) drafts.current.set(key, { snapshot: loaded.snapshot, values: { ...form.getFieldsValue(true) } })
  }
  const handleError = (err: unknown) => {
    if (!readCurrent()) return
    if (err instanceof ApiError && err.status === 409) setError(`${errorMessage(err)}。当前输入已保留，请刷新并核对。`)
    else setError(errorMessage(err))
  }
  const refreshSnapshot = () => { readingRef.current = true; setLoading(true); setReload((value) => value + 1) }
  const requireReady = () => writeCurrent() && !!loaded && loaded.key === key && !loading && !readingRef.current && !(awaitTimeline && timelineId === null) && !conflict && !error && !savingRef.current

  const save = async () => {
    if (!requireReady() || !loaded) return
    let values: MemberState
    try { values = await form.validateFields() } catch { return }
    if (!requireReady()) return
    const state: MemberState = { ...values, position: values.position.trim() }
    const delta = loaded.snapshot.exists ? memberChanges(loaded.snapshot.state, state) : null
    if (delta && Object.keys(delta).length === 0) { message.info('没有需要保存的修改'); return }
    savingRef.current = true
    setSaving(true)
    try {
      if (timelineId === null) {
        const intent = mode === 'baseline'
          ? { edit_mode: 'baseline' as const, expected_revision: 0 }
          : { ...memberPositionIntent(position, 1), expected_revision: 0 }
        await memberApi.create(projectId, target.organizationId, { character_id: target.characterId, ...state, ...intent })
        if (writeCurrent()) setAwaitTimeline(true)
      } else if (mode === 'baseline') {
        if (loaded.revision === null) throw new Error('基础版本不可用，请刷新')
        if (loaded.snapshot.exists && target.member) {
          await memberApi.update(projectId, target.organizationId, target.member.id, { ...delta, edit_mode: 'baseline', expected_revision: loaded.revision })
        } else {
          await progressionApi.updateBaseline(projectId, timelineId, {
            expected_revision: loaded.revision, changes: loaded.snapshot.exists ? { ...delta } : { exists: true, ...state },
          })
        }
      } else {
        if (loaded.revision === null || !normalized) throw new Error('章节位置或设定版本不可用')
        if (!loaded.snapshot.exists) {
          await progressionApi.createProgression(projectId, timelineId, {
            expected_revision: loaded.revision, field_key: 'presence', custom_field_id: null, operation: 'replace',
            value: { exists: true, state: { ...state } }, ...normalized, sort_order: 0, note: '',
          })
        } else if (target.member) {
          await memberApi.update(projectId, target.organizationId, target.member.id, { ...delta, ...memberPositionIntent(position, loaded.revision) })
        } else {
          const entries = Object.entries(delta ?? {})
          if (entries.length !== 1) throw new Error('历史成员每次可修改一个字段，请分别保存职位、状态和备注')
          await progressionApi.createProgression(projectId, timelineId, {
            expected_revision: loaded.revision, field_key: entries[0][0], custom_field_id: null, operation: 'replace',
            value: entries[0][1], ...normalized, sort_order: 0, note: '',
          })
        }
      }
      if (!writeCurrent()) return
      drafts.current.delete(key)
      message.success('成员设定已保存')
      changed()
    } catch (err) { handleError(err) } finally { if (current.current) { savingRef.current = false; setSaving(false) } }
  }

  const exit = async () => {
    if (!requireReady() || !loaded?.snapshot.exists || loaded.revision === null || timelineId === null || !normalized) return
    if (hasDraft()) { message.warning('请先保存或放弃未保存的成员修改'); return }
    savingRef.current = true
    setSaving(true)
    try {
      if (target.member) {
        await memberApi.remove(projectId, target.organizationId, target.member.id, { ...memberPositionIntent(position, loaded.revision), edit_mode: 'progression' })
      } else {
        await progressionApi.createProgression(projectId, timelineId, {
          expected_revision: loaded.revision, field_key: 'presence', custom_field_id: null, operation: 'replace', value: { exists: false },
          ...normalized, sort_order: 0, note: '',
        })
      }
      if (!writeCurrent()) return
      message.success('已记录剧情退出')
      changed()
    } catch (err) { handleError(err) } finally { if (current.current) { savingRef.current = false; setSaving(false) } }
  }

  const clearBaseline = async () => {
    if (!requireReady() || timelineId === null || loaded?.revision === null || !loaded?.snapshot.exists) return
    if (hasDraft()) { message.warning('请先保存或放弃未保存的成员修改'); return }
    savingRef.current = true
    setSaving(true)
    try {
      await progressionApi.updateBaseline(projectId, timelineId, { expected_revision: loaded.revision, changes: { exists: false } })
      if (!writeCurrent()) return
      message.success('已修正基础归属')
      changed()
    } catch (err) { handleError(err) } finally { if (current.current) { savingRef.current = false; setSaving(false) } }
  }

  const confirmBaseline = async () => {
    if (!requireReady() || mode !== 'baseline' || timelineId === null || !loaded || loaded.revision === null) return
    if (hasDraft()) { message.warning('请先保存或放弃未保存的成员修改'); return }
    savingRef.current = true
    setSaving(true)
    try {
      await progressionApi.updateBaseline(projectId, timelineId, { expected_revision: loaded.revision, changes: {}, confirm: true })
      if (!writeCurrent()) return
      message.success('成员基础设定已确认')
      changed()
    } catch (err) { handleError(err) } finally { if (current.current) { savingRef.current = false; setSaving(false) } }
  }

  const deleteRecord = async () => {
    if (!requireReady() || timelineId === null || loaded?.revision === null || !loaded) return
    if (hasDraft()) { message.warning('请先保存或放弃未保存的成员修改'); return }
    savingRef.current = true
    setSaving(true)
    try {
      await memberApi.removeRecord(projectId, target.organizationId, target.characterId, loaded.revision)
      if (!writeCurrent()) return
      message.success('成员资料及历史已删除')
      onChanged()
      onClose()
    } catch (err) { handleError(err) } finally { if (current.current) { savingRef.current = false; setSaving(false) } }
  }

  const deleteEvent = async (event: SettingProgression) => {
    if (!events || timelineId === null || !writeCurrent() || savingRef.current) return
    savingRef.current = true
    setSaving(true)
    try {
      await progressionApi.removeProgression(projectId, timelineId, event.id, events.revision)
      if (!writeCurrent()) return
      message.success('变化已删除')
      changed()
    } catch (err) { handleError(err) } finally { if (current.current) { savingRef.current = false; setSaving(false) } }
  }

  return (
    <Modal title={`成员设定：${target.label}`} open onCancel={close} onOk={() => void save()} okText={loaded?.snapshot.exists ? '保存修改' : '记录加入'} cancelText="关闭" confirmLoading={saving}
      okButtonProps={{ disabled: !canEdit || !loaded || loading || (awaitTimeline && timelineId === null) || conflict || !!error }} width={720} destroyOnClose>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {target.timeline?.baseline_status === 'legacy_unverified' && <Alert type="warning" showIcon message="旧资料待校准" description="现有资料作为初始设定，未记录的旧历史无法自动还原。"
          action={<Popconfirm title="确认成员基础设定已校准？" description="请先检查并保存所有基础字段。" onConfirm={() => void confirmBaseline()}><Button size="small" disabled={!canEdit || mode !== 'baseline' || !loaded || loading || saving || conflict || !!error}>确认已校准</Button></Popconfirm>} />}
        {error && <Alert type="error" showIcon message={error} action={conflict ? <Popconfirm title="丢弃此时点的草稿并重新载入？" onConfirm={() => { drafts.current.delete(key); refreshSnapshot() }}><Button size="small">重新载入</Button></Popconfirm> : <Button size="small" onClick={refreshSnapshot}>刷新</Button>} />}
        <Radio.Group aria-label="成员编辑意图" value={mode} onChange={(e) => { readingRef.current = true; setMode(e.target.value) }} disabled={saving} options={[{ label: '基础设定', value: 'baseline' }, { label: '章节变化', value: 'progression' }]} />
        {mode === 'progression' && <ChapterPositionInput chapters={chapters} value={position} onChange={(next) => { readingRef.current = true; setPosition(next) }} disabled={saving} />}
        {mode === 'progression' && !normalized && <Typography.Text type="secondary">选择生效章节后读取该时点的成员状态。</Typography.Text>}
        <Spin spinning={loading}>
          {loaded?.key === key && <>
            <Typography.Paragraph type="secondary">{snapshotText(loaded.snapshot)}{!loaded.snapshot.exists && '，可填写资料并记录加入。'}</Typography.Paragraph>
            <Form form={form} layout="vertical" disabled={!canEdit || saving} onValuesChange={onValuesChange}>
              <Form.Item name="position" label="职位" rules={[{ max: 50, message: '不超过 50 字' }]}><Input readOnly={conflict} /></Form.Item>
              <Form.Item name="status" label="状态" rules={[{ required: true, message: '请选择状态' }]}><Select options={STATUS_OPTIONS} disabled={conflict} /></Form.Item>
              <Form.Item name="note" label="备注"><Input.TextArea readOnly={conflict} autoSize={{ minRows: 1, maxRows: 4 }} /></Form.Item>
            </Form>
            {loaded.snapshot.exists && mode === 'progression' && <Popconfirm title="在所选章节记录退出？" description="之前章节的成员记录会保留。" okText="记录退出" cancelText="取消" onConfirm={() => void exit()}><Button danger disabled={!canEdit || saving || conflict || !!error}>剧情退出</Button></Popconfirm>}
            {loaded.snapshot.exists && mode === 'baseline' && timelineId !== null && <Popconfirm title="从基础设定移除成员？" description="后续加入和退出变化仍会保留。" onConfirm={() => void clearBaseline()}><Button danger disabled={!canEdit || saving || conflict || !!error}>从基础设定移除</Button></Popconfirm>}
          </>}
        </Spin>
        {timelineId !== null && <Card size="small" title="剧情变化" extra={<Button size="small" onClick={() => void loadEvents()}>刷新</Button>}>
          <List size="small" dataSource={events?.items ?? []} locale={{ emptyText: '暂无剧情变化' }} renderItem={(event) => <List.Item actions={[
            <Button key="edit" type="link" size="small" onClick={() => setEventEditing(event)}>{canEdit ? '编辑' : '查看'}</Button>,
            <Popconfirm key="delete" title="删除这条剧情变化？" description="其余历史将重新计算。" onConfirm={() => void deleteEvent(event)}><Button type="link" size="small" danger disabled={!canEdit || saving}>删除</Button></Popconfirm>,
          ]}><List.Item.Meta title={<Space wrap><span>{EVENT_LABELS[event.field_key ?? ''] ?? '未知变化'}</span>{event.operation === 'append' && <Tag>追加</Tag>}{event.orphaned && <Tag color="warning">{event.anchor_kind === 'scene' ? '场景已退场或失联' : '关联章节已删除'}</Tag>}</Space>}
            description={<><span>{describeAnchor(event, chapters)} · {describeBoundary(event)}</span><br /><span style={{ whiteSpace: 'pre-wrap' }}>{formatMemberEvent(event)}</span></>} /></List.Item>} />
          {events && events.total > 50 && <Space><Button size="small" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</Button><span>第 {page} 页</span><Button size="small" disabled={page * 50 >= events.total} onClick={() => setPage(page + 1)}>下一页</Button></Space>}
        </Card>}
        {timelineId !== null && <Popconfirm title="删除全部成员资料和历史？" description="该角色在此组织的所有基础设定和剧情变化都会删除，无法撤销。" okText="删除全部历史" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => void deleteRecord()}><Button danger disabled={!canEdit || saving || !loaded || !!error || conflict}>删除完整成员资料</Button></Popconfirm>}
      </Space>
      {eventEditing && events && timelineId !== null && <MembershipEventEditor key={eventEditing.id} projectId={projectId} timelineId={timelineId} event={eventEditing} revision={events.revision} chapters={chapters} onClose={() => setEventEditing(null)} onChanged={() => { setEventEditing(null); changed() }} />}
    </Modal>
  )
}

function eventPresence(event: SettingProgression): MemberSnapshot | null {
  const value = event.value
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  if (value.exists === false) return { exists: false }
  if (value.exists !== true || !value.state || typeof value.state !== 'object' || Array.isArray(value.state)) return null
  try { return memberSnapshot({ ...value.state, exists: true }) } catch { return null }
}

function formatMemberEvent(event: SettingProgression): string {
  if (event.field_key === 'presence') {
    const snapshot = eventPresence(event)
    if (!snapshot) return '成员归属资料无法读取'
    if (!snapshot.exists) return '退出组织'
    return ['加入组织', snapshot.state.position || '未设职位', memberStatusLabel(snapshot.state.status).label, snapshot.state.note].filter(Boolean).join(' · ')
  }
  if (event.field_key === 'status' && typeof event.value === 'string') return memberStatusLabel(event.value).label
  return typeof event.value === 'string' ? event.value || '（空）' : '变化内容无法读取'
}

function MembershipEventEditor({ projectId, timelineId, event, revision, chapters, onClose, onChanged }: { projectId: number; timelineId: number; event: SettingProgression; revision: number; chapters: ChapterBrief[]; onClose: () => void; onChanged: () => void }) {
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(projectId)
  const { modal } = App.useApp()
  const originalPosition: ProgressionPositionInput = { anchor_kind: event.anchor_kind, ...(event.anchor_kind === 'scene' ? { scene_id: event.scene_id } : {}), chapter_id: event.chapter_id, chapter_no: event.chapter_no, boundary: event.boundary }
  const initialPresence = event.field_key === 'presence' ? eventPresence(event) : null
  const [position, setPosition] = useState<ProgressionPositionInput>(originalPosition)
  const [value, setValue] = useState(() => typeof event.value === 'string' ? event.value : '')
  const [presence, setPresence] = useState<MemberSnapshot | null>(initialPresence)
  const [expectedRevision, setExpectedRevision] = useState(revision)
  const [error, setError] = useState('')
  const [stale, setStale] = useState(false)
  const [saving, setSaving] = useState(false)
  const mounted = useRef(true)
  const busy = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const changed = positionKey('progression', position) !== positionKey('progression', originalPosition) ||
    (event.field_key === 'presence' ? JSON.stringify(presence) !== JSON.stringify(initialPresence) : value !== event.value)
  const close = () => {
    if (!changed) { onClose(); return }
    modal.confirm({ title: '放弃未保存的变化修改？', content: '关闭后这些输入不会保存。', okText: '放弃修改', cancelText: '继续编辑', onOk: onClose })
  }
  const refresh = async () => {
    if (!canReadNow() || busy.current) return
    busy.current = true
    setSaving(true)
    try {
      const latest = await progressionApi.getProgression(projectId, timelineId, event.id)
      if (!mounted.current || !canReadNow()) return
      if (!latest.item.active || latest.item.revision !== event.revision) {
        setError('这条变化已被其他位置修改，当前输入已保留。请复制需要的内容，再重新打开变化。')
      } else {
        setExpectedRevision(latest.timeline_revision)
        setStale(false)
        setError('')
      }
    } catch (err) { if (mounted.current && canReadNow()) setError(errorMessage(err)) }
    finally { if (mounted.current) { busy.current = false; setSaving(false) } }
  }
  const save = async () => {
    if (!canEditNow() || busy.current || stale) return
    const anchor = toPositionInput(position)
    if (!anchor) { setError('请选择有效章节'); return }
    if (event.field_key === 'presence' && !presence) { setError('无法读取原有成员归属，请刷新后重试'); return }
    const nextValue: SettingProgression['value'] = event.field_key === 'presence'
      ? presence?.exists ? { exists: true, state: { ...presence.state, position: presence.state.position.trim() } } : { exists: false }
      : value
    busy.current = true
    setSaving(true)
    try {
      await progressionApi.updateProgression(projectId, timelineId, event.id, { expected_revision: expectedRevision, value: nextValue, ...anchor })
      if (mounted.current && canEditNow()) onChanged()
    } catch (err) {
      if (mounted.current && canEditNow()) {
        setError(`${errorMessage(err)}。当前输入已保留。`)
        if (err instanceof ApiError && err.status === 409) setStale(true)
      }
    } finally { if (mounted.current) { busy.current = false; setSaving(false) } }
  }
  return <Modal title={`编辑变化：${EVENT_LABELS[event.field_key ?? ''] ?? '成员'}`} open onCancel={close} onOk={() => void save()} confirmLoading={saving} okButtonProps={{ disabled: !canEdit || stale || (event.field_key === 'presence' && !presence) }} okText="保存变化" cancelText="取消">
    <Space direction="vertical" style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} action={stale && <Button size="small" onClick={() => void refresh()}>刷新并核对</Button>} />}
      <ChapterPositionInput chapters={chapters} value={position} onChange={setPosition} disabled={!canEdit || saving} />
      {event.field_key === 'presence' ? <>
        <Typography.Text>成员归属</Typography.Text>
        <Radio.Group aria-label="成员归属变化" value={presence?.exists} disabled={!canEdit || saving || !presence} onChange={(e) => setPresence(e.target.value ? { exists: true, state: presence?.exists ? presence.state : { ...EMPTY_STATE } } : { exists: false })} options={[{ label: '加入组织', value: true }, { label: '退出组织', value: false }]} />
        {presence?.exists && <>
          <label>职位<Input aria-label="加入时职位" maxLength={50} value={presence.state.position} disabled={!canEdit || saving} onChange={(e) => setPresence({ exists: true, state: { ...presence.state, position: e.target.value } })} /></label>
          <label>状态<Select aria-label="加入时状态" style={{ width: '100%' }} value={presence.state.status} options={STATUS_OPTIONS} disabled={!canEdit || saving} onChange={(status: MemberState['status']) => setPresence({ exists: true, state: { ...presence.state, status } })} /></label>
          <label>备注<Input.TextArea aria-label="加入时备注" value={presence.state.note} disabled={!canEdit || saving} onChange={(e) => setPresence({ exists: true, state: { ...presence.state, note: e.target.value } })} autoSize={{ minRows: 2, maxRows: 8 }} /></label>
        </>}
      </> : <>
        <Typography.Text>{EVENT_LABELS[event.field_key ?? '']}</Typography.Text>
        {event.field_key === 'status' ? <Select aria-label="成员状态" style={{ width: '100%' }} value={value} options={STATUS_OPTIONS} onChange={setValue} disabled={!canEdit || saving} /> : <Input.TextArea aria-label="变化内容" value={value} onChange={(e) => setValue(e.target.value)} disabled={!canEdit || saving} autoSize={{ minRows: 2, maxRows: 8 }} />}
      </>}
    </Space>
  </Modal>
}

export default function CharacterMembershipsTab({ projectId, character }: { projectId: number; character: Character }) {
  return <Memberships projectId={projectId} scope={{ kind: 'character', id: character.id }} />
}

export function OrganizationMembershipsTab({ projectId, organizationId, onChanged }: { projectId: number; organizationId: number; onChanged?: () => void }) {
  return <Memberships projectId={projectId} scope={{ kind: 'organization', id: organizationId }} onChanged={onChanged} />
}
