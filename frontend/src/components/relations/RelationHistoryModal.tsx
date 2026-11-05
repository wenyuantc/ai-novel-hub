import { Empty, List, Modal, Space, Spin, Tabs, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { errorMessage } from '../../api/client'
import { formatChangeValue, HISTORY_FIELD_LABELS, RELATION_SOURCE_LABELS, relationshipApi } from '../../api/relationships'
import type { Relationship, RelationshipHistory } from '../../api/types'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { formatTime } from '../../utils/time'
import RelationProgressionPanel from './RelationProgressionPanel'

interface Props { projectId: number; relationship: Relationship; onClose: () => void }
interface PairProps { projectId: number; pair: readonly [number, number]; relationshipId?: number }

/** 剧情变化和旧操作记录分开显示，旧审计始终只读。 */
export function RelationPairHistory({ projectId, pair, relationshipId }: PairProps) {
  return <Tabs items={[
    { key: 'story', label: '剧情变化', children: <RelationProgressionPanel key={`${projectId}:${pair[0]}:${pair[1]}`} projectId={projectId} pair={pair} /> },
    { key: 'audit', label: '操作记录（只读）', children: <RelationAuditHistory projectId={projectId} pair={pair} relationshipId={relationshipId} /> },
  ]} />
}

function RelationAuditHistory({ projectId, pair, relationshipId }: PairProps) {
  const { canRead, canReadNow } = useProjectAccess(projectId)
  const identity = useAuthStore((s) => `${s.status}:${s.user?.id ?? ''}:${authorizationKey(s.user)}`)
  const [a, b] = pair
  const key = `${projectId}:${a}:${b}:${relationshipId ?? ''}:${identity}`
  const [result, setResult] = useState<{ key: string; data: RelationshipHistory[]; error: string }>()
  useEffect(() => {
    if (!canRead || !canReadNow()) return
    let active = true
    const request = relationshipId === undefined ? relationshipApi.historyForPair(projectId, a, b) : relationshipApi.history(projectId, relationshipId)
    request.then(
      (data) => { if (active && canReadNow()) setResult({ key, data, error: '' }) },
      (err: unknown) => { if (active && canReadNow()) setResult({ key, data: [], error: errorMessage(err) }) },
    )
    return () => { active = false }
  }, [projectId, a, b, relationshipId, key, canRead, canReadNow])
  const current = canRead && result?.key === key ? result : undefined
  const data = current?.data ?? []
  const loading = canRead && !current
  return <>
    <Typography.Paragraph type="secondary">旧操作记录按记录时间显示，供查阅。</Typography.Paragraph>
    {current?.error && <Typography.Text type="danger">{current.error}</Typography.Text>}
    <Spin spinning={loading}>
      {data.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无操作记录" /> : <List dataSource={[...data].reverse()} renderItem={(h) => <List.Item>
        <Space direction="vertical" size={2} style={{ width: '100%' }}>
          <Space wrap size={4}><Typography.Text type="secondary">{formatTime(h.created_at)}</Typography.Text><Tag>{RELATION_SOURCE_LABELS[h.source] ?? h.source}</Tag>
            {h.chapter_no !== null && <Tag color="blue">第 {h.chapter_no} 章</Tag>}{h.note && <Typography.Text>{h.note}</Typography.Text>}</Space>
          {Object.entries(h.changes).map(([field, change]) => {
            const [oldV, newV] = Array.isArray(change) ? change : [undefined, change]
            return <Typography.Text key={field} style={{ fontSize: 13 }}>{HISTORY_FIELD_LABELS[field] ?? field}：{formatChangeValue(oldV)} → {formatChangeValue(newV)}</Typography.Text>
          })}
        </Space>
      </List.Item>} />}
    </Spin>
  </>
}

export default function RelationHistoryModal({ projectId, relationship, onClose }: Props) {
  return <Modal open title={`关系历史：${relationship.a_name} ↔ ${relationship.b_name}`} footer={null} width={640} onCancel={onClose}>
    <RelationPairHistory projectId={projectId} pair={[relationship.a_id, relationship.b_id]} relationshipId={relationship.id} />
  </Modal>
}
