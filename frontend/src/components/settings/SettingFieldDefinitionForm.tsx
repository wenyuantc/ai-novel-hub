import { Form, Input, InputNumber, Select, Switch, Typography } from 'antd'
import { SETTING_ENTRY_TYPE_OPTIONS, SETTING_FIELD_TYPE_LABELS } from '../../api/settingFields'
import type { SettingEntryType, SettingFieldType } from '../../api/types'
import type { DefinitionDraft, OptionDraft } from './settingFieldDraft'

interface Props { value: DefinitionDraft; disabled: boolean; onChange: (patch: Partial<DefinitionDraft>) => void }

/** 仅编辑作品字段的配置，条目起始值和章节变化分别保存。 */
export default function SettingFieldDefinitionForm({ value, disabled, onChange }: Props) {
  return <Form layout="vertical" disabled={disabled}>
    <Form.Item label="字段名称" htmlFor="setting-field-label" required>
      <Input id="setting-field-label" value={value.label} placeholder="例如：修为、所属组织、随身物品"
        onChange={(event) => onChange({ label: event.target.value })} />
    </Form.Item>
    <Form.Item label="字段类型" htmlFor="setting-field-type" required extra="四类字段都支持记录章节变化。已有值或历史的字段不能直接转换类型。">
      <Select<SettingFieldType> id="setting-field-type" value={value.field_type}
        options={Object.entries(SETTING_FIELD_TYPE_LABELS).map(([type, label]) => ({ value: type, label }))}
        onChange={(field_type) => onChange({ field_type, reference_types: field_type === 'reference' ? value.reference_types : [] })} />
    </Form.Item>
    <Form.Item label="适用类型" htmlFor="setting-field-applicable" required extra="同一字段可用于多种设定。缩小范围时，系统会保护已保存的值和历史。">
      <Select<SettingEntryType[]> id="setting-field-applicable" mode="multiple" value={value.applicable_types}
        options={SETTING_ENTRY_TYPE_OPTIONS} placeholder="选择使用这个字段的条目类型" onChange={(applicable_types) => onChange({ applicable_types })} />
    </Form.Item>
    {value.field_type === 'reference' && <Form.Item label="允许引用的目标类型" htmlFor="setting-field-reference" required>
      <Select<SettingEntryType[]> id="setting-field-reference" mode="multiple" value={value.reference_types}
        options={SETTING_ENTRY_TYPE_OPTIONS} placeholder="例如：组织、角色、世界观地点" onChange={(reference_types) => onChange({ reference_types })} />
    </Form.Item>}
    <Form.Item label="显示顺序" htmlFor="setting-field-sort" extra="数字较小的字段排在前面。">
      <InputNumber id="setting-field-sort" value={value.sort} step={1} min={-2_147_483_648} max={2_147_483_647}
        onChange={(sort) => onChange({ sort })} style={{ width: '100%' }} />
    </Form.Item>
    <Form.Item label="启用状态" extra="停用后保留已有值和章节历史。">
      <Switch checked={!value.archived} checkedChildren="启用" unCheckedChildren="停用" onChange={(enabled) => onChange({ archived: !enabled })} aria-label="启用这个字段" />
    </Form.Item>
    {value.field_type === 'dropdown' && <Typography.Paragraph type="secondary">先保存字段，再为它添加下拉选项。选项改名或停用后，原有章节记录仍保留关联。</Typography.Paragraph>}
  </Form>
}

/** 选项拥有自己的名称、顺序和停用状态，不用显示名替代其稳定身份。 */
export function SettingFieldOptionForm({ value, disabled, onChange }: { value: OptionDraft; disabled: boolean; onChange: (patch: Partial<OptionDraft>) => void }) {
  return <Form layout="vertical" disabled={disabled}>
    <Form.Item label="选项名称" htmlFor="setting-option-label" required>
      <Input id="setting-option-label" value={value.label} placeholder="例如：练气、筑基、金丹" onChange={(event) => onChange({ label: event.target.value })} />
    </Form.Item>
    <Form.Item label="显示顺序" htmlFor="setting-option-sort" extra="数字较小的选项排在前面。">
      <InputNumber id="setting-option-sort" value={value.sort} step={1} min={-2_147_483_648} max={2_147_483_647}
        onChange={(sort) => onChange({ sort })} style={{ width: '100%' }} />
    </Form.Item>
    <Form.Item label="启用状态" extra="停用后不再用于新选择；历史记录仍显示原选项。">
      <Switch checked={!value.archived} checkedChildren="启用" unCheckedChildren="停用" onChange={(enabled) => onChange({ archived: !enabled })} aria-label="启用这个选项" />
    </Form.Item>
  </Form>
}
