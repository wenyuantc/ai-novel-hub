import { PlusOutlined } from '@ant-design/icons'
import { Button, Card, Space, Tag, Typography } from 'antd'
import { SETTING_ENTRY_TYPE_LABELS, SETTING_FIELD_TYPE_LABELS } from '../../api/settingFields'
import type { SettingFieldDefinition, SettingFieldOption } from '../../api/types'

interface Props {
  definition: SettingFieldDefinition
  editable: boolean
  disabled: boolean
  onEdit: () => void
  onOption: (option: SettingFieldOption | null) => void
}

/** 配置按名称呈现，窄内容区允许标题、范围和选项自然换行。 */
export default function SettingFieldDefinitionCard({ definition: item, editable, disabled, onEdit, onOption }: Props) {
  return <Card size="small" styles={{ title: { whiteSpace: 'normal', paddingBlock: 12 } }}
    title={<Space wrap size={8}><span style={{ overflowWrap: 'anywhere' }}>{item.label}</span><Tag>{SETTING_FIELD_TYPE_LABELS[item.field_type]}</Tag>{item.archived && <Tag>已停用</Tag>}{item.ai_hidden && <Tag>对 AI 隐藏</Tag>}</Space>}>
    <Typography.Paragraph style={{ marginBottom: 8 }}>适用于：{item.applicable_types.map((type) => SETTING_ENTRY_TYPE_LABELS[type]).join('、')}</Typography.Paragraph>
    {item.field_type === 'reference' && <Typography.Paragraph style={{ marginBottom: 8 }}>可引用：{item.reference_types.map((type) => SETTING_ENTRY_TYPE_LABELS[type]).join('、')}</Typography.Paragraph>}
    <Typography.Text type="secondary">显示顺序：{item.sort}</Typography.Text>
    {item.field_type === 'dropdown' && <div style={{ marginTop: 12 }}>
      <Typography.Text type="secondary">下拉选项{editable ? '（点击名称编辑）' : ''}</Typography.Text>
      <div style={{ marginTop: 8 }}>
        {item.options.length ? <Space wrap size={[8, 8]}>{[...item.options].sort((a, b) => a.sort - b.sort || a.id - b.id).map((option) => editable
          ? <Button key={option.id} size="small" disabled={disabled} style={{ maxWidth: '100%', height: 'auto', whiteSpace: 'normal', textAlign: 'left' }} onClick={() => onOption(option)}>
            <span style={{ overflowWrap: 'anywhere' }}>{option.label}{option.archived ? '（已停用）' : ''}</span>
          </Button>
          : <Tag key={option.id} style={{ maxWidth: '100%', whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{option.label}{option.archived ? '（已停用）' : ''}</Tag>)}</Space>
          : <Typography.Text type="secondary">尚未添加选项</Typography.Text>}
      </div>
    </div>}
    {editable && <Space wrap style={{ display: 'flex', marginTop: 16 }}>
      <Button disabled={disabled} onClick={onEdit}>编辑字段</Button>
      {item.field_type === 'dropdown' && <Button icon={<PlusOutlined />} disabled={disabled || item.archived} onClick={() => onOption(null)}>添加选项</Button>}
    </Space>}
  </Card>
}
