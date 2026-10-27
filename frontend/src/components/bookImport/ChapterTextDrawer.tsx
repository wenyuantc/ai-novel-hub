import { ScissorOutlined } from '@ant-design/icons'
import { Alert, App, Drawer, Spin, theme, Typography } from 'antd'
import { useCallback } from 'react'
import { bookImportApi } from '../../api/bookImport'
import type { BookChapterView } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useLoader } from '../../hooks/useLoader'
import { canSplitAt, formatChars } from '../../utils/bookImport'

interface Props {
  importId: number
  /** 打开的章节；null = 关闭 */
  chapter: BookChapterView | null
  /** 修订号变化时重新加载（全文随校正变化） */
  revision: number
  disabled?: boolean
  onClose: () => void
  /** 从第 line 行拆分（行号从 1 起；由页面执行并处理 409） */
  onSplit: (chapter: BookChapterView, line: number, title: string) => void
}

/** 章节全文抽屉：带行号，点击某一行（非第 1 行、非空行）确认后从此行拆分，拆出的新章标题取该行。移动端宽度 100%。 */
export default function ChapterTextDrawer({ importId, chapter, revision, disabled, onClose, onSplit }: Props) {
  const { modal } = App.useApp()
  const { token } = theme.useToken()
  const isMobile = useIsMobile()
  const index = chapter?.index ?? null
  const loader = useCallback(
    // revision 参与依赖：校正后重新取全文
    () => (index !== null && revision >= 0 ? bookImportApi.chapterText(importId, index) : Promise.resolve(null)),
    [importId, index, revision],
  )
  const { data, loading, error } = useLoader(loader)
  const lines = data?.lines ?? []

  const confirmSplit = (line: number) => {
    if (!chapter || disabled || !canSplitAt(lines, line)) return
    const title = lines[line - 1].trim().slice(0, 200)
    modal.confirm({
      title: `从第 ${line} 行拆分？`,
      content: `第 ${line} 行及之后的内容成为新的一章，标题为「${title}」。`,
      okText: '拆分',
      cancelText: '取消',
      onOk: () => onSplit(chapter, line, title),
    })
  }

  return (
    <Drawer
      open={chapter !== null}
      onClose={onClose}
      width={isMobile ? '100%' : 640}
      title={chapter ? `${chapter.index}. ${chapter.title}` : ''}
      destroyOnHidden
    >
      {chapter && (
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          {formatChars(chapter.chars)} · 点击某一行可从此行拆分（第 1 行与空行除外）；正文请在导入后到编辑器中修改。
        </Typography.Paragraph>
      )}
      {error && <Alert type="error" showIcon message={error} />}
      {/* 悬停高亮与剪刀图标（颜色取 token，随主题变化） */}
      <style>{`.book-import-line:hover{background:${token.colorFillTertiary}}.book-import-line .book-import-line-icon{visibility:hidden}.book-import-line:hover .book-import-line-icon{visibility:visible}`}</style>
      <Spin spinning={loading}>
        <div style={{ fontSize: 14, lineHeight: 1.8 }}>
          {lines.map((text, i) => {
            const line = i + 1
            const splittable = !disabled && canSplitAt(lines, line)
            return (
              <div
                key={i}
                onClick={splittable ? () => confirmSplit(line) : undefined}
                title={splittable ? '从此行拆分' : undefined}
                className="book-import-line"
                style={{ display: 'flex', gap: 8, cursor: splittable ? 'pointer' : 'default', borderRadius: 4 }}
              >
                <span
                  style={{
                    flex: '0 0 auto',
                    minWidth: 36,
                    textAlign: 'right',
                    color: token.colorTextQuaternary,
                    userSelect: 'none',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {line}
                </span>
                <span style={{ flex: 1, minWidth: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{text || ' '}</span>
                {splittable && (
                  <ScissorOutlined className="book-import-line-icon" style={{ color: token.colorTextTertiary, paddingTop: 6 }} />
                )}
              </div>
            )
          })}
        </div>
      </Spin>
    </Drawer>
  )
}
