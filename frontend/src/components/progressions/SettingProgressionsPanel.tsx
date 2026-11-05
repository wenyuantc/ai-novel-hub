import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Divider, Empty, List, Pagination, Popconfirm, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { progressionApi } from '../../api/progressions'
import type { SettingProgression, SettingProgressionInput, SettingTextField, SettingTextFieldInput } from '../../api/types'
import { invalidateProject } from '../../utils/projectRefresh'
import EffectiveSettingPreview from './EffectiveSettingPreview'
import ProgressionEditModal from './ProgressionEditModal'
import TextFieldModal from './TextFieldModal'
import TextFieldsPanel from './TextFieldsPanel'
import { compareTextField, fieldChoices } from './fields'
import { describeAnchor } from './position'
import { type TextProgressionTarget, useProgressionPanelData } from './useProgressionPanelData'

interface Props {
  projectId: number
  target: TextProgressionTarget
  /** 只表示时间线已变化；调用方应重新比较基础值，不能直接覆盖未保存表单。 */
  onChanged?: (timelineRevision: number) => void
  /** 外层有基础表单时，由其检查未保存草稿后执行确认。 */
  onRequestConfirmBaseline?: () => void
}

type EventEditor = { event: SettingProgression | null }
type FieldEditor = { field: SettingTextField | null }

const SOURCE_LABELS: Record<SettingProgression['source'], string> = {
  manual: '手工记录', analysis: '章节分析', legacy_import: '旧资料导入',
}

function eventValue(event: SettingProgression): string {
  if (event.value === null || event.value === '') return '（空）'
  return typeof event.value === 'string' ? event.value : JSON.stringify(event.value)
}

/** 变化和自定义字段独立于外层基础表单，刷新不回填用户的基础草稿。 */
export default function SettingProgressionsPanel({ projectId, target, onChanged, onRequestConfirmBaseline }: Props) {
  const { message } = App.useApp()
  const [page, setPage] = useState(1)
  const [eventEditor, setEventEditor] = useState<EventEditor | null>(null)
  const [fieldEditor, setFieldEditor] = useState<FieldEditor | null>(null)
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)
  const panel = useProgressionPanelData(projectId, target, page)
  const { data, error, loading, refresh, refreshForComparison, canRead, canEdit, canEditNow, isCurrent } = panel
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const writable = canEdit && !busy && !loading && !error
  const kind = target.subject_kind
  const updated = (revision: number) => {
    if (!isCurrent() || !canEditNow()) return
    onChanged?.(revision)
    invalidateProject(projectId, [kind === 'world' ? 'world' : 'characters'])
  }
  const requireWritable = () => {
    if (!isCurrent() || !canEditNow()) throw new Error('权限或当前条目已变化，请重新打开')
  }

  const saveEvent = async (body: SettingProgressionInput) => {
    if (!data || !eventEditor) throw new Error('设定尚未加载，请刷新后重试')
    requireWritable()
    const result = eventEditor.event
      ? await progressionApi.updateProgression(projectId, data.timeline.id, eventEditor.event.id, body)
      : await progressionApi.createProgression(projectId, data.timeline.id, body)
    requireWritable()
    updated(result.timeline_revision)
    message.success('剧情变化已保存')
  }

  const saveField = async (body: SettingTextFieldInput) => {
    if (!data || !fieldEditor) throw new Error('设定尚未加载，请刷新后重试')
    requireWritable()
    const result = fieldEditor.field
      ? await progressionApi.updateTextField(projectId, data.timeline.id, fieldEditor.field.id, body)
      : await progressionApi.createTextField(projectId, data.timeline.id, body)
    requireWritable()
    updated(result.timeline_revision)
    message.success('自定义字段已保存')
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
      const result = await progressionApi.removeProgression(projectId, data.timeline.id, event.id, latest.timeline_revision)
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

  const fields = fieldChoices(kind, data.fields)
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
        <TextFieldsPanel fields={data.fields} canEdit={writable} busy={busy} onCreate={() => setFieldEditor({ field: null })} onEdit={(field) => setFieldEditor({ field })} onDelete={removeField} />
        <Divider style={{ margin: '4px 0' }} />
        <div>
          <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
            <Typography.Title level={5} style={{ margin: 0 }}>剧情变化</Typography.Title>
            <Space>
              <Button size="small" icon={<ReloadOutlined />} onClick={refresh} disabled={loading}>刷新</Button>
              <Button size="small" type="primary" icon={<PlusOutlined />} disabled={!writable} onClick={() => setEventEditor({ event: null })}>新增变化</Button>
            </Space>
          </Space>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>记录从哪个章节起追加或替换某个字段，不会修改其他字段。</Typography.Paragraph>
          <List
            size="small"
            dataSource={data.progressions.items}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无剧情变化" /> }}
            renderItem={(event) => {
              const selected = fields.find((field) => event.custom_field_id !== null ? field.custom_field_id === event.custom_field_id : field.field_key === event.field_key)
              return (
                <List.Item
                  actions={[
                    <Button key="edit" type="link" size="small" onClick={() => setEventEditor({ event })}>{canEdit ? '编辑' : '查看'}</Button>,
                    <Popconfirm
                      key="delete"
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
                        <Typography.Text strong>{selected?.label ?? '未知字段'}</Typography.Text>
                        <Tag>{event.operation === 'append' ? '追加' : '替换'}</Tag>
                        <Tag>{SOURCE_LABELS[event.source]}</Tag>
                        {event.orphaned && <Tag color="warning">关联章节已删除</Tag>}
                      </Space>
                    )}
                    description={(
                      <Space direction="vertical" size={0} style={{ width: '100%' }}>
                        <Typography.Text type="secondary">{describeAnchor(event, data.chapters)} · {event.boundary === 'start' ? '从本章起' : '本章结束后'}</Typography.Text>
                        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} ellipsis={{ rows: 3, expandable: true }}>{eventValue(event)}</Typography.Paragraph>
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
        <EffectiveSettingPreview projectId={projectId} timelineId={data.timeline.id} timelineRevision={data.baseline.timeline_revision} chapters={data.chapters} fields={fields} onRefresh={refresh} />
      </Space>

      {eventEditor && (
        <ProgressionEditModal
          event={eventEditor.event}
          timelineRevision={data.baseline.timeline_revision}
          fields={fields}
          chapters={data.chapters}
          canEdit={writable}
          canEditNow={canEditNow}
          onCancel={() => setEventEditor(null)}
          onSubmit={saveEvent}
          onRefresh={async (id) => {
            const result = await refreshForComparison(id)
            return { timelineRevision: result.data.baseline.timeline_revision, event: result.event }
          }}
        />
      )}
      {fieldEditor && (
        <TextFieldModal
          field={fieldEditor.field}
          timelineRevision={data.baseline.timeline_revision}
          canEdit={writable}
          canEditNow={canEditNow}
          onCancel={() => setFieldEditor(null)}
          onSubmit={saveField}
          onRefresh={async (id) => {
            const result = await refreshForComparison(null)
            return { timelineRevision: result.data.baseline.timeline_revision, field: result.data.fields.find((item) => item.id === id) ?? null }
          }}
        />
      )}
    </Spin>
  )
}
