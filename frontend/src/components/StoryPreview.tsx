import { Card, Collapse, Descriptions, Empty, List, Tag, Typography } from 'antd'
import type { CSSProperties } from 'react'
import type { Character, Outline, WorldSetting } from '../api/types'
import { groupOutlines, volumeLabel } from '../utils/outline'

const pre: CSSProperties = { whiteSpace: 'pre-wrap', marginBottom: 0 }

export function WorldPreview({ items }: { items: WorldSetting[] }) {
  if (items.length === 0) return <Empty description="暂无世界观条目" />
  return (
    <List
      dataSource={items}
      renderItem={(w) => (
        <List.Item>
          <List.Item.Meta
            title={w.title}
            description={<Typography.Paragraph style={pre}>{w.content || '（无内容）'}</Typography.Paragraph>}
          />
        </List.Item>
      )}
    />
  )
}

export function CharacterPreview({ items }: { items: Character[] }) {
  if (items.length === 0) return <Empty description="暂无角色" />
  return (
    <List
      grid={{ gutter: 12, xs: 1, sm: 1, md: 2, lg: 2, xl: 3 }}
      dataSource={items}
      renderItem={(c) => (
        <List.Item>
          <Card
            size="small"
            title={
              <span>
                {c.name} {c.role_type && <Tag color="purple">{c.role_type}</Tag>}
              </span>
            }
          >
            <Descriptions size="small" column={1}>
              <Descriptions.Item label="简介">
                <span style={{ whiteSpace: 'pre-wrap' }}>{c.profile || '-'}</span>
              </Descriptions.Item>
              <Descriptions.Item label="关系">
                <span style={{ whiteSpace: 'pre-wrap' }}>{c.relations_text || '-'}</span>
              </Descriptions.Item>
            </Descriptions>
          </Card>
        </List.Item>
      )}
    />
  )
}

export function OutlinePreview({ items }: { items: Outline[] }) {
  if (items.length === 0) return <Empty description="暂无大纲" />
  const groups = groupOutlines(items)
  return (
    <Collapse
      defaultActiveKey={groups.map((g) => String(g.volumeNo))}
      items={groups.map((g) => ({
        key: String(g.volumeNo),
        label: `${volumeLabel(g)}（${g.items.length} 章）`,
        children: (
          <List
            size="small"
            dataSource={g.items}
            renderItem={(o) => (
              <List.Item>
                <List.Item.Meta
                  title={`第${o.chapter_no}章 ${o.title}`}
                  description={<span style={{ whiteSpace: 'pre-wrap' }}>{o.summary}</span>}
                />
              </List.Item>
            )}
          />
        ),
      }))}
    />
  )
}
