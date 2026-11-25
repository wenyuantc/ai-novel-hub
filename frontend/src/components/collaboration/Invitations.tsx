import { Alert, Button, Modal, Space, Spin } from 'antd'
import { useCallback, useState } from 'react'
import { collaborationApi, type CollaborationInvitation } from '../../api/collaboration'
import { useCollaborationFacts } from './useCollaborationFacts'
import './collaboration.css'

import { Link } from 'react-router-dom'
import { invalidateNotifications } from '../../api/notifications'
import UnavailableTarget from '../notifications/UnavailableTarget'

type ResponseAction = 'accept' | 'decline'
type Intent = { invitation: CollaborationInvitation; action: ResponseAction; identityKey: string }

export function InvitationRows({ invitations, disabled, onSelect }: {
  invitations: CollaborationInvitation[]
  disabled: boolean
  onSelect: (invitation: CollaborationInvitation, action: ResponseAction) => void
}) {
  return <ul className="collaboration-invitations">{invitations.map((invitation) => (
    <li className="collaboration-invitation" key={`${invitation.scope}:${invitation.invitation_id}`}>
      <div className="collaboration-invitation-copy">
        <strong>{invitation.scope === 'project' ? '作品' : '系列'} · {invitation.title}</strong>
        <span>邀请人：{invitation.owner} · {invitation.scope === 'series' ? '设定协作者' : invitation.role === 'editor' ? '编辑者' : '查看者'}</span>
        {invitation.scope === 'project' && <span>{invitation.allow_owner_ai
          ? '已允许使用作者模型配置，可能产生作者费用'
          : '未允许使用作者模型配置'}</span>}
      </div>
      <Space wrap>
        <Button disabled={disabled} onClick={() => onSelect(invitation, 'decline')}>拒绝</Button>
        <Button type="primary" disabled={disabled} onClick={() => onSelect(invitation, 'accept')}>接受</Button>
      </Space>
    </li>
  ))}</ul>
}

export default function Invitations({ onChanged, invitationId, invitationScope }: {
  onChanged: () => void
  invitationId: string | null
  invitationScope: string | null
}) {
  const read = useCallback((signal: AbortSignal) => collaborationApi.invitations(signal), [])
  const facts = useCollaborationFacts(`invitations:${invitationId ?? ''}`, true, read, onChanged)
  const [intent, setIntent] = useState<Intent | null>(null)
  const current = intent?.identityKey === facts.identityKey && facts.data?.some((item) =>
    item.scope === intent.invitation.scope && item.invitation_id === intent.invitation.invitation_id
      && item.revision === intent.invitation.revision) ? intent : null
  const choose = (invitation: CollaborationInvitation, action: ResponseAction) =>
    setIntent({ invitation, action, identityKey: facts.identityKey })
  const respond = async () => {
    if (!current) return
    const { invitation, action } = current
    const succeeded = await facts.mutate(
      (signal) => collaborationApi.respond(invitation.scope, invitation.invitation_id, action, invitation.revision, signal),
      action === 'accept' ? '邀请已接受。' : '邀请已拒绝。',
    )
    if (succeeded && facts.isCurrent()) {
      invalidateNotifications()
      setIntent(null)
    }
  }
  const visible = invitationId === null ? facts.data : facts.data?.filter((item) =>
    item.invitation_id === invitationId && item.scope === invitationScope)
  if (invitationId === null && !facts.loading && !facts.error && !facts.notice && facts.data?.length === 0) return null
  return <section className="collaboration-invitation-section" aria-label="待处理邀请" aria-busy={facts.loading}>
    <h2>待处理邀请</h2>
    {invitationId !== null && <Link to="/projects">查看全部邀请</Link>}
    {invitationId !== null && !facts.stale && visible?.length === 0 && <UnavailableTarget />}
    {facts.error && <Alert type="error" showIcon message={facts.error} action={<Button size="small" disabled={facts.busy} onClick={facts.refresh}>重新加载</Button>} />}
    {facts.notice && <Alert type="info" showIcon message={facts.notice} />}
    {visible && <Spin spinning={facts.loading}><InvitationRows invitations={visible}
      disabled={facts.busy || facts.loading || facts.stale} onSelect={choose} /></Spin>}
    {facts.loading && !facts.data && <p role="status">正在加载邀请…</p>}
    <Modal title={current?.action === 'accept' ? '确认接受邀请' : '确认拒绝邀请'} open={Boolean(current)}
      okText={current?.action === 'accept' ? '接受邀请' : '拒绝邀请'} cancelText="取消"
      confirmLoading={facts.busy} okButtonProps={{ disabled: facts.stale || facts.loading }}
      onOk={() => void respond()} onCancel={() => { if (!facts.busy) setIntent(null) }}>
      {current && <p>确定{current.action === 'accept' ? '接受' : '拒绝'} {current.invitation.owner} 对「{current.invitation.title}」的邀请？</p>}
    </Modal>
  </section>
}
