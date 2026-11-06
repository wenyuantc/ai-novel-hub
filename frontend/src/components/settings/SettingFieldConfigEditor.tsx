import { DeleteOutlined, SaveOutlined } from '@ant-design/icons'
import { Alert, Button, Descriptions, Divider, Drawer, Popconfirm, Space, Typography } from 'antd'
import { SETTING_ENTRY_TYPE_LABELS, SETTING_FIELD_TYPE_LABELS } from '../../api/settingFields'
import type { SettingFieldDefinition, SettingFieldOption } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import SettingFieldDefinitionForm, { SettingFieldOptionForm } from './SettingFieldDefinitionForm'
import { configurationDirty, type SettingFieldsController, type SettingFieldsState } from './settingFieldsController'

interface Props { projectId: number; state: SettingFieldsState; controller: SettingFieldsController }

function ConfigurationSummary({ title, item }: { title: string; item: SettingFieldDefinition | SettingFieldOption | null }) {
  return <section aria-label={title}>
    <Typography.Title level={5}>{title}</Typography.Title>
    {!item ? <Typography.Text type="secondary">尚未创建</Typography.Text> : <Descriptions size="small" column={1}>
      <Descriptions.Item label="名称">{item.label}</Descriptions.Item>
      {'field_type' in item && <>
        <Descriptions.Item label="类型">{SETTING_FIELD_TYPE_LABELS[item.field_type]}</Descriptions.Item>
        <Descriptions.Item label="适用类型">{item.applicable_types.map((type) => SETTING_ENTRY_TYPE_LABELS[type]).join('、')}</Descriptions.Item>
        {item.field_type === 'reference' && <Descriptions.Item label="可引用">{item.reference_types.map((type) => SETTING_ENTRY_TYPE_LABELS[type]).join('、')}</Descriptions.Item>}
        {item.field_type === 'dropdown' && <Descriptions.Item label="选项">{item.options.map((option) => `${option.label}${option.archived ? '（已停用）' : ''}`).join('、') || '尚无选项'}</Descriptions.Item>}
      </>}
      <Descriptions.Item label="顺序">{item.sort}</Descriptions.Item>
      <Descriptions.Item label="状态">{item.archived ? '已停用' : '启用中'}</Descriptions.Item>
    </Descriptions>}
  </section>
}

/** 关闭只收起编辑器；原配置、冲突与草稿由页级controller继续持有。 */
export default function SettingFieldConfigEditor({ projectId, state, controller }: Props) {
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const mobile = useIsMobile()
  const editor = state.editor
  if (!editor) return null
  const busy = state.saving || state.reviewing || state.loading
  const locked = !canEdit || busy
  const title = editor.kind === 'definition' ? editor.original ? '编辑字段定义' : '新建字段定义'
    : editor.original ? '编辑下拉选项' : '添加下拉选项'
  const reviewedItem = editor.kind === 'definition' ? editor.reviewed?.item ?? null
    : editor.reviewed?.item.options.find((item) => item.id === editor.original?.id) ?? null
  const saveLocked = locked || editor.reviewRequired
  return <Drawer title={title} open={editor.open} width={mobile ? '100%' : 600} onClose={controller.closeEditor}
    maskClosable={!busy} closable={!busy} keyboard={!busy} footer={<Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
      <Button disabled={busy} onClick={controller.closeEditor}>收起并保留草稿</Button>
      {canEdit && <Button type="primary" icon={<SaveOutlined />} loading={state.saving} disabled={saveLocked || !configurationDirty(editor)}
        onClick={() => { if (canEditNow()) void controller.save() }}>保存{editor.kind === 'definition' ? '字段' : '选项'}</Button>}
    </Space>}>
    {state.notice && <Alert type="info" showIcon message={state.notice} style={{ marginBottom: 16 }} />}
    {state.error && <Alert type="error" showIcon message={state.error} description="当前草稿仍保留。" style={{ marginBottom: 16 }} />}
    {editor.reviewRequired && <Alert type="warning" showIcon message="请先核对最新字段配置"
      description="草稿和原配置仍保留。刷新列表不会替换保存版本，当前不能直接覆盖其他修改。"
      action={<Button size="small" loading={state.reviewing} disabled={busy} onClick={() => { void controller.review() }}>查看最新配置</Button>}
      style={{ marginBottom: 16 }} />}
    {!canEdit && <Alert type="info" showIcon message="当前为只读，未保存草稿仍保留" style={{ marginBottom: 16 }} />}
    {editor.kind === 'definition' ? <SettingFieldDefinitionForm value={editor.draft} disabled={locked} onChange={controller.editDefinition} />
      : <><Typography.Paragraph type="secondary">所属字段：{editor.definition.label}</Typography.Paragraph>
        <SettingFieldOptionForm value={editor.draft} disabled={locked} onChange={controller.editOption} /></>}
    {editor.reviewedRevision !== null && <>
      <Divider />
      <ConfigurationSummary title="开始编辑时的配置" item={editor.original} />
      <ConfigurationSummary title="最新保存的配置" item={reviewedItem} />
      <Popconfirm title="放弃草稿，采用最新配置？" description="当前未保存的修改将被替换。" okText="放弃草稿并载入" cancelText="保留草稿"
        disabled={locked} onConfirm={() => { if (canEditNow()) controller.useReviewed() }}>
        <Button disabled={locked} style={{ marginTop: 16 }}>放弃草稿，编辑最新配置</Button>
      </Popconfirm>
    </>}
    <Divider />
    <Space wrap>
      <Popconfirm title="放弃当前配置草稿？" okText="放弃草稿" cancelText="继续保留" disabled={busy}
        onConfirm={controller.discardEditor}>
        <Button disabled={busy}>放弃草稿</Button>
      </Popconfirm>
      {canEdit && editor.original && <Popconfirm title={`删除「${editor.original.label}」？`}
        description="仅未被使用的配置可删除。有已保存值或历史时，请使用停用。" okText="删除" cancelText="取消"
        okButtonProps={{ danger: true }} disabled={saveLocked} onConfirm={() => { if (canEditNow()) void controller.remove() }}>
        <Button danger icon={<DeleteOutlined />} disabled={saveLocked}>删除{editor.kind === 'definition' ? '字段' : '选项'}</Button>
      </Popconfirm>}
    </Space>
  </Drawer>
}
