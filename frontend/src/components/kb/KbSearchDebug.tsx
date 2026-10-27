import { App, Button, Card, Empty, Input, InputNumber, List, Space, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { kbApi } from '../../api/kb'
import type { KbSearchResult } from '../../api/types'
import ScoreTags from './ScoreTags'
import SearchResultSummary from './SearchResultSummary'

interface Props {
  projectId: number
}

/** 知识库检索调试：输入查询（可临时覆盖条数与最低分），列出命中的文件、标题路径、内容与三项分数。 */
export default function KbSearchDebug({ projectId }: Props) {
  const { message } = App.useApp()
  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState<number | null>(null)
  const [minScore, setMinScore] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<KbSearchResult | null>(null)

  const run = async () => {
    const q = query.trim()
    if (!q) {
      message.warning('请输入检索内容')
      return
    }
    setLoading(true)
    try {
      setResult(await kbApi.search(projectId, { query: q, top_k: topK, min_score: minScore }))
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Card size="small" title="检索调试">
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Input.TextArea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          maxLength={2000}
          autoSize={{ minRows: 2, maxRows: 6 }}
          placeholder="输入要检索的内容，例如「天剑宗的门规」"
        />
        <Space wrap>
          <InputNumber
            min={1}
            max={50}
            precision={0}
            value={topK}
            onChange={(v) => setTopK(v)}
            placeholder="条数（默认按设置）"
            style={{ width: 170 }}
          />
          <InputNumber
            min={0}
            max={1}
            step={0.05}
            precision={2}
            value={minScore}
            onChange={(v) => setMinScore(v)}
            placeholder="最低分（默认按设置）"
            style={{ width: 170 }}
          />
          <Button type="primary" loading={loading} onClick={() => void run()}>
            检索
          </Button>
        </Space>
        {result && (
          <>
            <SearchResultSummary result={result} />
            {result.hits.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有达到最低分数的结果" />
            ) : (
              <List
                size="small"
                dataSource={result.hits}
                rowKey="chunk_id"
                renderItem={(h) => (
                  <List.Item>
                    <Space direction="vertical" size={4} style={{ width: '100%' }}>
                      <Space wrap size={8}>
                        <Typography.Text strong>《{h.filename}》</Typography.Text>
                        {h.heading_path && <Typography.Text type="secondary">{h.heading_path}</Typography.Text>}
                      </Space>
                      <ScoreTags score={h.score} vectorScore={h.vector_score} keywordScore={h.keyword_score} />
                      <Typography.Paragraph
                        style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                        ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}
                      >
                        {h.content}
                      </Typography.Paragraph>
                    </Space>
                  </List.Item>
                )}
              />
            )}
          </>
        )}
      </Space>
    </Card>
  )
}
