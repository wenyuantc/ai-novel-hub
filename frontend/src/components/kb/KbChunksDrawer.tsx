import { Alert, Drawer, List, Pagination, Space, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { kbApi } from '../../api/kb'
import type { KbDocument } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'

interface Props {
  projectId: number
  document: KbDocument | null
  onClose: () => void
}

const PAGE_SIZE = 20

/** 文档分块查看（分页）：序号、标题路径、原文偏移与内容。 */
export default function KbChunksDrawer({ projectId, document, onClose }: Props) {
  return (
    <Drawer
      title={document ? `分块 · 《${document.filename}》` : '分块'}
      width={720}
      open={document !== null}
      onClose={onClose}
      destroyOnHidden
    >
      {document && <ChunkList key={document.id} projectId={projectId} document={document} />}
    </Drawer>
  )
}

function ChunkList({ projectId, document }: { projectId: number; document: KbDocument }) {
  const [page, setPage] = useState(1)
  const loader = useCallback(() => kbApi.chunks(projectId, document.id, page, PAGE_SIZE), [projectId, document.id, page])
  const { data, loading, error } = useLoader(loader)

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon message={error} />}
      {document.warnings.map((w) => (
        <Alert key={w} type="warning" showIcon message={w} />
      ))}
      <List
        loading={loading}
        dataSource={data?.items ?? []}
        rowKey="id"
        locale={{ emptyText: '还没有分块（文档可能仍在处理或处理失败）' }}
        renderItem={(c) => (
          <List.Item>
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space size={8} wrap>
                <Tag style={{ marginInlineEnd: 0 }}>#{c.seq}</Tag>
                {c.heading_path && <Typography.Text type="secondary">{c.heading_path}</Typography.Text>}
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {c.start_offset}–{c.end_offset} · {c.content.length} 字
                </Typography.Text>
              </Space>
              <Typography.Paragraph
                style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}
                ellipsis={{ rows: 6, expandable: true, symbol: '展开' }}
              >
                {c.content}
              </Typography.Paragraph>
            </Space>
          </List.Item>
        )}
      />
      {data && data.total > PAGE_SIZE && (
        <Pagination
          align="end"
          current={page}
          pageSize={PAGE_SIZE}
          total={data.total}
          showSizeChanger={false}
          onChange={setPage}
        />
      )}
    </Space>
  )
}
