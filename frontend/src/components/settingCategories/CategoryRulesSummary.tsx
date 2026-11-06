import { Space, Tag, Typography } from 'antd'
import { SETTING_ENTRY_TYPE_LABELS } from '../../api/settingFields'
import type { SettingCategory, SettingCategorySpec, SettingFieldDefinition } from '../../api/types'
import { categoryDraft, ruleDescription } from './categoryDraft'

interface Props { category: SettingCategory | SettingCategorySpec; definitions: readonly SettingFieldDefinition[] }

/** 规则名称来自同版本的完整字段元数据，停用身份仍可阅读。 */
export default function CategoryRulesSummary({ category, definitions }: Props) {
  return <Space direction="vertical" size={8} style={{ width: '100%', minWidth: 0, overflowWrap: 'anywhere' }}>
    <Space wrap size={[4, 8]}>{category.applicable_types.map((type) => <Tag key={type} style={{ whiteSpace: 'normal' }}>{SETTING_ENTRY_TYPE_LABELS[type]}</Tag>)}</Space>
    <Typography.Text type="secondary">{category.match_mode === 'all' ? '满足全部条件' : '满足任一条件'} · 显示顺序：{category.sort}</Typography.Text>
    <ol style={{ margin: 0, paddingInlineStart: 22 }}>{categoryDraft(category).rules.map((rule) => <li key={rule.key} style={{ marginBlock: 4 }}>{ruleDescription(rule, definitions)}</li>)}</ol>
  </Space>
}
