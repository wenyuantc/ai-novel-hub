import { MoreOutlined } from '@ant-design/icons'
import { Button, Dropdown } from 'antd'
import type { BookChapterView, BookOp } from '../../api/types'
import { availableOps, BOOK_OP_LABELS } from '../../utils/bookImport'

/** 菜单动作：校正操作，或「查看全文」 */
export type ChapterAction = BookOp | 'view'

interface Props {
  chapter: BookChapterView
  total: number
  disabled?: boolean
  /** 移动端卡片里额外提供「查看全文」 */
  withView?: boolean
  onAction: (action: ChapterAction, chapter: BookChapterView) => void
}

/** 章节的「⋮」操作菜单：只列出当前可用的校正操作（最后一章不能合并、只剩 1 章不能删、不在卷内不能改卷名 / 取消分卷） */
export default function ChapterOpsMenu({ chapter, total, disabled, withView, onAction }: Props) {
  const ops = availableOps(chapter, total)
  const items = [
    ...(withView ? [{ key: 'view', label: '查看全文' }, { type: 'divider' as const }] : []),
    ...ops.map((op) => ({ key: op, label: BOOK_OP_LABELS[op], danger: op === 'delete' })),
  ]
  return (
    // 阻止冒泡：行 / 卡片点击会打开全文抽屉
    <span onClick={(e) => e.stopPropagation()}>
      <Dropdown
        trigger={['click']}
        disabled={disabled}
        menu={{
          items,
          onClick: ({ key, domEvent }) => {
            domEvent.stopPropagation()
            onAction(key as ChapterAction, chapter)
          },
        }}
      >
        <Button size="small" type="text" icon={<MoreOutlined />} aria-label={`第 ${chapter.index} 章操作`} />
      </Dropdown>
    </span>
  )
}
