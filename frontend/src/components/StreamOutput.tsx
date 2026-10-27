import { Spin, theme, Typography } from 'antd'
import { useEffect, useRef } from 'react'

interface Props {
  text: string
  running: boolean
  placeholder?: string
  maxHeight?: number
}

/** 流式输出展示框：增量追加并自动滚动到底部。 */
export default function StreamOutput({ text, running, placeholder = '生成内容将在这里实时显示…', maxHeight = 360 }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const { token } = theme.useToken()

  useEffect(() => {
    const el = ref.current
    if (el && running) el.scrollTop = el.scrollHeight
  }, [text, running])

  return (
    <div
      ref={ref}
      style={{
        maxHeight,
        minHeight: 120,
        overflow: 'auto',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: 13,
        lineHeight: 1.6,
        padding: 12,
        background: token.colorFillQuaternary,
        border: `1px solid ${token.colorBorderSecondary}`,
        borderRadius: 6,
      }}
    >
      {text ? text : <Typography.Text type="secondary">{placeholder}</Typography.Text>}
      {running && (
        <span style={{ marginLeft: 6 }}>
          <Spin size="small" />
        </span>
      )}
    </div>
  )
}
