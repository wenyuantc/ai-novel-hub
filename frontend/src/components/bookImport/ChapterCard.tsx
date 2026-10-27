import { Tag, theme, Tooltip, Typography } from 'antd'
import type { BookChapterView } from '../../api/types'
import { BOOK_WARNING_LABELS, formatChars, volumeLabel } from '../../utils/bookImport'
import ChapterOpsMenu, { type ChapterAction } from './ChapterOpsMenu'

interface Props {
  chapter: BookChapterView
  total: number
  /** 是否为所在卷的第一章（显示卷名标签） */
  volumeStart: boolean
  disabled?: boolean
  onOpen: (chapter: BookChapterView) => void
  onAction: (action: ChapterAction, chapter: BookChapterView) => void
}

/**
 * 移动端校正列表的一行（卡片）：序号 + 标题、卷名（卷首章）、字数与开头一行、提示标签，操作收进「⋮」菜单（含查看全文）。
 * 宽度随容器（不写固定宽度），文字单行省略，375px 下不撑出横向滚动。
 */
export default function ChapterCard({ chapter: ch, total, volumeStart, disabled, onOpen, onAction }: Props) {
  const { token } = theme.useToken()
  const volume = volumeLabel(ch)
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(ch)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onOpen(ch)
      }}
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 8,
        padding: '8px 4px',
        minWidth: 0,
        cursor: 'pointer',
        borderBottom: `1px solid ${token.colorBorderSecondary}`,
      }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        {volumeStart && volume && (
          <Tag color="blue" style={{ marginBottom: 4, maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {volume}
          </Tag>
        )}
        <Typography.Text strong ellipsis style={{ display: 'block' }}>
          <Typography.Text type="secondary">{ch.index}.</Typography.Text> {ch.title}
        </Typography.Text>
        <Typography.Text type="secondary" ellipsis style={{ display: 'block', fontSize: 12 }}>
          {formatChars(ch.chars)} · {ch.first_line || '（空）'}
        </Typography.Text>
        {ch.warnings.length > 0 && (
          <div style={{ marginTop: 4, display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {ch.warnings.map((w) => (
              <Tooltip key={w} title={BOOK_WARNING_LABELS[w].hint}>
                <Tag color={BOOK_WARNING_LABELS[w].color} style={{ marginInlineEnd: 0 }}>
                  {BOOK_WARNING_LABELS[w].label}
                </Tag>
              </Tooltip>
            ))}
          </div>
        )}
      </div>
      <ChapterOpsMenu chapter={ch} total={total} disabled={disabled} withView onAction={onAction} />
    </div>
  )
}
