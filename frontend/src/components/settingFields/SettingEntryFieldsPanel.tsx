import { Alert, App, Button, Divider, Empty, List, Space, Spin, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { settingFieldsApi } from '../../api/settingFields'
import type { ProgressionPositionInput, SettingEntryRef, SettingFieldSlot, SettingProgressionInput } from '../../api/types'
import { useAuthStore } from '../../stores/auth'
import { invalidateProject } from '../../utils/projectRefresh'
import ProgressionEditModal from '../progressions/ProgressionEditModal'
import SettingProgressionsPanel from '../progressions/SettingProgressionsPanel'
import { fieldChoices, sharedProgressionCommand, type ProgressionFieldChoice } from '../progressions/fields'
import SettingFieldBaselineModal from './SettingFieldBaselineModal'
import SettingFieldValueView from './SettingFieldValueView'
import { useEntryFields } from './useEntryFields'
import { entryKey, entryProgressionTarget, fieldSlotValue } from './valueDraft'

interface Props {
  projectId: number; entry: SettingEntryRef; onChanged?: (revision: number) => void; onRequestConfirmBaseline?: () => void
  initialPosition?: ProgressionPositionInput | null
}

/** 四类实体共用独立字段区，身份或主体改变时旧草稿和回调失效。 */
export default function SettingEntryFieldsPanel(props: Props) {
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}`)
  return <EntryFieldsInner key={`${identity}:${props.projectId}:${entryKey(props.entry)}`} {...props} />
}

function EntryFieldsInner({ projectId, entry, onChanged, onRequestConfirmBaseline, initialPosition }: Props) {
  const { message } = App.useApp()
  const panel = useEntryFields(projectId, entry)
  const { data, chapters, error, loading, canRead, canEdit } = panel
  const [baselineEditors, setBaselineEditors] = useState<SettingFieldSlot[]>([])
  const [activeBaseline, setActiveBaseline] = useState<number | null>(null)
  const [eventEditors, setEventEditors] = useState<SettingFieldSlot[]>([])
  const [activeEvent, setActiveEvent] = useState<number | null>(null)
  const canEditNow = () => panel.isCurrent() && panel.canEditNow()
  const changed = (revision: number) => {
    if (!canEditNow()) return
    onChanged?.(revision)
    invalidateProject(projectId, ['setting_fields', entry.kind === 'world' ? 'world' : entry.kind === 'character' ? 'characters' : entry.kind === 'organization' ? 'organizations' : 'careers'])
  }
  const saveEvent = async (body: SettingProgressionInput, field: ProgressionFieldChoice) => {
    const slot = field.slot
    if (!slot) throw new Error('请选择共享字段')
    const command = sharedProgressionCommand(body, slot)
    const result = await panel.run((signal) => settingFieldsApi.createProgression(projectId, entry, command.definitionId, command.body, signal))
    changed(result.timeline_revision); message.success('剧情变化已保存')
    setEventEditors((old) => old.filter((item) => item.definition_id !== activeEvent)); setActiveEvent(null)
  }
  if (!canRead) return null
  if (!data) return error ? <Alert type="error" showIcon message={error} action={<Button onClick={panel.refresh}>重试</Button>} /> : <Spin aria-label="正在读取条目字段" />
  const writable = canEdit && !loading && !error
  const target = entryProgressionTarget(entry)
  const choices = fieldChoices(target.subject_kind, [], data.fields)
  const openBaseline = (slot: SettingFieldSlot) => {
    if (!panel.isCurrent()) return
    setBaselineEditors((old) => old.some((item) => item.definition_id === slot.definition_id) ? old : [...old, slot]); setActiveBaseline(slot.definition_id)
  }
  const openEvent = (slot: SettingFieldSlot) => {
    if (!canEditNow()) return
    setEventEditors((old) => old.some((item) => item.definition_id === slot.definition_id) ? old : [...old, slot]); setActiveEvent(slot.definition_id)
  }
  return <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
    <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}><Typography.Title level={5} style={{ margin: 0 }}>条目字段</Typography.Title><Link to={`/projects/${projectId}/setting-fields?type=${encodeURIComponent(data.entry.type)}`}>配置此类型字段</Link></Space>
    <Typography.Text type="secondary">起始值与剧情变化独立保存。当前有效值包括全部有效的计划章节变化，名称使用当前命名。</Typography.Text>
    {!canEdit && <Alert type="info" showIcon message="当前为只读，可查看字段、历史与按章有效值。" />}
    {error && <Alert type="error" showIcon message={error} description="保留上次快照及未保存草稿，重新读取后再修改。" action={<Button onClick={panel.refresh}>重试</Button>} />}
    {baselineEditors.length > 0 && activeBaseline === null && <Typography.Text type="secondary">本条目有已保留的起始值草稿，点击对应字段可继续。</Typography.Text>}
    <Spin spinning={loading}><List dataSource={data.fields} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该类型还没有适用的共享字段" /> }} renderItem={(slot) => <List.Item>
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        <Space wrap><Typography.Text strong>{slot.label}</Typography.Text><Tag>共享字段</Tag>{slot.archived && <Tag>已停用</Tag>}{slot.ai_hidden && <Tag>对 AI 隐藏</Tag>}</Space>
        <div><Typography.Text type="secondary">故事开始时：</Typography.Text><SettingFieldValueView projectId={projectId} value={fieldSlotValue(slot, true)} options={slot.options} reference={slot.base_reference} /></div>
        <div><Typography.Text type="secondary">当前有效值：</Typography.Text><SettingFieldValueView projectId={projectId} value={fieldSlotValue(slot)} options={slot.options} reference={slot.reference} /></div>
        <Space wrap><Button size="small" onClick={() => openBaseline(slot)} disabled={loading || !!error}>{canEdit ? '编辑起始值' : '查看起始值'}</Button><Button size="small" onClick={() => openEvent(slot)} disabled={!writable || slot.archived}>记录章节变化</Button></Space>
      </Space>
    </List.Item>} /></Spin>
    <Divider style={{ margin: 0 }} />
    {data.timeline_id !== null ? <SettingProgressionsPanel projectId={projectId} target={target} initialPosition={initialPosition} onChanged={onChanged} onRequestConfirmBaseline={onRequestConfirmBaseline} /> : <Typography.Text type="secondary">暂无变化。首次填写起始值或记录章节变化时会一起保存字段身份。</Typography.Text>}
    {baselineEditors.map((slot) => <SettingFieldBaselineModal key={`baseline:${slot.definition_id}`} open={activeBaseline === slot.definition_id} projectId={projectId} slot={slot} snapshot={data} canEdit={writable} canEditNow={canEditNow} onCancel={() => setActiveBaseline(null)} onRefresh={panel.readLatest} onSubmit={async (value, expected) => {
      if (slot.definition_id === null) throw new Error('请选择共享字段')
      const definitionId = slot.definition_id
      const result = await panel.run((signal) => settingFieldsApi.updateBaseline(projectId, entry, definitionId, { ...value, expected_fields_revision: expected.fields_revision, expected_timeline_revision: expected.timeline_revision }, signal))
      changed(result.timeline_revision); message.success('起始值已保存')
      setBaselineEditors((old) => old.filter((item) => item.definition_id !== slot.definition_id)); setActiveBaseline(null)
    }} />)}
    {eventEditors.map((slot) => <ProgressionEditModal key={`event:${slot.definition_id}`} projectId={projectId} open={activeEvent === slot.definition_id} event={null} initialField={`definition:${slot.definition_id}`} fields={choices} chapters={chapters} timelineRevision={data.timeline_revision} fieldsRevision={data.fields_revision} canEdit={writable} canEditNow={canEditNow} onCancel={() => setActiveEvent(null)} onSubmit={saveEvent} onRefresh={async () => { const current = await panel.readLatest(); return { timelineRevision: current.timeline_revision, fieldsRevision: current.fields_revision, event: null } }} />)}
  </Space>
}
