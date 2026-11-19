import { Descriptions, Drawer, Space, Tag, Typography } from 'antd'
import {
  COLLAB_AUDIT_ACTION,
  COLLAB_AUDIT_ERROR,
  COLLAB_AUDIT_RESULT,
  type CollaborationAuditEvent,
} from '../../api/collaborationAudit'
import { useModalWidth } from '../../hooks/useIsMobile'
import {
  collaborationAuditSummaryRows,
  collaborationResourceLabel,
  collaborationTargetLabel,
  collaborationUserLabel,
} from '../../utils/collaborationAudit'
import { formatTime } from '../../utils/time'

interface DetailContentProps {
  event: CollaborationAuditEvent
  ownerView?: boolean
}

/** 协作审计只读详情内容；严格只展示安全快照与白名单摘要，无写操作。 */
export function CollaborationAuditDetailContent({ event, ownerView = false }: DetailContentProps) {
  const result = COLLAB_AUDIT_RESULT[event.result]
  const summaryRows = collaborationAuditSummaryRows(event)
  return <Space direction="vertical" size={16} style={{ width: '100%', overflowWrap: 'anywhere' }}>
    <Descriptions size="small" column={1} bordered>
      <Descriptions.Item label="记录 ID">#{event.id}</Descriptions.Item>
      <Descriptions.Item label="发生时间">{formatTime(event.occurred_at, 'YYYY-MM-DD HH:mm:ss.SSS [UTC]Z')}</Descriptions.Item>
      {!ownerView && <Descriptions.Item label="作用范围与资源快照">{collaborationResourceLabel(event)}</Descriptions.Item>}
      <Descriptions.Item label="操作人快照">{collaborationUserLabel(event.actor_user_id, event.actor_username)}</Descriptions.Item>
      <Descriptions.Item label="目标成员快照">{collaborationTargetLabel(event, { ownerView })}</Descriptions.Item>
      <Descriptions.Item label="动作">
        {COLLAB_AUDIT_ACTION[event.action]}
        <br />
        <Typography.Text type="secondary">{event.action}</Typography.Text>
      </Descriptions.Item>
      <Descriptions.Item label="结果">
        <Tag color={result.color}>{result.label}</Tag>
      </Descriptions.Item>
      {event.error_code && <Descriptions.Item label="失败原因">
        {COLLAB_AUDIT_ERROR[event.error_code]}
        <br />
        <Typography.Text type="secondary">{event.error_code}</Typography.Text>
      </Descriptions.Item>}
      {event.invitation_id && <Descriptions.Item label="邀请轮次标识">{event.invitation_id}</Descriptions.Item>}
      <Descriptions.Item label="操作标识">{event.operation_id}</Descriptions.Item>
    </Descriptions>
    <div>
      <Typography.Title level={5} style={{ marginTop: 0 }}>权限与状态摘要</Typography.Title>
      <Typography.Paragraph type="secondary">
        {ownerView
          ? '仅展示本资源合法参与方的操作及安全摘要，不含正文、提示词或密钥；不包含其他用户的越权尝试，邀请不可用账号时不区分账号不存在或已禁用。'
          : '独立只追加保留，不含正文、提示词或密钥；不受「管理操作日志」按日期清理影响。作品、系列或用户删除后快照仍保留。'}
      </Typography.Paragraph>
      {summaryRows.length > 0
        ? <Descriptions size="small" column={1} bordered
            items={summaryRows.map((row, index) => ({ key: index, label: row.label, children: row.value }))} />
        : <Typography.Text type="secondary">本次操作无状态变更摘要。</Typography.Text>}
    </div>
  </Space>
}

interface DrawerProps {
  event: CollaborationAuditEvent | null
  ownerView?: boolean
  onClose: () => void
}

export default function CollaborationAuditDetailDrawer({ event, ownerView = false, onClose }: DrawerProps) {
  const width = useModalWidth(640)
  return <Drawer open={event !== null} title="协作审计详情（只读）" width={width} onClose={onClose} destroyOnHidden>
    {event && <CollaborationAuditDetailContent event={event} ownerView={ownerView} />}
  </Drawer>
}
