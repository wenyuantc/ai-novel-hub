import { LockOutlined } from '@ant-design/icons'
import { Alert, Card, Empty, Space, Spin, Tag, theme, Typography } from 'antd'
import type { CSSProperties } from 'react'
import type { PromptPreview } from '../../api/types'
import { formatIssue, splitLocked } from '../../utils/prompts'

interface Props {
  preview: PromptPreview | null
  loading: boolean
  /** 预览请求本身失败（网络等），与模板校验问题区分 */
  error: string | null
}

const box: CSSProperties = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  fontSize: 12,
  lineHeight: 1.7,
  maxHeight: 520,
  overflow: 'auto',
  margin: 0,
}
const lockedStyle: CSSProperties = {
  display: 'block',
  padding: '2px 8px',
  margin: '2px 0',
  userSelect: 'text',
}

const ROLE_LABELS: Record<string, string> = { system: '系统消息', user: '用户消息', assistant: '助手消息' }

/** 用示例数据渲染的 messages；锁定的输出结构 / 规则段灰底只读展示。 */
export default function PromptPreviewPane({ preview, loading, error }: Props) {
  const { token } = theme.useToken()
  return (
    <Card
      size="small"
      title={
        <Space>
          实时预览（示例数据）
          {loading && <Spin size="small" />}
        </Space>
      }
    >
      {error && <Alert type="warning" showIcon message={`预览失败：${error}`} style={{ marginBottom: 8 }} />}
      {preview && preview.errors.length > 0 && (
        <Alert
          type="error"
          showIcon
          message="模板有错误，修正后才能预览"
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {preview.errors.map((e, i) => (
                <li key={i}>{formatIssue(e)}</li>
              ))}
            </ul>
          }
          style={{ marginBottom: 8 }}
        />
      )}
      {!preview ? (
        loading ? null : <Empty description="暂无预览" />
      ) : (
        <Space direction="vertical" style={{ width: '100%' }}>
          {preview.messages.map((m, i) => (
            <div key={i}>
              <Typography.Text strong>{ROLE_LABELS[m.role] ?? m.role}</Typography.Text>
              <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                {Array.from(m.content).length} 字
              </Typography.Text>
              <pre style={box}>
                {splitLocked(m.content, preview.locked).map((seg, j) =>
                  seg.locked ? (
                    <span key={j} style={{ ...lockedStyle, background: token.colorFillTertiary, color: token.colorTextSecondary, borderLeft: `3px solid ${token.colorBorder}` }} title="锁定内容由系统提供，不能修改">
                      <Tag icon={<LockOutlined />} style={{ marginBottom: 2 }}>
                        锁定：{seg.locked}
                      </Tag>
                      {'\n'}
                      {seg.text}
                    </span>
                  ) : (
                    <span key={j}>{seg.text}</span>
                  ),
                )}
              </pre>
            </div>
          ))}
        </Space>
      )}
    </Card>
  )
}
