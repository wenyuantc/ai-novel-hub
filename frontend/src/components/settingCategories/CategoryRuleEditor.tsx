import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined } from '@ant-design/icons'
import { Button, Card, Form, Input, InputNumber, Select, Space, Typography } from 'antd'
import type { SettingCategory, SettingEntryType, SettingFieldDefinition } from '../../api/types'
import { retainedDropdown, type CategoryRuleDraft } from './categoryDraft'

interface Props {
  rule: CategoryRuleDraft; index: number; count: number; types: readonly SettingEntryType[]
  definitions: readonly SettingFieldDefinition[]; original: SettingCategory | null; disabled: boolean; invalid: boolean
  onChange: (rule: CategoryRuleDraft) => void; onMove: (direction: -1 | 1) => void; onRemove: () => void
}

/** 条件只选择稳定字段与选项身份；换字段后必须重新选择具体选项。 */
export default function CategoryRuleEditor({ rule, index, count, types, definitions, original, disabled, invalid, onChange, onMove, onRemove }: Props) {
  const prefix = `category-rule-${rule.key}`
  const field = rule.kind === 'dropdown' ? definitions.find((item) => item.id === rule.definition_id) : null
  const retained = rule.kind === 'dropdown' && retainedDropdown(rule, original)
  const fields = definitions.filter((item) => item.field_type === 'dropdown' && (types.some((type) => item.applicable_types.includes(type)) || item.id === field?.id))
  const fieldOptions = fields.map((item) => ({ value: item.id, label: `${item.label}${item.archived ? '（已停用）' : ''}`, disabled: item.archived && !(retained && item.id === field?.id) }))
  if (rule.kind === 'dropdown' && rule.definition_id !== null && !field) fieldOptions.push({ value: rule.definition_id, label: '原字段不可用，请读取最新对照', disabled: true })
  const options = (field?.options ?? []).map((item) => ({ value: item.id, label: `${item.label}${item.archived ? '（已停用）' : ''}`, disabled: (field?.archived || item.archived) && !(retained && rule.kind === 'dropdown' && rule.option_id === item.id) }))
  return <Card size="small" style={{ minWidth: 0 }}>
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap style={{ width: '100%', justifyContent: 'space-between', gap: 12 }}>
        <Typography.Text strong>条件 {index + 1}</Typography.Text>
        {!disabled && <Space wrap size={8}>
          <Button size="small" icon={<ArrowUpOutlined />} aria-label={`上移条件 ${index + 1}`} disabled={index === 0} onClick={() => onMove(-1)} />
          <Button size="small" icon={<ArrowDownOutlined />} aria-label={`下移条件 ${index + 1}`} disabled={index === count - 1} onClick={() => onMove(1)} />
          <Button size="small" danger icon={<DeleteOutlined />} aria-label={`删除条件 ${index + 1}`} disabled={count === 1} onClick={onRemove}>删除</Button>
        </Space>}
      </Space>
      <Form.Item label="条件类型" htmlFor={`${prefix}-kind`} style={{ marginBottom: 0 }}>
        <Select id={`${prefix}-kind`} aria-label={`条件 ${index + 1} 类型`} disabled={disabled} value={rule.kind} style={{ width: '100%' }}
          options={[{ value: 'tag', label: '整理标签包含' }, { value: 'dropdown', label: '下拉字段等于' }]}
          onChange={(kind: 'tag' | 'dropdown') => onChange(kind === 'tag' ? { key: rule.key, sort: rule.sort, kind, tag: '' } : { key: rule.key, sort: rule.sort, kind, definition_id: null, option_id: null })} />
      </Form.Item>
      {rule.kind === 'tag' ? <Form.Item label="整理标签" htmlFor={`${prefix}-tag`} validateStatus={invalid ? 'error' : undefined} style={{ marginBottom: 0 }}>
        <Input id={`${prefix}-tag`} aria-label={`条件 ${index + 1} 整理标签`} placeholder="填写完整标签，区分大小写" value={rule.tag} disabled={disabled} onChange={(event) => onChange({ ...rule, tag: event.target.value })} />
      </Form.Item> : <>
        <Form.Item label="下拉字段" htmlFor={`${prefix}-field`} validateStatus={invalid ? 'error' : undefined} style={{ marginBottom: 0 }}>
          <Select id={`${prefix}-field`} aria-label={`条件 ${index + 1} 下拉字段`} showSearch optionFilterProp="label" placeholder="选择适用的下拉字段" value={rule.definition_id ?? undefined}
            disabled={disabled} options={fieldOptions} style={{ width: '100%' }} notFoundContent="没有可用下拉字段，可先在设定字段中创建。"
            onChange={(definition_id: number) => onChange({ ...rule, definition_id, option_id: null })} />
        </Form.Item>
        <Form.Item label="具体选项" htmlFor={`${prefix}-option`} validateStatus={invalid ? 'error' : undefined} style={{ marginBottom: 0 }}>
          <Select id={`${prefix}-option`} aria-label={`条件 ${index + 1} 具体选项`} showSearch optionFilterProp="label" placeholder="选择具体选项" value={options.some((item) => item.value === rule.option_id) ? rule.option_id : undefined}
            disabled={disabled || !field} options={options} style={{ width: '100%' }} onChange={(option_id: number) => onChange({ ...rule, option_id })} />
        </Form.Item>
        {retained && (field?.archived || field?.options.some((item) => item.id === rule.option_id && item.archived)) && <Typography.Text type="secondary">这是原分类保留的停用条件；新条件只能选择启用的字段和选项。</Typography.Text>}
      </>}
      <Form.Item label="条件顺序" htmlFor={`${prefix}-sort`} style={{ marginBottom: 0 }}>
        <InputNumber id={`${prefix}-sort`} aria-label={`条件 ${index + 1} 顺序`} value={rule.sort} disabled={disabled} precision={0} min={-2_147_483_648} max={2_147_483_647} onChange={(sort) => { if (sort !== null) onChange({ ...rule, sort }) }} />
      </Form.Item>
    </Space>
  </Card>
}
