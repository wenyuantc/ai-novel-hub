import { App, Button, Card, Checkbox, Empty, Input, InputNumber, List, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { memoryApi } from '../../api/memories'
import type { MemorySearchResult } from '../../api/types'
import { MEMORY_KIND_COLORS, MEMORY_KIND_LABELS } from '../../utils/kb'
import MemorySourceBadge from './MemorySourceBadge'
import ScoreTags from './ScoreTags'
import SearchResultSummary from './SearchResultSummary'

interface Props {
  projectId: number
}

/** 剧情记忆检索调试：查询 +「按第 N 章召回」（只在第 N 章之前检索）+ 包含已屏蔽，列出命中记忆与三项分数。 */
export default function MemorySearchDebug({ projectId }: Props) {
  const { message } = App.useApp()
  const [query, setQuery] = useState('')
  const [beforeNo, setBeforeNo] = useState<number | null>(null)
  const [includeMuted, setIncludeMuted] = useState(false)
  const [topK, setTopK] = useState<number | null>(null)
  const [minScore, setMinScore] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<MemorySearchResult | null>(null)

  const run = async () => {
    const q = query.trim()
    if (!q) {
      message.warning('请输入检索内容')
      return
    }
    setLoading(true)
    try {
      setResult(
        await memoryApi.search(projectId, {
          query: q,
          before_chapter_no: beforeNo,
          include_muted: includeMuted,
          top_k: topK,
          min_score: minScore,
        }),
      )
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
          placeholder="输入本章标题、梗概、关键事件或角色名"
        />
        <Space wrap>
          <InputNumber
            min={1}
            max={100000}
            precision={0}
            value={beforeNo}
            onChange={(v) => setBeforeNo(v)}
            addonBefore="按第"
            addonAfter="章召回"
            placeholder="不限"
            style={{ width: 200 }}
          />
          <Checkbox checked={includeMuted} onChange={(e) => setIncludeMuted(e.target.checked)}>
            包含已屏蔽
          </Checkbox>
        </Space>
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
        {beforeNo !== null && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            只在第 {beforeNo} 章之前的记忆中检索（生成时还会排除前情里已给出全文或摘要的章节）
          </Typography.Text>
        )}
        {result && (
          <>
            <SearchResultSummary result={result} />
            {result.hits.length === 0 ? (
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有达到最低分数的结果" />
            ) : (
              <List
                size="small"
                dataSource={result.hits}
                rowKey={(h) => h.memory.id}
                renderItem={(h) => (
                  <List.Item>
                    <Space direction="vertical" size={4} style={{ width: '100%' }}>
                      <Space wrap size={4}>
                        <Typography.Text strong>第 {h.memory.chapter_no} 章</Typography.Text>
                        <Tag color={MEMORY_KIND_COLORS[h.memory.kind]} style={{ marginInlineEnd: 0 }}>
                          {MEMORY_KIND_LABELS[h.memory.kind]}
                        </Tag>
                        {h.memory.pinned && <Tag color="gold">已钉住</Tag>}
                        {h.memory.muted && <Tag>已屏蔽</Tag>}
                        <MemorySourceBadge memory={h.memory} />
                      </Space>
                      <ScoreTags score={h.score} vectorScore={h.vector_score} keywordScore={h.keyword_score} />
                      <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
                        {h.memory.content}
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
