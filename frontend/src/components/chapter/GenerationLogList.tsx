import { App, Button, Card, Descriptions, Modal, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { CSSProperties } from 'react'
import { useCallback, useState } from 'react'
import { GENERATION_KIND_LABELS, GENERATION_STATUS, generationLogApi } from '../../api/chapterVersions'
import { errorMessage } from '../../api/client'
import type { GenerationLog, GenerationLogBrief } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'

interface Props {
  pid: number
  chapterId: number
}

const pre: CSSProperties = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  margin: 0,
  maxHeight: 360,
  overflow: 'auto',
  fontSize: 12,
  lineHeight: 1.6,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
}

const ROLE_LABELS: Record<string, string> = { system: '系统', user: '用户', assistant: '助手' }

function formatDuration(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`
}

/** 本章生成记录：列表 + 详情（参数与完整提示词，只读）。 */
export default function GenerationLogList({ pid, chapterId }: Props) {
  const { message } = App.useApp()
  const loader = useCallback(() => generationLogApi.list(pid, chapterId), [pid, chapterId])
  const { data: items = [], loading } = useLoader(loader)
  const [detail, setDetail] = useState<GenerationLog | null>(null)
  const [loadingId, setLoadingId] = useState<number | null>(null)

  const openDetail = async (id: number) => {
    setLoadingId(id)
    try {
      setDetail(await generationLogApi.get(pid, chapterId, id))
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setLoadingId(null)
    }
  }

  const columns: ColumnsType<GenerationLogBrief> = [
    { title: '时间', dataIndex: 'created_at', width: 140, render: (t: string) => formatTime(t) },
    { title: '类型', dataIndex: 'kind', width: 90, render: (k: GenerationLogBrief['kind']) => GENERATION_KIND_LABELS[k] },
    {
      title: '模型',
      dataIndex: 'model',
      ellipsis: true,
      render: (m: string, r) => (
        <Tooltip title={`${r.provider} / ${m}`}>
          <span>{m || '-'}</span>
        </Tooltip>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 80,
      render: (s: GenerationLogBrief['status'], r) => {
        const tag = <Tag color={GENERATION_STATUS[s].color}>{GENERATION_STATUS[s].label}</Tag>
        return r.error ? <Tooltip title={r.error}>{tag}</Tooltip> : tag
      },
    },
    { title: '字数', dataIndex: 'output_chars', width: 70 },
    { title: '耗时', dataIndex: 'duration_ms', width: 80, render: (ms: number) => formatDuration(ms) },
    {
      title: '',
      key: 'detail',
      width: 70,
      render: (_, r) => (
        <Button type="link" size="small" loading={loadingId === r.id} onClick={() => void openDetail(r.id)}>
          详情
        </Button>
      ),
    },
  ]

  return (
    <>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={items}
        columns={columns}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        locale={{ emptyText: '暂无生成记录' }}
      />
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        每个项目保留最近 200 条记录；token 用量暂未统计。
      </Typography.Text>
      <Modal
        open={detail !== null}
        title={detail ? `生成记录 #${detail.id}` : ''}
        width={900}
        footer={null}
        onCancel={() => setDetail(null)}
      >
        {detail && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="类型">{GENERATION_KIND_LABELS[detail.kind]}</Descriptions.Item>
              <Descriptions.Item label="状态">
                <Tag color={GENERATION_STATUS[detail.status].color}>{GENERATION_STATUS[detail.status].label}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="模型">
                {detail.provider} / {detail.model}
              </Descriptions.Item>
              <Descriptions.Item label="时间">{formatTime(detail.created_at, 'YYYY-MM-DD HH:mm:ss')}</Descriptions.Item>
              <Descriptions.Item label="产出字数">{detail.output_chars}</Descriptions.Item>
              <Descriptions.Item label="耗时">{formatDuration(detail.duration_ms)}</Descriptions.Item>
              <Descriptions.Item label="Token（输入 / 输出）" span={2}>
                {detail.prompt_tokens ?? '-'} / {detail.completion_tokens ?? '-'}
              </Descriptions.Item>
              {detail.error && (
                <Descriptions.Item label="错误 / 警告" span={2}>
                  <Typography.Text type="danger">{detail.error}</Typography.Text>
                </Descriptions.Item>
              )}
            </Descriptions>
            <Card size="small" title="参数">
              <pre style={pre}>{JSON.stringify(detail.params, null, 2)}</pre>
            </Card>
            {detail.messages.map((m, i) => (
              <Card key={i} size="small" title={`提示词 ${i + 1} · ${ROLE_LABELS[m.role] ?? m.role}`}>
                <pre style={pre}>{typeof m.content === 'string' ? m.content : JSON.stringify(m.content, null, 2)}</pre>
              </Card>
            ))}
          </Space>
        )}
      </Modal>
    </>
  )
}
