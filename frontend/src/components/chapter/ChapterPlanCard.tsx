import { Card, Space, Tag, Typography } from 'antd'
import type { Plan } from '../../api/types'

interface Props {
  plan: Plan
}

/** 章节编辑器「AI 生成」上方的本章细纲要点：生成时「本章任务」取自细纲（梗概、关键事件、叙事目标、情感基调）。 */
export default function ChapterPlanCard({ plan }: Props) {
  return (
    <Card
      size="small"
      type="inner"
      title={
        <Space size={4} wrap>
          <span>本章细纲</span>
          <Tag>{plan.est_words} 字</Tag>
          {plan.emotion && <Tag color="purple">{plan.emotion}</Tag>}
        </Space>
      }
      styles={{ body: { padding: '8px 12px' } }}
    >
      <Typography.Text strong style={{ display: 'block' }}>
        {plan.title}
      </Typography.Text>
      {plan.summary && (
        <Typography.Paragraph
          type="secondary"
          style={{ whiteSpace: 'pre-wrap', margin: '4px 0' }}
          ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}
        >
          {plan.summary}
        </Typography.Paragraph>
      )}
      {plan.key_events.length > 0 && (
        <ol style={{ paddingInlineStart: 18, margin: '4px 0' }}>
          {plan.key_events.map((e) => (
            <li key={e}>
              <Typography.Text style={{ fontSize: 12 }}>{e}</Typography.Text>
            </li>
          ))}
        </ol>
      )}
      {plan.goal && (
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
          叙事目标：{plan.goal}
        </Typography.Text>
      )}
    </Card>
  )
}
