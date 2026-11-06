import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Divider, Empty, List, Pagination, Popconfirm, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { progressionApi } from '../../api/progressions'
import { settingFieldsApi } from '../../api/settingFields'
import type { ProgressionPositionInput, SettingProgression, SettingProgressionInput, SettingTextField, SettingTextFieldInput } from '../../api/types'
import { invalidateProject } from '../../utils/projectRefresh'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import EffectiveSettingPreview from './EffectiveSettingPreview'
import ProgressionEditModal from './ProgressionEditModal'
import TextFieldModal from './TextFieldModal'
import TextFieldsPanel from './TextFieldsPanel'
import { compareTextField, fieldChoices, progressionField, progressionReference, progressionValueLabel, progressionUpdateInput, sharedProgressionCommand, type ProgressionFieldChoice } from './fields'
import { fieldValueFromRaw, subjectEntry } from '../settingFields/valueDraft'
import LegacyFieldBindingModal from '../settingFields/LegacyFieldBindingModal'
import LegacyFieldVisibilityModal from '../settingFields/LegacyFieldVisibilityModal'
import SettingFieldValueView from '../settingFields/SettingFieldValueView'
import { describeAnchor } from './position'
import { type TextProgressionTarget, useProgressionPanelData } from './useProgressionPanelData'

interface Props {
  projectId: number
  target: TextProgressionTarget
  /** 只表示时间线已变化；调用方应重新比较基础值，不能直接覆盖未保存表单。 */
  onChanged?: (timelineRevision: number) => void
  /** 外层有基础表单时，由其检查未保存草稿后执行确认。 */
  onRequestConfirmBaseline?: () => void
  initialPosition?: ProgressionPositionInput | null
}

type EventEditor = { event: SettingProgression | null }
type FieldEditor = { field: SettingTextField | null }

const SOURCE_LABELS: Record<SettingProgression['source'], string> = {
  manual: '手工记录', analysis: '章节分析', legacy_import: '旧资料导入',
}

/** 变化和自定义字段独立于外层基础表单，刷新不回填用户的基础草稿。 */
export default function SettingProgressionsPanel({ projectId, target, onChanged, onRequestConfirmBaseline, initialPosition }: Props) {
  const { message } = App.useApp()
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const [page, setPage] = useState(1)
  const [eventEditors, setEventEditors] = useState<EventEditor[]>([])
  const [activeEvent, setActiveEvent] = useState<number | 'new' | null>(null)
  const [fieldEditors, setFieldEditors] = useState<FieldEditor[]>([])
  const [activeField, setActiveField] = useState<number | 'new' | null>(null)
  const [binding, setBinding] = useState<SettingTextField | null>(null)
  const [visibilityField, setVisibilityField] = useState<SettingTextField | null>(null)
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)
  const panel = useProgressionPanelData(projectId, target, page)
  const { data, error, loading, refresh, refreshForComparison, canRead, canEdit, canEditNow, isCurrent } = panel
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const writable = canEdit && !busy && !loading && !error
  const kind = target.subject_kind
  const updated = (revision: number, visibilityChanged = false) => {
    if (!isCurrent() || !canEditNow()) return
    onChanged?.(revision)
    invalidateProject(projectId, ['setting_fields', kind === 'world' ? 'world' : kind === 'organization' ? 'organizations' : kind === 'career_entry' ? 'careers' : 'characters',
      ...(visibilityChanged ? ['setting-ai-visibility' as const] : [])])
  }
  const requireWritable = () => {
    if (!isCurrent() || !canEditNow()) throw new Error('权限或当前条目已变化，请重新打开')
  }
  const editEvent = (event: SettingProgression | null) => {
    if (!isCurrent()) return
    const id = event?.id ?? 'new'
    setEventEditors((old) => old.some((editor) => (editor.event?.id ?? 'new') === id) ? old : [...old, { event }])
    setActiveEvent(id)
  }
  const editField = (field: SettingTextField | null) => {
    if (!isCurrent()) return
    const id = field?.id ?? 'new'
    setFieldEditors((old) => old.some((editor) => (editor.field?.id ?? 'new') === id) ? old : [...old, { field }])
    setActiveField(id)
  }

  const saveEvent = async (body: SettingProgressionInput, selected: ProgressionFieldChoice, comparedEvent: SettingProgression | null) => {
    if (!data) throw new Error('设定尚未加载，请刷新后重试')
    requireWritable()
    let result
    if (!comparedEvent && selected.slot) {
      const command = sharedProgressionCommand(body, selected.slot)
      result = await settingFieldsApi.createProgression(projectId, data.entryFields.entry.reference, command.definitionId, command.body)
    } else if (comparedEvent) {
      result = await progressionApi.updateProgression(projectId, data.timeline.id, comparedEvent.id, progressionUpdateInput(body, comparedEvent, selected))
    } else result = await progressionApi.createProgression(projectId, data.timeline.id, body)
    requireWritable()
    updated(result.timeline_revision)
    message.success('剧情变化已保存')
    setEventEditors((old) => old.filter((editor) => (editor.event?.id ?? 'new') !== (comparedEvent?.id ?? 'new')))
    setActiveEvent(null)
  }

  const saveField = async (body: SettingTextFieldInput, field: SettingTextField | null) => {
    if (!data) throw new Error('设定尚未加载，请刷新后重试')
    requireWritable()
    const result = field
      ? await progressionApi.updateTextField(projectId, data.timeline.id, field.id, body)
      : await progressionApi.createTextField(projectId, data.timeline.id, body)
    requireWritable()
    updated(result.timeline_revision)
    message.success('自定义字段已保存')
    setFieldEditors((old) => old.filter((editor) => (editor.field?.id ?? 'new') !== (field?.id ?? 'new')))
    setActiveField(null)
  }

  const removeEvent = async (event: SettingProgression) => {
    if (!data || !writable || !canEditNow() || !isCurrent()) return
    setBusy(true)
    try {
      const latest = await progressionApi.getProgression(projectId, data.timeline.id, event.id)
      requireWritable()
      if (!latest.item.active || latest.item.revision !== event.revision || latest.timeline_revision !== data.baseline.timeline_revision) {
        throw new Error('变化已更新，请刷新并重新核对后再删除')
      }
      if (event.definition_id != null && latest.fields_revision !== data.entryFields.fields_revision) throw new Error('字段配置已更新，请刷新并核对后删除')
      const result = await progressionApi.removeProgression(projectId, data.timeline.id, event.id, latest.timeline_revision, event.definition_id != null ? data.entryFields.fields_revision : undefined)
      requireWritable()
      if (page > 1 && data.progressions.items.length === 1) setPage(page - 1)
      updated(result.timeline_revision)
      message.success('剧情变化已删除')
    } catch (err) {
      if (isCurrent() && canEditNow()) message.error(errorMessage(err))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const removeField = async (field: SettingTextField) => {
    if (!data || !writable || !canEditNow() || !isCurrent()) return
    setBusy(true)
    try {
      const latest = await progressionApi.listTextFields(projectId, data.timeline.id)
      requireWritable()
      const currentField = latest.items.find((item) => item.id === field.id) ?? null
      if (latest.timeline_revision !== data.baseline.timeline_revision || compareTextField(field, currentField) !== 'unchanged') {
        throw new Error('字段已更新，请刷新并重新核对后再删除')
      }
      const result = await progressionApi.removeTextField(projectId, data.timeline.id, field.id, latest.timeline_revision)
      requireWritable()
      updated(result.timeline_revision)
      message.success('自定义字段已删除')
    } catch (err) {
      if (isCurrent() && canEditNow()) message.error(errorMessage(err))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  const confirmBaseline = async () => {
    if (!data || !writable || !canEditNow() || !isCurrent()) return
    if (onRequestConfirmBaseline) { onRequestConfirmBaseline(); return }
    setBusy(true)
    try {
      const result = await progressionApi.updateBaseline(projectId, data.timeline.id, {
        expected_revision: data.baseline.timeline_revision, changes: {}, confirm: true,
      })
      requireWritable()
      updated(result.timeline_revision)
      message.success('基础设定已确认')
    } catch (err) {
      if (isCurrent() && canEditNow()) message.error(errorMessage(err))
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  if (!canRead) return null
  if (!data) return error ? (
    <Alert type="error" showIcon message={error} action={<Button size="small" onClick={refresh}>重试</Button>} />
  ) : <Spin aria-label="正在读取剧情变化" />

  const fields = fieldChoices(kind, data.fields, data.entryFields.fields)
  const rowScope = `${identity}:${data.baseline.timeline_revision}:${data.entryFields.fields_revision}:${loading}`
  return (
    <Spin spinning={loading}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {error && <Alert type="error" showIcon message={error} description="当前显示上次读取的内容，请刷新后再修改。" action={<Button size="small" onClick={refresh}>刷新</Button>} />}
        {data.baseline.baseline_status === 'legacy_unverified' && (
          <Alert
            type="warning"
            showIcon
            message="旧资料待校准"
            description="当前档案已保留为基础设定；过去未记录的变化无法自动还原。请检查本条目的全部基础字段，并按需补录剧情变化。"
            action={(
              <Popconfirm
                key={`baseline:${rowScope}`}
                title="确认本条目的基础设定已校准？"
                description="请先检查所有基础字段；新增的剧情变化不会自动确认旧资料。"
                okText="确认已校准"
                cancelText="稍后"
                okButtonProps={{ disabled: !writable }}
                onConfirm={confirmBaseline}
              >
                <Button size="small" disabled={!writable}>确认已校准</Button>
              </Popconfirm>
            )}
          />
        )}
        <TextFieldsPanel key={rowScope} fields={data.fields} canEdit={writable} busy={busy} onCreate={() => editField(null)} onEdit={editField} onDelete={removeField} onBind={setBinding}
          hiddenFieldIds={new Set(data.entryFields.legacy_fields.flatMap((slot) => slot.ai_hidden && slot.field_id !== null ? [slot.field_id] : []))}
          onVisibility={(field) => { if (isCurrent() && canEditNow()) setVisibilityField(field) }} />
        <Divider style={{ margin: '4px 0' }} />
        <div>
          <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
            <Typography.Title level={5} style={{ margin: 0 }}>剧情变化</Typography.Title>
            <Space>
              <Button size="small" icon={<ReloadOutlined />} onClick={refresh} disabled={loading}>刷新</Button>
              <Button size="small" type="primary" icon={<PlusOutlined />} disabled={!writable || fields.every((field) => field.slot?.archived)} onClick={() => editEvent(null)}>新增变化</Button>
            </Space>
          </Space>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>记录从哪个章节起追加或替换某个字段，不会修改其他字段。</Typography.Paragraph>
          <List
            rowKey="id"
            size="small"
            dataSource={data.progressions.items}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无剧情变化" /> }}
            renderItem={(event) => {
              const selected = progressionField(event, fields)
              const typed = selected?.slot ? fieldValueFromRaw(selected.slot.field_type, event.value) : null
              return (
                <List.Item
                  actions={[
                    <Button key="edit" type="link" size="small" onClick={() => editEvent(event)}>{canEdit ? '编辑' : '查看'}</Button>,
                    <Popconfirm
                      key={`delete:${rowScope}:${event.id}:${event.revision}`}
                      title="删除这条剧情变化？"
                      description="删除后会按剩余变化重新计算各章节设定。"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true, disabled: !writable }}
                      onConfirm={() => removeEvent(event)}
                    >
                      <Button type="link" size="small" danger disabled={!writable}>删除</Button>
                    </Popconfirm>,
                  ]}
                >
                  <List.Item.Meta
                    title={(
                      <Space wrap size={[4, 4]}>
                        <Typography.Text strong>{selected?.label ?? event.field_label ?? '字段暂不可用'}</Typography.Text>
                        <Tag>{event.operation === 'append' ? '追加' : '替换'}</Tag>
                        <Tag>{SOURCE_LABELS[event.source]}</Tag>
                        {event.orphaned && <Tag color="warning">关联章节已删除</Tag>}
                      </Space>
                    )}
                    description={(
                      <Space direction="vertical" size={0} style={{ width: '100%' }}>
                        <Typography.Text type="secondary">{describeAnchor(event, data.chapters)} · {event.boundary === 'start' ? '从本章起' : '本章结束后'}</Typography.Text>
                        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} ellipsis={{ rows: 3, expandable: true }}>{typed ? <SettingFieldValueView projectId={projectId} value={typed} options={selected?.slot?.options} reference={progressionReference(event)} /> : progressionValueLabel(event, selected)}</Typography.Paragraph>
                        {event.note && <Typography.Text type="secondary">备注：{event.note}</Typography.Text>}
                      </Space>
                    )}
                  />
                </List.Item>
              )
            }}
          />
          {data.progressions.total > panel.pageSize && <Pagination size="small" current={page} pageSize={panel.pageSize} total={data.progressions.total} onChange={setPage} showSizeChanger={false} />}
        </div>
        <Divider style={{ margin: '4px 0' }} />
        <EffectiveSettingPreview key={JSON.stringify(initialPosition)} projectId={projectId} entry={data.entryFields.entry.reference} initialPosition={initialPosition} fieldsRevision={data.entryFields.fields_revision} timelineId={data.timeline.id} timelineRevision={data.baseline.timeline_revision} chapters={data.chapters} fields={fields} onRefresh={refresh} />
      </Space>

      {eventEditors.map((eventEditor) => (
        <ProgressionEditModal
          key={eventEditor.event?.id ?? 'new'}
          open={activeEvent === (eventEditor.event?.id ?? 'new')}
          projectId={projectId}
          event={eventEditor.event}
          timelineRevision={data.baseline.timeline_revision}
          fieldsRevision={data.entryFields.fields_revision}
          fields={fields}
          chapters={data.chapters}
          canEdit={writable}
          canEditNow={() => isCurrent() && canEditNow()}
          onCancel={() => setActiveEvent(null)}
          onSubmit={saveEvent}
          onRefresh={async (id) => {
            const result = await refreshForComparison(id)
            return { timelineRevision: result.data.baseline.timeline_revision, fieldsRevision: result.data.entryFields.fields_revision, event: result.event }
          }}
        />
      ))}
      {binding && subjectEntry(target) && <LegacyFieldBindingModal projectId={projectId} field={binding} snapshot={data.entryFields} canEdit={writable} canEditNow={() => isCurrent() && canEditNow()} onCancel={() => setBinding(null)} onSaved={(revision) => { updated(revision, true); setBinding(null) }} onRefresh={async () => (await refreshForComparison(null)).data.entryFields} />}
      {visibilityField && <LegacyFieldVisibilityModal key={`${projectId}:${data.timeline.id}:${visibilityField.id}`} projectId={projectId} field={visibilityField} snapshot={data.entryFields}
        canEdit={writable} canEditNow={() => isCurrent() && canEditNow()} onCancel={() => setVisibilityField(null)}
        onSaved={(revision) => { updated(revision, true); setVisibilityField(null); message.success('字段 AI 可见性已保存') }}
        onRefresh={async () => (await refreshForComparison(null)).data.entryFields} />}
      {fieldEditors.map((fieldEditor) => (
        <TextFieldModal
          key={fieldEditor.field?.id ?? 'new'}
          open={activeField === (fieldEditor.field?.id ?? 'new')}
          field={fieldEditor.field}
          timelineRevision={data.baseline.timeline_revision}
          canEdit={writable}
          canEditNow={() => isCurrent() && canEditNow()}
          onCancel={() => setActiveField(null)}
          onSubmit={(body) => saveField(body, fieldEditor.field)}
          onRefresh={async (id) => {
            const result = await refreshForComparison(null)
            return { timelineRevision: result.data.baseline.timeline_revision, field: result.data.fields.find((item) => item.id === id) ?? null }
          }}
        />
      ))}
    </Spin>
  )
}
