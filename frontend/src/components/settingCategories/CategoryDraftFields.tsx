import { PlusOutlined } from '@ant-design/icons'
import { Button, Form, Input, InputNumber, Radio, Select, Space, Typography } from 'antd'
import { useRef } from 'react'
import { SETTING_ENTRY_TYPE_OPTIONS } from '../../api/settingFields'
import type { SettingEntryType } from '../../api/types'
import type { CategoryEditor, CategorySession } from './categoryEditor'
import { moveCategoryRule, type CategoryRuleDraft } from './categoryDraft'
import CategoryRuleEditor from './CategoryRuleEditor'

interface Props { session: CategorySession; controller: CategoryEditor; disabled: boolean }

/** 没有嵌套提交表单，条件输入的Enter不会触发保存。 */
export default function CategoryDraftFields({ session, controller, disabled }: Props) {
  const serial = useRef(0)
  const draft = session.draft
  const replace = (index: number, rule: CategoryRuleDraft) => controller.edit({ rules: draft.rules.map((item, position) => position === index ? rule : item) })
  return <Form layout="vertical" component="div" style={{ minWidth: 0 }}>
    <Form.Item label="分类名称" htmlFor="category-name" required><Input id="category-name" value={draft.name} disabled={disabled} placeholder="例如：同盟成员" onChange={(event) => controller.edit({ name: event.target.value })} /></Form.Item>
    <Form.Item label="适用类型" htmlFor="category-types" required>
      <Select<SettingEntryType[]> id="category-types" mode="multiple" aria-label="分类适用类型" value={draft.applicable_types} disabled={disabled} options={SETTING_ENTRY_TYPE_OPTIONS} style={{ width: '100%' }} onChange={(applicable_types) => controller.edit({ applicable_types })} />
    </Form.Item>
    <Form.Item label="匹配方式" htmlFor="category-match-mode" required>
      <Radio.Group id="category-match-mode" aria-label="分类匹配方式" disabled={disabled} value={draft.match_mode} onChange={(event) => { const value: unknown = event.target.value; if (value === 'all' || value === 'any') controller.edit({ match_mode: value }) }}
        options={[{ label: '满足全部条件（ALL）', value: 'all' }, { label: '满足任一条件（ANY）', value: 'any' }]} />
    </Form.Item>
    <Form.Item label="显示顺序" htmlFor="category-sort"><InputNumber id="category-sort" value={draft.sort} disabled={disabled} precision={0} min={-2_147_483_648} max={2_147_483_647} onChange={(sort) => controller.edit({ sort })} /></Form.Item>
    <Space direction="vertical" size={12} style={{ width: '100%', minWidth: 0 }}>
      <Typography.Text strong>自动归类条件（{draft.rules.length}/20）</Typography.Text>
      <Typography.Text type="secondary">成员由条件自动计算。标签精确匹配；没有该下拉字段或未选值的条目不满足对应条件。</Typography.Text>
      {draft.rules.map((rule, index) => <CategoryRuleEditor key={rule.key} rule={rule} index={index} count={draft.rules.length} types={draft.applicable_types}
        definitions={session.definitions?.items ?? []} original={session.original} disabled={disabled} invalid={session.issueIndex === index}
        onChange={(value) => replace(index, value)} onMove={(direction) => controller.edit({ rules: moveCategoryRule(draft.rules, index, direction) })}
        onRemove={() => controller.edit({ rules: draft.rules.filter((_, position) => position !== index) })} />)}
      {!disabled && <Button icon={<PlusOutlined />} disabled={draft.rules.length >= 20} onClick={() => { serial.current += 1; controller.edit({ rules: [...draft.rules, { key: `added-${Date.now()}-${serial.current}`, kind: 'tag', tag: '', sort: draft.rules.length }] }) }}>添加条件</Button>}
    </Space>
  </Form>
}
