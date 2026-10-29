import type { ReactNode } from 'react'
import { Modal, Space, Typography } from 'antd'
import TextDiff from '../TextDiff'

export interface DiffPair {
  label: string
  a: string
  b: string
}

interface Props {
  open: boolean
  title: string
  /** 说明 a / b 分别是什么 */
  hint?: string
  pairs: DiffPair[]
  footer?: ReactNode
  onClose: () => void
}

/** 模板差异对比（系统段 / 用户段各一份逐字 diff），复用章节版本的 TextDiff。 */
export default function PromptDiffModal({ open, title, hint, pairs, footer = null, onClose }: Props) {
  return (
    <Modal open={open} title={title} onCancel={onClose} footer={footer} width={900} destroyOnHidden>
      <Space direction="vertical" style={{ width: '100%' }}>
        {hint && <Typography.Text type="secondary">{hint}</Typography.Text>}
        {pairs.map((p) => (
          <div key={p.label}>
            <Typography.Title level={5}>{p.label}</Typography.Title>
            <TextDiff a={p.a} b={p.b} maxHeight={360} />
          </div>
        ))}
      </Space>
    </Modal>
  )
}
