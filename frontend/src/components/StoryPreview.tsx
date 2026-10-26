import { Card, Collapse, Descriptions, Empty, List, Tag, Typography } from 'antd'
import type { CSSProperties } from 'react'
import { careerKindLabel } from '../api/careers'
import { lifeStatusLabel, roleTypeColor } from '../api/characters'
import type { Career, Character, Outline, WorldSetting } from '../api/types'
import { groupOutlines, volumeLabel } from '../utils/outline'
import { asWorldCategory, WORLD_CATEGORY_LABELS } from '../utils/world'

const pre: CSSProperties = { whiteSpace: 'pre-wrap', marginBottom: 0 }

export function WorldPreview({ items }: { items: WorldSetting[] }) {
  if (items.length === 0) return <Empty description="暂无世界观条目" />
  return (
    <List
      dataSource={items}
      renderItem={(w) => (
        <List.Item>
          <List.Item.Meta
            title={
              <span>
                <Tag>{WORLD_CATEGORY_LABELS[asWorldCategory(w.category ?? 'other')]}</Tag>
                {w.title}
              </span>
            }
            description={<Typography.Paragraph style={pre}>{w.content || '（无内容）'}</Typography.Paragraph>}
          />
        </List.Item>
      )}
    />
  )
}

/** 角色向导结果：结构化字段（旧格式结果只有简介 / 旧关系备注，同样可展示） */
export function CharacterPreview({ items }: { items: Character[] }) {
  if (items.length === 0) return <Empty description="暂无角色" />
  return (
    <List
      grid={{ gutter: 12, xs: 1, sm: 1, md: 2, lg: 2, xl: 3 }}
      dataSource={items}
      renderItem={(c) => {
        const life = lifeStatusLabel(c.life_status ?? 'alive')
        const rows: [string, string | undefined][] = [
          ['别名', (c.aliases ?? []).join('、')],
          ['年龄 / 性别', [c.age, c.gender].filter(Boolean).join(' / ')],
          ['外貌', c.appearance],
          ['性格', c.personality],
          ['背景', c.background],
          ['简介', c.profile],
          ['旧关系备注', c.relations_text],
        ]
        return (
          <List.Item>
            <Card
              size="small"
              title={
                <span>
                  {c.name} {c.role_type && <Tag color={roleTypeColor(c.role_type)}>{c.role_type}</Tag>}
                  {c.life_status && c.life_status !== 'alive' && <Tag color={life.color}>{life.label}</Tag>}
                </span>
              }
            >
              <Descriptions size="small" column={1}>
                {rows
                  .filter(([, v]) => !!v)
                  .map(([label, v]) => (
                    <Descriptions.Item key={label} label={label}>
                      <Typography.Paragraph style={pre} ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}>
                        {v}
                      </Typography.Paragraph>
                    </Descriptions.Item>
                  ))}
              </Descriptions>
            </Card>
          </List.Item>
        )
      }}
    />
  )
}

/** 职业体系向导结果：类别、阶梯与说明 */
export function CareerPreview({ items }: { items: Career[] }) {
  if (items.length === 0) return <Empty description="暂无职业" />
  return (
    <List
      grid={{ gutter: 12, xs: 1, sm: 1, md: 2, lg: 2, xl: 3 }}
      dataSource={items}
      renderItem={(c) => {
        const rows: [string, string][] = [
          ['描述', c.description],
          ['获得条件', c.requirements],
          ['特殊能力', c.abilities],
          ['体系规则', c.rules],
        ]
        return (
          <List.Item>
            <Card
              size="small"
              title={
                <span>
                  {c.name} <Tag color={c.kind === 'sub' ? 'blue' : 'gold'}>{careerKindLabel(c.kind)}</Tag>
                </span>
              }
            >
              <Typography.Paragraph style={{ marginBottom: 8 }}>
                {(c.stages ?? []).map((s) => s.name).join(' → ')}
              </Typography.Paragraph>
              <Descriptions size="small" column={1}>
                {rows
                  .filter(([, v]) => !!v)
                  .map(([label, v]) => (
                    <Descriptions.Item key={label} label={label}>
                      <Typography.Paragraph style={pre} ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}>
                        {v}
                      </Typography.Paragraph>
                    </Descriptions.Item>
                  ))}
              </Descriptions>
            </Card>
          </List.Item>
        )
      }}
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
