import { Checkbox, Empty, Input, InputNumber, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { EditableSuggestion } from '../../api/entityTasks'
import { categoryLabel } from '../../api/relationships'

interface Props {
  /** 主体（新角色 / 被解析的角色）名字，用于「对方是 X 的 …」文案 */
  subjectName: string
  value: EditableSuggestion[]
  onChange: (next: EditableSuggestion[]) => void
  disabled?: boolean
}

/** 关系建议复选列表：可改称呼 / 亲密度 / 描述；resolved=false 置灰并显示原因，不可勾选。 */
export default function RelationSuggestionList({ subjectName, value, onChange, disabled }: Props) {
  if (value.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有关系建议" />
  const patch = (key: string, p: Partial<EditableSuggestion>) =>
    onChange(value.map((s) => (s.key === key ? { ...s, ...p } : s)))
  const selectable = value.filter((s) => s.resolved)
  const allChecked = selectable.length > 0 && selectable.every((s) => s.checked)
  return (
    <Table<EditableSuggestion>
      size="small"
      rowKey="key"
      pagination={false}
      dataSource={value}
      scroll={{ x: 760 }}
      columns={[
        {
          title: (
            <Checkbox
              checked={allChecked}
              indeterminate={!allChecked && selectable.some((s) => s.checked)}
              disabled={disabled || selectable.length === 0}
              onChange={(e) => onChange(value.map((s) => (s.resolved ? { ...s, checked: e.target.checked } : s)))}
            />
          ),
          width: 40,
          render: (_, s) => (
            <Checkbox
              checked={s.checked && s.resolved}
              disabled={disabled || !s.resolved}
              onChange={(e) => patch(s.key, { checked: e.target.checked })}
            />
          ),
        },
        {
          title: '对方',
          width: 150,
          render: (_, s) => (
            <Space direction="vertical" size={0}>
              <Typography.Text type={s.resolved ? undefined : 'secondary'} delete={!s.resolved}>
                {s.target_name}
              </Typography.Text>
              {!s.resolved && (
                <Typography.Text type="danger" style={{ fontSize: 12 }}>
                  {s.reason || '无法关联到已有角色'}
                </Typography.Text>
              )}
            </Space>
          ),
        },
        {
          title: '称呼（对方 / 自己）',
          width: 240,
          render: (_, s) => (
            <Space size={4} wrap>
              <Tooltip title={`对方是${subjectName}的…`}>
                <Input
                  size="small"
                  style={{ width: 90 }}
                  maxLength={20}
                  value={s.edit_target_role}
                  disabled={disabled || !s.resolved}
                  onChange={(e) => patch(s.key, { edit_target_role: e.target.value })}
                />
              </Tooltip>
              <span>/</span>
              <Tooltip title={`${subjectName}是对方的…`}>
                <Input
                  size="small"
                  style={{ width: 90 }}
                  maxLength={20}
                  value={s.edit_self_role}
                  disabled={disabled || !s.resolved}
                  onChange={(e) => patch(s.key, { edit_self_role: e.target.value })}
                />
              </Tooltip>
              {s.type.type_id === null && <Tag color="orange">新类型</Tag>}
              <Tag color={categoryLabel(s.type.category).color}>{categoryLabel(s.type.category).label}</Tag>
            </Space>
          ),
        },
        {
          title: '亲密度',
          width: 90,
          render: (_, s) => (
            <InputNumber
              size="small"
              min={-100}
              max={100}
              value={s.intimacy}
              disabled={disabled || !s.resolved}
              onChange={(v) => patch(s.key, { intimacy: typeof v === 'number' ? v : 0 })}
              style={{ width: 76 }}
            />
          ),
        },
        {
          title: '描述',
          render: (_, s) => (
            <Input.TextArea
              size="small"
              autoSize={{ minRows: 1, maxRows: 4 }}
              value={s.description}
              disabled={disabled || !s.resolved}
              onChange={(e) => patch(s.key, { description: e.target.value })}
            />
          ),
        },
      ]}
    />
  )
}
