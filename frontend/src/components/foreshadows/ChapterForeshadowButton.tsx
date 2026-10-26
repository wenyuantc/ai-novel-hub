import { BulbOutlined } from '@ant-design/icons'
import { Badge, Button, Empty, Popover, Space, Spin, Tag, Typography } from 'antd'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { foreshadowApi, reminderBadgeCount, reminderSections } from '../../api/foreshadows'
import { useLoader } from '../../hooks/useLoader'

interface Props {
  projectId: number
  chapterId: number
}

const SECTION_COLOR: Record<string, string> = {
  to_plant: 'blue',
  must_resolve: 'green',
  overdue: 'red',
  upcoming: 'orange',
  long_term: 'purple',
}

/** 章节编辑器工具栏「本章伏笔」：徽标 = 本章计划埋设 + 必须回收；Popover 按【伏笔提醒】五档展示（与生成上下文同源）。 */
export default function ChapterForeshadowButton({ projectId, chapterId }: Props) {
  const loader = useCallback(() => foreshadowApi.reminders(projectId, chapterId), [projectId, chapterId])
  const { data, loading, error, reload } = useLoader(loader)
  const count = reminderBadgeCount(data)
  const sections = reminderSections(data)

  const content = (
    <div style={{ width: 360, maxHeight: 420, overflow: 'auto' }}>
      <Spin spinning={loading}>
        {error ? (
          <Typography.Text type="danger">{error}</Typography.Text>
        ) : sections.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本章没有需要提醒的伏笔" />
        ) : (
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            {sections.map((s) => (
              <div key={s.key}>
                <Tag color={SECTION_COLOR[s.key]}>{s.label}</Tag>
                {s.items.map(({ foreshadow: f, detail }) => (
                  <div key={f.id} style={{ marginTop: 4, paddingLeft: 4 }}>
                    <Link to={`/projects/${projectId}/foreshadows?id=${f.id}`}>
                      #{f.id} {f.title}
                    </Link>
                    {f.character_names.length > 0 && (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        （关联：{f.character_names.join('、')}）
                      </Typography.Text>
                    )}
                    {detail && (
                      <div>
                        <Typography.Text type={s.key === 'overdue' ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
                          {detail}
                        </Typography.Text>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </Space>
        )}
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '8px 0 0' }}>
          生成本章时这些内容会作为【伏笔提醒】注入上下文（关闭「注入上下文」的伏笔不出现）。
          <Link to={`/projects/${projectId}/foreshadows`}>管理伏笔</Link>
        </Typography.Paragraph>
      </Spin>
    </div>
  )

  return (
    <Popover
      title={data ? `第 ${data.chapter_no} 章伏笔` : '本章伏笔'}
      content={content}
      trigger="click"
      placement="bottomRight"
      onOpenChange={(open) => open && reload()}
    >
      <Badge count={count} size="small" title={`本章计划埋设 / 必须回收 ${count} 条`}>
        <Button size="small" icon={<BulbOutlined />}>
          本章伏笔
        </Button>
      </Badge>
    </Popover>
  )
}
