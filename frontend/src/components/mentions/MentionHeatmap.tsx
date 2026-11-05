import { Button, Empty, Space, Typography, theme } from 'antd'
import type { CSSProperties } from 'react'
import { useMemo } from 'react'
import { MENTION_CATEGORY_LABELS } from '../../api/mentions'
import type { MentionChapter, MentionEntity, MentionSummaryEntry } from '../../api/mentions'
import { mentionCellLabel, mentionChapterRows, mentionEntityKey } from '../../utils/mentionStats'

interface Props {
  entries: MentionSummaryEntry[]
  chapters: MentionChapter[]
  onSelect: (entity: MentionEntity, chapter?: MentionChapter) => void
}

const cell: CSSProperties = { padding: 0, minWidth: 48, textAlign: 'center' }

/** 章节热力图：数字、歧义标记和未写状态与颜色同时呈现，命中格可键盘打开。 */
export default function MentionHeatmap({ entries, chapters, onSelect }: Props) {
  const { token } = theme.useToken()
  const rows = useMemo(() => mentionChapterRows(entries, chapters), [entries, chapters])
  const max = Math.max(1, ...rows.map((row) => row.count))
  const byKey = new Map(rows.map((row) => [row.key, row]))
  const labelStyle: CSSProperties = {
    position: 'sticky', left: 0, zIndex: 1, width: 168, minWidth: 168, maxWidth: 168,
    padding: '4px 8px', textAlign: 'left', background: token.colorBgContainer,
  }
  if (!chapters.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这个范围还没有章节" />
  if (!entries.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有符合筛选条件的条目" />

  return <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
    <Typography.Text type="secondary">
      数字为确定提及次数，颜色越深表示本窗口内越集中。? 为有歧义的次数，— 为未写章节。
    </Typography.Text>
    <div style={{ overflowX: 'auto', maxWidth: '100%', border: `1px solid ${token.colorBorderSecondary}`, borderRadius: token.borderRadius }}>
      <table style={{ borderCollapse: 'separate', borderSpacing: 4, width: 'max-content', minWidth: '100%' }}>
        <caption style={{ padding: 8, textAlign: 'left', color: token.colorTextSecondary }}>条目 × 章节提及热力图；点击有提及的格子查看原文</caption>
        <thead><tr>
          <th scope="col" style={labelStyle}>设定条目</th>
          {chapters.map((chapter) => <th scope="col" key={chapter.id} title={chapter.title}
            style={{ ...cell, padding: '8px 4px', fontWeight: 500, color: token.colorTextSecondary }}>
            第 {chapter.chapter_no} 章
          </th>)}
        </tr></thead>
        <tbody>{entries.map((entry) => <tr key={mentionEntityKey(entry.entity)}>
          <th scope="row" style={labelStyle}>
            <Button type="link" size="small" onClick={() => onSelect(entry.entity)}
              style={{ height: 'auto', padding: 0, whiteSpace: 'normal', textAlign: 'left', overflowWrap: 'anywhere' }}>
              {entry.entity.name}
            </Button>
            <div style={{ color: token.colorTextSecondary, fontSize: 12, fontWeight: 400 }}>{MENTION_CATEGORY_LABELS[entry.entity.category]}</div>
          </th>
          {chapters.map((chapter) => {
            const row = byKey.get(`${mentionEntityKey(entry.entity)}:${chapter.id}`)
            if (!row) return null
            const hit = row.count > 0 || row.ambiguous_count > 0
            const label = mentionCellLabel(row)
            return <td key={chapter.id} style={cell}>
              {!chapter.has_content ? <span title={label} aria-label={label}
                style={{ display: 'block', padding: '8px 4px', color: token.colorTextSecondary }}>—</span> :
                <Button type="text" size="small" disabled={!hit} title={label} aria-label={label}
                  onClick={() => onSelect(entry.entity, chapter)}
                  style={{ position: 'relative', overflow: 'hidden', width: '100%', height: 40, padding: '2px 4px',
                    border: `1px ${row.ambiguous_count > 0 ? 'dashed' : 'solid'} ${row.ambiguous_count > 0 ? token.colorWarning : token.colorBorderSecondary}`,
                    color: hit ? token.colorText : token.colorTextSecondary, background: token.colorBgContainer }}>
                  {row.count > 0 && <span aria-hidden style={{ position: 'absolute', inset: 0, background: token.colorPrimary, opacity: 0.08 + 0.18 * row.count / max }} />}
                  <span style={{ position: 'relative', display: 'block', lineHeight: 1.2 }}>
                    {row.count}
                    {row.ambiguous_count > 0 && <small style={{ display: 'block', color: token.colorWarningText }}>? {row.ambiguous_count}</small>}
                  </span>
                </Button>}
            </td>
          })}
        </tr>)}</tbody>
      </table>
    </div>
  </Space>
}
