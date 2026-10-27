import { ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Collapse, Space, Spin, theme, Typography } from 'antd'
import type { CSSProperties } from 'react'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { memoryApi } from '../../api/memories'
import type { ContextPreview } from '../../api/types'

interface Props {
  chapterId: number
}

const pre: CSSProperties = {
  whiteSpace: 'pre-wrap',
  fontSize: 12,
  maxHeight: 240,
  overflow: 'auto',
  padding: 8,
  borderRadius: 4,
  marginBottom: 0,
}

/** 「AI 生成」Tab 内的折叠面板：展开时读取本次生成会注入的【参考资料】与【相关旧情节】（裁剪前；为空表示不注入）。 */
export default function ContextPreviewPanel({ chapterId }: Props) {
  const { token } = theme.useToken()
  const [data, setData] = useState<ContextPreview | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await memoryApi.contextPreview(chapterId))
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Collapse
      size="small"
      onChange={(keys) => {
        if (keys.length > 0 && !data && !loading) void load()
      }}
      items={[
        {
          key: 'preview',
          label: '本次注入的旧情节和参考资料',
          extra: data ? (
            <Button
              type="text"
              size="small"
              aria-label="刷新注入预览"
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={(e) => {
                e.stopPropagation()
                void load()
              }}
            />
          ) : undefined,
          children: (
            <Spin spinning={loading}>
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                {error && <Alert type="error" showIcon message={error} />}
                {data && !data.memories && !data.knowledge && (
                  <Typography.Text type="secondary">
                    本次不会注入旧情节或参考资料（没有命中、没有更早的记忆，或已关闭知识库自动注入）。
                  </Typography.Text>
                )}
                {data?.memories && <pre style={{ ...pre, background: token.colorFillQuaternary }}>{data.memories}</pre>}
                {data?.knowledge && <pre style={{ ...pre, background: token.colorFillQuaternary }}>{data.knowledge}</pre>}
                {data && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    按当前保存的章节内容检索；实际生成时若上下文超出预算，会先裁掉参考资料尾部与未钉住的旧情节。
                  </Typography.Text>
                )}
              </Space>
            </Spin>
          ),
        },
      ]}
    />
  )
}
