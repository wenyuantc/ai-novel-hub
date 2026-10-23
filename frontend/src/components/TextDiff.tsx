import { Alert, Space, Typography } from 'antd'
import type { CSSProperties } from 'react'
import { useMemo } from 'react'
import { computeDiff } from '../utils/textDiff'

interface Props {
  /** 旧文本 */
  a: string
  /** 新文本 */
  b: string
  maxHeight?: number
}

const box: CSSProperties = {
  overflow: 'auto',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  fontSize: 14,
  lineHeight: 1.8,
  padding: 12,
  background: '#fafafa',
  border: '1px solid #f0f0f0',
  borderRadius: 6,
}
const removedStyle: CSSProperties = { background: '#fff1f0', color: '#cf1322', textDecoration: 'line-through' }
const addedStyle: CSSProperties = { background: '#f6ffed', color: '#389e0d' }

/** 逐字差异对比：删除标红加删除线，新增标绿，并统计增删字数。 */
export default function TextDiff({ a, b, maxHeight = 480 }: Props) {
  const result = useMemo(() => computeDiff(a, b), [a, b])

  if (!result) {
    return <Alert type="warning" showIcon message="两份文本差异过大，无法逐字对比" />
  }
  const same = result.added === 0 && result.removed === 0 && a === b
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Space size="large">
        <Typography.Text style={{ color: '#389e0d' }}>+{result.added} 字</Typography.Text>
        <Typography.Text style={{ color: '#cf1322' }}>-{result.removed} 字</Typography.Text>
        {same && <Typography.Text type="secondary">内容相同</Typography.Text>}
      </Space>
      <div style={{ ...box, maxHeight }}>
        {result.parts.length === 0 ? (
          <Typography.Text type="secondary">（空）</Typography.Text>
        ) : (
          result.parts.map((p, i) =>
            p.kind === 'equal' ? (
              <span key={i}>{p.value}</span>
            ) : p.kind === 'added' ? (
              <ins key={i} style={addedStyle}>
                {p.value}
              </ins>
            ) : (
              <del key={i} style={removedStyle}>
                {p.value}
              </del>
            ),
          )
        )}
      </div>
    </Space>
  )
}
