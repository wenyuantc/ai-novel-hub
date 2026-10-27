import { Alert, List, Space, Tag, theme, Typography } from 'antd'
import type { BatchItem, BatchSummary } from '../../api/types'
import { batchCounts, batchRowView, batchSummaryText } from '../../utils/batch'
import StreamOutput from '../StreamOutput'

interface Props {
  /** checkpoint.items（与范围内章节一一对应） */
  items: BatchItem[]
  /** 正在处理的章节下标与阶段 */
  current?: { index: number; phase: 'generating' | 'analyzing' } | null
  /** 当前章的实时正文；undefined = 不显示实时输出 */
  text?: string
  running?: boolean
  /** done / paused / 结束后的汇总 */
  summary?: BatchSummary | null
}

/**
 * 批量生成的章节清单（状态、字数、分析结果、失败原因 / 警告）+ 当前章实时正文。
 * 章节页横幅展开与任务面板「查看结果」（BatchResultView）共用。
 */
export default function BatchProgressView({ items, current = null, text, running = false, summary = null }: Props) {
  const { token } = theme.useToken()
  const counts = batchCounts(items)
  const currentItem = current ? items[current.index] : undefined
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      {summary ? (
        <Alert
          type={summary.failed.length ? 'warning' : 'success'}
          showIcon
          message={batchSummaryText(summary)}
          description={
            summary.warnings.length > 0 ? (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {summary.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            ) : undefined
          }
        />
      ) : (
        items.length > 0 && (
          <Typography.Text type="secondary">
            已处理 {counts.processed}/{counts.total} 章，已生成 {counts.done} 章
          </Typography.Text>
        )
      )}
      {items.length === 0 ? (
        <Typography.Text type="secondary">任务开始运行后显示章节清单</Typography.Text>
      ) : (
        <div style={{ maxHeight: 320, overflow: 'auto', border: `1px solid ${token.colorBorderSecondary}`, borderRadius: 6 }}>
          <List
            size="small"
            dataSource={items}
            renderItem={(item, index) => {
              const view = batchRowView(item, current?.index === index ? current.phase : null)
              return (
                <List.Item key={item.chapter_id}>
                  <div style={{ width: '100%' }}>
                    <Space wrap size={6}>
                      <Typography.Text strong>第 {item.chapter_no ?? '?'} 章</Typography.Text>
                      <Typography.Text>{item.title || '（未命名）'}</Typography.Text>
                      <Tag color={view.color}>{view.label}</Tag>
                      {view.detail && (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {view.detail}
                        </Typography.Text>
                      )}
                    </Space>
                    {view.note && (
                      <Typography.Text
                        type={item.state === 'failed' ? 'danger' : 'warning'}
                        style={{ fontSize: 12, display: 'block' }}
                      >
                        {view.note}
                      </Typography.Text>
                    )}
                  </div>
                </List.Item>
              )
            }}
          />
        </div>
      )}
      {text !== undefined && current && (
        <>
          <Typography.Text type="secondary">
            第 {currentItem?.chapter_no ?? current.index + 1} 章 {currentItem?.title ?? ''} ·{' '}
            {current.phase === 'analyzing' ? '分析中（分析输出不显示）' : '实时输出'}
          </Typography.Text>
          <StreamOutput text={text} running={running} maxHeight={320} placeholder="等待输出…" />
        </>
      )}
    </Space>
  )
}
