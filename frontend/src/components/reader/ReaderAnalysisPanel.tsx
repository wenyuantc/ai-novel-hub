import { Alert, Empty, List, Space, Spin, Tag, Typography } from 'antd'
import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { analysisApi } from '../../api/analysis'
import { candidateLabel, readCandidates } from '../../api/foreshadows'
import type { Analysis } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'

interface Props {
  projectId: number
  chapterId: number
}

/** 阅读器侧边栏：本章最新一次分析的摘要、情节点、钩子与伏笔；没有分析时提示并给出「去分析」入口。 */
export default function ReaderAnalysisPanel({ projectId, chapterId }: Props) {
  const loader = useCallback(async (): Promise<Analysis | null> => {
    const list = await analysisApi.list(chapterId)
    return list[0] ? analysisApi.get(list[0].id) : null
  }, [chapterId])
  const { data, loading, error } = useLoader(loader)
  const editorLink = `/projects/${projectId}/chapters/${chapterId}?panel=analysis`

  if (loading && data === undefined) {
    return (
      <div style={{ padding: 32, textAlign: 'center' }}>
        <Spin />
      </div>
    )
  }
  if (error) return <Alert type="error" showIcon message={error} />
  if (!data) {
    return (
      <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本章还没有分析">
        <Link to={editorLink}>去分析</Link>
      </Empty>
    )
  }
  const candidates = readCandidates(data.foreshadow_candidates)
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space wrap size={4}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          分析于 {formatTime(data.created_at)}
        </Typography.Text>
        {data.stale && <Tag color="warning">正文已修改，分析可能过期</Tag>}
        <Link to={editorLink}>查看完整分析</Link>
      </Space>
      <section>
        <Typography.Title level={5}>摘要</Typography.Title>
        {data.summary ? (
          <Typography.Paragraph style={{ marginBottom: 0 }}>{data.summary}</Typography.Paragraph>
        ) : (
          <Typography.Text type="secondary">无</Typography.Text>
        )}
      </section>
      <section>
        <Typography.Title level={5}>情节点</Typography.Title>
        <List
          size="small"
          dataSource={data.plot_points}
          locale={{ emptyText: '无' }}
          renderItem={(p) => (
            <List.Item style={{ paddingInline: 0 }}>
              <Space align="start" size={6}>
                <Tag color={p.importance >= 4 ? 'red' : p.importance >= 3 ? 'orange' : 'default'} style={{ marginInlineEnd: 0 }}>
                  {'★'.repeat(Math.max(1, Math.min(5, p.importance)))}
                </Tag>
                <span>{p.text}</span>
              </Space>
            </List.Item>
          )}
        />
      </section>
      <section>
        <Typography.Title level={5}>钩子</Typography.Title>
        <List
          size="small"
          dataSource={data.hooks}
          locale={{ emptyText: '无' }}
          renderItem={(h) => (
            <List.Item style={{ paddingInline: 0 }}>
              <Space align="start" size={6}>
                <Tag color="purple" style={{ marginInlineEnd: 0 }}>
                  {h.type || '钩子'} · {h.strength}
                </Tag>
                <span>{h.text}</span>
              </Space>
            </List.Item>
          )}
        />
      </section>
      <section>
        <Typography.Title level={5}>伏笔</Typography.Title>
        <List
          size="small"
          dataSource={candidates}
          locale={{ emptyText: '无' }}
          renderItem={(c) => (
            <List.Item style={{ paddingInline: 0 }}>
              <Space direction="vertical" size={0}>
                <Space size={6} wrap>
                  <Tag color={c.action === 'resolve' ? 'green' : 'blue'} style={{ marginInlineEnd: 0 }}>
                    {candidateLabel(c)}
                  </Tag>
                  {c.keyword && <Typography.Text strong>{c.keyword}</Typography.Text>}
                  {c.expected_resolve_chapter !== null && (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      预计第 {c.expected_resolve_chapter} 章回收
                    </Typography.Text>
                  )}
                </Space>
                {c.text && <span>{c.text}</span>}
              </Space>
            </List.Item>
          )}
        />
      </section>
    </Space>
  )
}
