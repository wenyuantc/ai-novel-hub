import { Checkbox, Empty, Input, Select, Space, Table, Tag, Typography } from 'antd'
import { CAREER_ROLE_LABELS } from '../../api/careers'
import type { EditableCareerSuggestion } from '../../api/entityTasks'
import type { Career } from '../../api/types'

interface Props {
  value: EditableCareerSuggestion[]
  onChange: (next: EditableCareerSuggestion[]) => void
  /** 本项目职业（用于换阶段下拉）；未加载时阶段只读 */
  careers: Career[]
  disabled?: boolean
}

/** AI 生成角色的职业建议：resolved=false 置灰显示原因；可改阶段与状态备注。 */
export default function CareerSuggestionList({ value, onChange, careers, disabled }: Props) {
  if (value.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有职业建议" />
  const patch = (key: string, p: Partial<EditableCareerSuggestion>) =>
    onChange(value.map((s) => (s.key === key ? { ...s, ...p } : s)))
  return (
    <Table<EditableCareerSuggestion>
      size="small"
      rowKey="key"
      pagination={false}
      dataSource={value}
      columns={[
        {
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
          title: '职业',
          render: (_, s) => (
            <Space direction="vertical" size={0}>
              <Space size={4}>
                <Typography.Text delete={!s.resolved} type={s.resolved ? undefined : 'secondary'}>
                  {s.career_name}
                </Typography.Text>
                {s.role && <Tag color={s.role === 'main' ? 'gold' : 'blue'}>{CAREER_ROLE_LABELS[s.role]}</Tag>}
              </Space>
              {!s.resolved && (
                <Typography.Text type="danger" style={{ fontSize: 12 }}>
                  {s.reason || '无法关联到已有职业'}
                </Typography.Text>
              )}
            </Space>
          ),
        },
        {
          title: '阶段',
          width: 180,
          render: (_, s) => {
            const career = careers.find((c) => c.id === s.career_id)
            if (!career || !s.resolved) return s.stage_name || '-'
            return (
              <Select
                size="small"
                style={{ width: '100%' }}
                value={s.stage_id ?? undefined}
                disabled={disabled}
                options={career.stages.map((st) => ({ value: st.id, label: `${st.position}·${st.name}` }))}
                onChange={(id: number) => {
                  const st = career.stages.find((x) => x.id === id)
                  patch(s.key, { stage_id: id, stage_name: st?.name ?? s.stage_name, stage_position: st?.position ?? null })
                }}
              />
            )
          },
        },
        {
          title: '状态备注',
          render: (_, s) => (
            <Input
              size="small"
              maxLength={200}
              value={s.note}
              disabled={disabled || !s.resolved}
              onChange={(e) => patch(s.key, { note: e.target.value })}
            />
          ),
        },
      ]}
    />
  )
}
