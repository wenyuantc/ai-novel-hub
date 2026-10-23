import { Empty, List, Modal, Space, Spin, Tag, Typography } from 'antd'
import { useCallback } from 'react'
import { formatChangeValue, HISTORY_FIELD_LABELS, RELATION_SOURCE_LABELS, relationshipApi } from '../../api/relationships'
import type { Relationship } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'

interface Props {
  projectId: number
  relationship: Relationship
  onClose: () => void
}

/** 关系变化历史（每次变化一条：第几章、来源、{字段: [旧, 新]}）。 */
export default function RelationHistoryModal({ projectId, relationship, onClose }: Props) {
  const loader = useCallback(() => relationshipApi.history(projectId, relationship.id), [projectId, relationship.id])
  const { data = [], loading, error } = useLoader(loader)
  return (
    <Modal open title={`关系历史：${relationship.a_name} ↔ ${relationship.b_name}`} footer={null} width={640} onCancel={onClose}>
      {error && <Typography.Text type="danger">{error}</Typography.Text>}
      <Spin spinning={loading}>
        {data.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史" />
        ) : (
          <List
            dataSource={[...data].reverse()}
            renderItem={(h) => (
              <List.Item>
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  <Space wrap size={4}>
                    <Typography.Text type="secondary">{formatTime(h.created_at)}</Typography.Text>
                    <Tag>{RELATION_SOURCE_LABELS[h.source] ?? h.source}</Tag>
                    {h.chapter_no !== null && <Tag color="blue">第 {h.chapter_no} 章</Tag>}
                    {h.note && <Typography.Text>{h.note}</Typography.Text>}
                  </Space>
                  {Object.entries(h.changes).map(([field, pair]) => {
                    const [oldV, newV] = Array.isArray(pair) ? pair : [undefined, pair]
                    return (
                      <Typography.Text key={field} style={{ fontSize: 13 }}>
                        {HISTORY_FIELD_LABELS[field] ?? field}：{formatChangeValue(oldV)} → {formatChangeValue(newV)}
                      </Typography.Text>
                    )
                  })}
                </Space>
              </List.Item>
            )}
          />
        )}
      </Spin>
    </Modal>
  )
}
