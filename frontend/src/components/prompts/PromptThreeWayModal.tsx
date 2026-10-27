import { Col, Modal, Row, Tabs, theme, Typography } from 'antd'
import type { CSSProperties } from 'react'
import TextDiff from '../TextDiff'

export interface ThreeWayPart {
  label: string
  oldDefault: string
  newDefault: string
  mine: string
}

interface Props {
  open: boolean
  parts: ThreeWayPart[]
  onClose: () => void
}

const pre: CSSProperties = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  fontSize: 12,
  lineHeight: 1.6,
  maxHeight: 320,
  overflow: 'auto',
  borderRadius: 6,
  padding: 8,
  margin: 0,
}

/** 默认已更新时的三方对比：旧默认（保存覆盖时）/ 新默认 / 我的版本，并给出「默认的变化」与「我的改动」两份 diff。 */
export default function PromptThreeWayModal({ open, parts, onClose }: Props) {
  const { token } = theme.useToken()
  return (
    <Modal open={open} title="三方对比：旧默认 / 新默认 / 我的版本" onCancel={onClose} footer={null} width={1100} destroyOnHidden>
      <Tabs
        items={parts.map((p) => ({
          key: p.label,
          label: p.label,
          children: (
            <>
              <Row gutter={12}>
                {(
                  [
                    ['旧默认（保存覆盖时）', p.oldDefault],
                    ['新默认（当前系统默认）', p.newDefault],
                    ['我的版本', p.mine],
                  ] as const
                ).map(([title, text]) => (
                  <Col span={8} key={title}>
                    <Typography.Text strong>{title}</Typography.Text>
                    <pre style={{ ...pre, background: token.colorFillQuaternary, border: `1px solid ${token.colorBorderSecondary}` }}>{text || '（空）'}</pre>
                  </Col>
                ))}
              </Row>
              <Typography.Title level={5} style={{ marginTop: 16 }}>
                系统默认的变化（旧默认 → 新默认）
              </Typography.Title>
              <TextDiff a={p.oldDefault} b={p.newDefault} maxHeight={260} />
              <Typography.Title level={5} style={{ marginTop: 16 }}>
                我的改动（旧默认 → 我的版本）
              </Typography.Title>
              <TextDiff a={p.oldDefault} b={p.mine} maxHeight={260} />
            </>
          ),
        }))}
      />
    </Modal>
  )
}
