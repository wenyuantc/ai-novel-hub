import { Alert, Button, Checkbox, Input, Modal, Select, Space, Spin } from 'antd'
import { useCallback, useState } from 'react'
import {
  collaborationApi, type CollaborationMember, type CollaborationRole, type CollaborationScope,
} from '../../api/collaboration'
import { useCollaborationFacts } from './useCollaborationFacts'
import './collaboration.css'

export interface MemberPanelProps {
  scope: CollaborationScope
  resourceId: number
  canManage: boolean
  onChanged?: () => void
}

type Draft = { username: string; role: CollaborationRole; allowOwnerAi: boolean; identityKey: string }
type Intent =
  | { kind: 'invite'; draft: Draft; identityKey: string }
  | { kind: 'update' | 'revoke'; member: CollaborationMember; role: CollaborationRole; allowOwnerAi: boolean; identityKey: string }

const ROLE_OPTIONS = [{ value: 'viewer', label: '查看者' }, { value: 'editor', label: '编辑者' }]
const STATUS_LABELS: Record<CollaborationMember['status'], string> = {
  pending: '待接受', accepted: '已加入', declined: '已拒绝', revoked: '已撤销',
}
const roleLabel = (role: CollaborationRole) => role === 'editor' ? '编辑者' : '查看者'
const feeDescription = '允许使用作者的模型配置和 API 密钥，可能产生作者的外部服务费用。'

export function MemberRows({ members, scope, disabled, onEdit, onRevoke }: {
  members: CollaborationMember[]
  scope: CollaborationScope
  disabled: boolean
  onEdit: (member: CollaborationMember) => void
  onRevoke: (member: CollaborationMember) => void
}) {
  if (!members.length) return <p className="collaboration-empty">还没有邀请协作者。</p>
  return <ul className="collaboration-members">{members.map((member) => {
    const active = member.status === 'pending' || member.status === 'accepted'
    return <li key={member.user_id} className="collaboration-member">
      <div className="collaboration-member-copy">
        <strong>{member.username}</strong><span>{scope === 'series' ? '设定协作者' : roleLabel(member.role)} · {STATUS_LABELS[member.status]}</span>
        {scope === 'project' && <span>{member.allow_owner_ai ? '已允许使用作者模型' : '未允许使用作者模型'}</span>}
      </div>
      {active && <Space wrap>
        {scope === 'project' && <Button size="small" disabled={disabled} onClick={() => onEdit(member)}>修改权限</Button>}
        <Button size="small" danger disabled={disabled} onClick={() => onRevoke(member)}>撤销</Button>
      </Space>}
    </li>
  })}</ul>
}

/** 作者修改或撤销已加入成员的作品权限时，后端会终止其进行中的 AI 任务（含智能体轮次，E4i-c 复核带入）。 */
export const MEMBER_AI_ABORT_NOTICE = '修改或撤销该成员的权限后，会立即中止其进行中的 AI 任务（包括智能体对话），已提交的内容会保留。'

export function MemberChangeNotice({ scope, member }: { scope: CollaborationScope; member: Pick<CollaborationMember, 'status'> }) {
  if (scope !== 'project' || member.status !== 'accepted') return null
  return <Alert type="warning" showIcon message={MEMBER_AI_ABORT_NOTICE} />
}

export default function MemberPanel({ scope, resourceId, canManage, onChanged }: MemberPanelProps) {
  const read = useCallback((signal: AbortSignal) => collaborationApi.members(scope, resourceId, signal), [scope, resourceId])
  const facts = useCollaborationFacts(`members:${scope}:${resourceId}`, canManage, read, onChanged)
  const [storedDraft, setStoredDraft] = useState<Draft>({ username: '', role: 'viewer', allowOwnerAi: false, identityKey: '' })
  const [storedIntent, setStoredIntent] = useState<Intent | null>(null)
  const draft = storedDraft.identityKey === facts.identityKey
    ? storedDraft : { username: '', role: 'viewer' as const, allowOwnerAi: false, identityKey: facts.identityKey }
  const changeDraft = (changes: Partial<Draft>) => setStoredDraft({ ...draft, ...changes, identityKey: facts.identityKey })
  const intent = facts.data !== null && storedIntent?.identityKey === facts.identityKey ? storedIntent : null
  const originalMember = intent && intent.kind !== 'invite' ? intent.member : null
  const liveMember = originalMember ? facts.data?.find((member) => member.user_id === originalMember.user_id) : null
  const sameInvitation = !!liveMember && !!originalMember && liveMember.invitation_id === originalMember.invitation_id
  const sameRevision = sameInvitation && liveMember?.revision === originalMember?.revision
  const memberActive = liveMember?.status === 'pending' || liveMember?.status === 'accepted'
  const duplicate = intent?.kind === 'invite' && facts.data?.some((member) =>
    member.username.toLocaleLowerCase() === intent.draft.username.toLocaleLowerCase()
      && (member.status === 'pending' || member.status === 'accepted'))
  const changing = facts.busy || facts.loading || facts.stale || facts.data === null
  const cannotConfirm = changing || (intent?.kind === 'invite' ? !!duplicate : !sameRevision || !memberActive)

  const edit = (kind: 'update' | 'revoke', member: CollaborationMember) => setStoredIntent({
    kind, member, role: member.role, allowOwnerAi: !!member.allow_owner_ai, identityKey: facts.identityKey,
  })
  const confirm = async () => {
    if (!intent || cannotConfirm) return
    const success = intent.kind === 'invite'
      ? await facts.mutate((signal) => scope === 'project'
        ? collaborationApi.inviteProject(resourceId, {
          username: intent.draft.username, role: intent.draft.role, allow_owner_ai: intent.draft.allowOwnerAi,
        }, signal)
        : collaborationApi.inviteSeries(resourceId, intent.draft.username, signal), '邀请已发送。')
      : intent.kind === 'update'
        ? await facts.mutate((signal) => collaborationApi.updateProject(resourceId, intent.member.user_id, {
          expected_revision: intent.member.revision, role: intent.role, allow_owner_ai: intent.allowOwnerAi,
        }, signal), '成员权限已更新。')
        : await facts.mutate((signal) => collaborationApi.revoke(
          scope, resourceId, intent.member.user_id, intent.member.revision, signal,
        ), '成员权限已撤销。')
    if (success && facts.isCurrent()) {
      if (intent.kind === 'invite') changeDraft({ username: '' })
      setStoredIntent(null)
    }
  }
  if (!canManage) return null
  const title = intent?.kind === 'invite' ? '确认发送邀请' : intent?.kind === 'update' ? '确认修改权限' : '确认撤销权限'
  return <section className="collaboration-panel" aria-label={scope === 'project' ? '作品成员' : '系列成员'} aria-busy={facts.loading}>
    <div className="collaboration-heading"><h3>{scope === 'project' ? '作品成员' : '系列设定协作者'}</h3>
      <Button size="small" disabled={facts.busy} onClick={facts.refresh}>刷新</Button></div>
    {facts.error && <Alert type="error" showIcon message={facts.error} action={<Button size="small" disabled={facts.busy} onClick={facts.refresh}>重新加载</Button>} />}
    {facts.notice && <Alert type="info" showIcon message={facts.notice} />}
    <div className="collaboration-invite-form">
      <Input aria-label="邀请用户名" placeholder="输入准确的用户名" maxLength={64} value={draft.username}
        onChange={(event) => changeDraft({ username: event.target.value })} disabled={changing} />
      {scope === 'project' && <Select aria-label="邀请角色" value={draft.role} options={ROLE_OPTIONS}
        onChange={(role: CollaborationRole) => changeDraft({ role })} disabled={changing} />}
      <Button type="primary" disabled={changing || !draft.username.trim()} onClick={() => setStoredIntent({
        kind: 'invite', draft: { ...draft, username: draft.username.trim() }, identityKey: facts.identityKey,
      })}>邀请</Button>
    </div>
    {scope === 'project' && <div className="collaboration-fee-choice">
      <Checkbox checked={draft.allowOwnerAi} disabled={changing} onChange={(event) => changeDraft({ allowOwnerAi: event.target.checked })}>
        允许使用作者模型配置
      </Checkbox><span>{feeDescription}</span>
    </div>}
    {scope === 'series' && <p className="collaboration-hint">系列协作者只能编辑共享设定，不因此获得系列内作品的访问权。</p>}
    {facts.data && <Spin spinning={facts.loading}><MemberRows members={facts.data} scope={scope} disabled={changing}
      onEdit={(member) => edit('update', member)} onRevoke={(member) => edit('revoke', member)} /></Spin>}
    {facts.loading && !facts.data && <p role="status">正在加载成员…</p>}
    <Modal title={title} open={!!intent} okText={intent?.kind === 'invite' ? '发送邀请' : intent?.kind === 'update' ? '保存权限' : '确认撤销'}
      cancelText="取消" confirmLoading={facts.busy} okButtonProps={{ disabled: cannotConfirm }} onOk={() => void confirm()}
      onCancel={() => { if (!facts.busy) setStoredIntent(null) }}>
      {intent?.kind === 'invite' && <>
        <p>邀请「{intent.draft.username}」成为{scope === 'project' ? roleLabel(intent.draft.role) : '系列设定协作者'}？</p>
        {scope === 'project' && <p>{intent.draft.allowOwnerAi ? feeDescription : '不允许使用作者模型配置。'}</p>}
        {duplicate && <Alert type="warning" showIcon message="该用户已有待处理邀请或有效成员身份，请先核对成员列表。" />}
      </>}
      {intent && intent.kind !== 'invite' && <>
        <p>{intent.kind === 'revoke' ? `撤销「${intent.member.username}」的邀请或访问权限？` : `修改「${intent.member.username}」的作品权限。`}</p>
        {intent.kind === 'update' && <Space direction="vertical" className="collaboration-modal-fields">
          <Select aria-label="成员角色" value={intent.role} options={ROLE_OPTIONS} disabled={changing}
            onChange={(role: CollaborationRole) => setStoredIntent({ ...intent, role })} />
          <Checkbox checked={intent.allowOwnerAi} disabled={changing}
            onChange={(event) => setStoredIntent({ ...intent, allowOwnerAi: event.target.checked })}>允许使用作者模型配置</Checkbox>
          <span>{feeDescription}</span>
        </Space>}
        <MemberChangeNotice scope={scope} member={intent.member} />
        {!sameRevision && <Alert type="warning" showIcon message="成员状态已变化，请核对最新信息。"
          description={sameInvitation && memberActive && liveMember
            ? <Button size="small" onClick={() => setStoredIntent({ ...intent, member: liveMember })}>采用最新状态继续核对</Button>
            : '当前邀请已更换或失效，请关闭并从成员列表重新选择。'} />}
      </>}
    </Modal>
  </section>
}
