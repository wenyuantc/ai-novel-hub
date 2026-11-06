import { Button, Card, Space, Tag, Typography } from 'antd'
import { SETTING_ENTRY_TYPE_LABELS } from '../../api/settingFields'
import { splitPane, splitRow } from '../../utils/layout'
import { ruleDescription } from './categoryDraft'
import type { CategoryEditor, CategorySession } from './categoryEditor'
import CategoryRulesSummary from './CategoryRulesSummary'

interface Props { session: CategorySession; controller: CategoryEditor; editable: boolean }

/** 对照不更新保存依据；作者明确采用后再重新校验草稿。 */
export default function CategoryReview({ session, controller, editable }: Props) {
  if (!session.review) return null
  return <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
    <Typography.Text>最新配置已读取。请核对名称、类型和条件，再明确采用保存依据；此操作不会自动保存。</Typography.Text>
    <div style={splitRow}>
      <Card size="small" title="打开时的分类" style={splitPane(230)}>
        {session.original ? <><Typography.Paragraph strong style={{ overflowWrap: 'anywhere' }}>{session.original.name}</Typography.Paragraph><CategoryRulesSummary category={session.original} definitions={session.definitions?.items ?? []} /></> : '当时是新建分类草稿。'}
      </Card>
      <Card size="small" title="当前草稿" style={splitPane(230)}>
        <Typography.Paragraph strong style={{ overflowWrap: 'anywhere' }}>{session.draft.name || '尚未命名'}</Typography.Paragraph>
        <Space wrap size={[4, 8]}>{session.draft.applicable_types.map((type) => <Tag key={type}>{SETTING_ENTRY_TYPE_LABELS[type]}</Tag>)}</Space>
        <Typography.Paragraph type="secondary">{session.draft.match_mode === 'all' ? '满足全部条件' : '满足任一条件'} · 显示顺序：{session.draft.sort ?? '待填写'}</Typography.Paragraph>
        <ol style={{ paddingInlineStart: 20, overflowWrap: 'anywhere' }}>{session.draft.rules.map((rule) => <li key={rule.key}>{ruleDescription(rule, session.definitions?.items ?? [])}</li>)}</ol>
      </Card>
      <Card size="small" title="最新分类" style={splitPane(230)}>
        {session.review.original ? <><Typography.Paragraph strong style={{ overflowWrap: 'anywhere' }}>{session.review.original.name}</Typography.Paragraph><CategoryRulesSummary category={session.review.original} definitions={session.review.definitions.items} /></> : '尚未创建此分类，将按当前配置校验新草稿。'}
      </Card>
    </div>
    <Space wrap size={[12, 12]}>
      {editable && <Button type="primary" onClick={() => controller.useReview(true)}>确认保留草稿，采用最新保存依据</Button>}
      <Button onClick={() => controller.useReview(false)}>{editable ? '放弃草稿，改用最新分类' : '采用最新分类查看'}</Button>
    </Space>
  </Space>
}
