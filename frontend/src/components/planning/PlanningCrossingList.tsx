import { Alert, Button, Drawer, Empty, List, Pagination, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { planningApi } from '../../api/planning'
import type { PlanningRow, SubplotCrossing } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useMentionQuery as usePlanningQuery } from '../../hooks/useMentionQuery'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { planningRowLabel } from '../../utils/planning'

interface Props { projectId: number; row?: PlanningRow; onClose: () => void; onOpen: (event: SubplotCrossing, revision: number) => void }
export default function PlanningCrossingList({ projectId, row, onClose, onOpen }: Props) {
  const [page, setPage] = useState(1)
  const sourceKind = row?.source.kind, sourceId = row?.source.id
  const load = useCallback((signal: AbortSignal) => planningApi.crossings(projectId, { page, page_size: 20, source_kind: sourceKind, source_id: sourceId }, signal), [projectId, page, sourceKind, sourceId])
  const result = usePlanningQuery(projectId, `crossings:${sourceKind}:${sourceId}:${page}`, load)
  useProjectRefresh(projectId, ['planning'], result.reload)
  const width = useModalWidth(680)
  return <Drawer open title={row ? `明确交汇 · ${planningRowLabel(row)}` : '全部明确交汇事件'} width={width} onClose={onClose}>
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {result.loading && <Spin />}
      {result.error && <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}
      {result.data && <><List dataSource={result.data.items} locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无明确交汇事件" /> }} renderItem={(event) => <List.Item style={{ display: 'block' }}>
        <Button type="link" onClick={() => onOpen(event, result.data!.revision)}>◆ {event.title}</Button><Tag>{event.subplot_ids.length} 条参与支线</Tag>
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{event.description}</Typography.Paragraph>
        <Typography.Text type="secondary">{event.source.kind === 'chapter' ? '章节' : event.source.kind === 'plan' ? '细纲' : '待关联规划'}记录 #{event.source.id}</Typography.Text>
      </List.Item>} /><Pagination current={page} pageSize={20} total={result.data.total} onChange={setPage} showSizeChanger={false} /></>}
    </Space>
  </Drawer>
}
