import { Alert, Button, Card, Drawer, Form, Input, Popconfirm, Radio, Space, Tag, theme, Tooltip, Typography } from 'antd'
import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import type { ConsistencyIssue, IssueEvidence, IssueStatus } from '../../api/consistency'
import { useModalWidth } from '../../hooks/useIsMobile'
import {
  evidenceChapterLabel, evidenceLink, isDraftDirty, ISSUE_STATUS, NOTE_LIMIT, type IssueDraft,
} from '../../utils/consistency'
import { formatTime } from '../../utils/time'
import { IssueTags } from './IssueList'
import { statusFocusTarget } from './statusFocus'

function EvidenceCard({ projectId, evidence, canEdit }: { projectId: number; evidence: IssueEvidence; canEdit: boolean }) {
  const link = evidenceLink(projectId, evidence)
  const date = evidence.story_time
  const { token } = theme.useToken()
  return <Card size="small" title={evidenceChapterLabel(evidence)} styles={{ body: { minWidth: 0 } }}>
    <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
      <blockquote style={{ margin: 0, paddingInlineStart: 12, borderInlineStart: `3px solid ${token.colorBorder}` }}>{evidence.quote || '（无引句）'}</blockquote>
    </Typography.Paragraph>
    <Space size={[8, 4]} wrap style={{ marginTop: 8 }}>
      {date && <Tag>{date.source === 'unknown' ? '日期依据不明' : date.source === 'unmarked' ? '未标时间'
        : `检查时：${date.label} · ${date.source === 'scene' ? '场景指定' : '沿用本章'}`}</Tag>}
      {evidence.story_time_stale && <Tag color="warning">日期或场景归属已变化</Tag>}
      {link.notes.map((note) => <Tag key={note} color={link.kind === 'deleted' ? 'default' : 'warning'} style={{ whiteSpace: 'normal' }}>{note}</Tag>)}
      {link.kind !== 'deleted' && <>
        <Link to={link.readerHref}>{link.kind === 'anchored' ? '在阅读器中定位' : '在阅读器中打开该章'}</Link>
        {canEdit && <Link to={link.editorHref}>{link.kind === 'anchored' ? '在编辑器中定位' : '在编辑器中打开该章'}</Link>}
      </>}
    </Space>
  </Card>
}

interface Props {
  projectId: number
  issue: ConsistencyIssue | null
  draft: IssueDraft | null
  conflict: boolean
  saving: boolean
  refreshing: boolean
  rechecking: boolean
  canUpdate: boolean
  canRecheck: boolean
  /** 不能重新核查的原因（已有检查在进行、未配置模型等）；空串 = 可以 */
  recheckBlocked: string
  onDraft: (draft: IssueDraft) => void
  onSave: () => void
  onRefresh: () => void
  onRecheck: () => void
  onClose: () => void
  /** 抽屉关闭动画结束后调用（页面把焦点还给打开它的列表条目） */
  onClosed?: () => void
}

/** 问题详情：说明、修改建议、证据卡片与跳转、状态与备注表单（CAS，冲突保留本地输入）、重新核查。 */
export default function IssueDrawer({
  projectId, issue, draft, conflict, saving, refreshing, rechecking, canUpdate, canRecheck, recheckBlocked,
  onDraft, onSave, onRefresh, onRecheck, onClose, onClosed,
}: Props) {
  const width = useModalWidth(720)
  // 保存完成后「保存」按钮因没有未保存修改而禁用，浏览器会把焦点丢到页面主体（Esc 关不掉抽屉）：
  // 此时把焦点放回抽屉内的状态选项；用户已把焦点移到别处时不打扰。
  const formRef = useRef<HTMLDivElement>(null)
  const wasSaving = useRef(false)
  useEffect(() => {
    const finished = wasSaving.current && !saving
    wasSaving.current = saving
    if (!finished) return
    const root = formRef.current
    const active = document.activeElement
    const lost = !active || active === document.body || (active instanceof HTMLButtonElement && active.disabled && !!root?.contains(active))
    if (root && lost) statusFocusTarget(root)?.focus()
  }, [saving])
  const dirty = !!draft && isDraftDirty(draft)
  const hasLiveEvidence = !!issue && issue.evidence.some((item) => item.chapter_id !== null && item.chapter_no !== null)
  const blocked = recheckBlocked || (hasLiveEvidence ? '' : '涉及的章节均已删除，无法重新核查')
  return <Drawer open={issue !== null} width={width} title="潜在冲突详情" onClose={onClose} destroyOnHidden
    afterOpenChange={(open) => { if (!open) onClosed?.() }}
    extra={<Button onClick={onRefresh} loading={refreshing}>刷新</Button>}>
    {issue && draft && <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
      <Typography.Title level={4} style={{ margin: 0, overflowWrap: 'anywhere' }}>{issue.title || '未命名问题'}</Typography.Title>
      <IssueTags issue={issue} />
      <Typography.Text type="secondary">由 AI 根据各章事实判断，属于潜在冲突，仅供参考；是否修改由你决定。</Typography.Text>
      {issue.needs_recheck && <Alert type="warning" showIcon message="需重新核查"
        description="检查后有证据发生变化，请核对下方标记的正文、日期或场景归属。" />}
      {issue.reappeared && <Alert type="warning" showIcon message="再次出现" description="该问题曾标记为已解决，最近一次检查再次检出，已重新变为待处理。" />}
      {issue.not_reproduced && <Alert type="info" showIcon message="本次未复现" description="最近一次检查没有再检出这个矛盾；状态不会自动改变，确认后可自行标记为已解决。" />}
      <section aria-label="说明">
        <Typography.Title level={5}>说明</Typography.Title>
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{issue.description || '（无）'}</Typography.Paragraph>
      </section>
      <section aria-label="修改建议">
        <Typography.Title level={5}>修改建议</Typography.Title>
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{issue.suggestion || '（无）'}</Typography.Paragraph>
      </section>
      <section aria-label="原文证据">
        <Typography.Title level={5}>原文证据</Typography.Title>
        <Space direction="vertical" size="small" style={{ width: '100%', minWidth: 0 }}>
          {issue.evidence.length === 0 && <Typography.Text type="secondary">没有记录证据</Typography.Text>}
          {issue.evidence.map((evidence, index) => <EvidenceCard key={`${evidence.chapter_id}:${evidence.fact_id}:${index}`}
            projectId={projectId} evidence={evidence} canEdit={canUpdate} />)}
        </Space>
      </section>
      <section aria-label="状态与备注">
        <Typography.Title level={5}>状态与备注</Typography.Title>
        {canUpdate ? <div ref={formRef}><Form layout="vertical" onFinish={onSave}>
          <Form.Item label="状态">
            <Radio.Group value={draft.status} disabled={saving} onChange={(event) => onDraft({ ...draft, status: event.target.value as IssueStatus })}
              options={(Object.keys(ISSUE_STATUS) as IssueStatus[]).map((value) => ({ value, label: ISSUE_STATUS[value].label }))} />
          </Form.Item>
          <Form.Item label="备注" htmlFor="consistency-issue-note">
            <Input.TextArea id="consistency-issue-note" value={draft.note} disabled={saving} maxLength={NOTE_LIMIT} showCount
              autoSize={{ minRows: 3, maxRows: 10 }} placeholder="记录处理方式或忽略原因" onChange={(event) => onDraft({ ...draft, note: event.target.value })} />
          </Form.Item>
          {conflict && <Alert type="error" showIcon style={{ marginBottom: 12 }} message="问题已被其他人修改"
            description="你填写的状态和备注仍保留在表单里。请先刷新读取最新内容，确认后再保存。"
            action={<Button onClick={onRefresh} loading={refreshing}>刷新</Button>} />}
          <Button type="primary" htmlType="submit" loading={saving} disabled={!dirty}>保存</Button>
        </Form></div> : <Space direction="vertical" size={4} style={{ width: '100%' }}>
          <Typography.Text>状态：{ISSUE_STATUS[issue.status].label}</Typography.Text>
          <Typography.Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>备注：{issue.note || '（无）'}</Typography.Paragraph>
          <Typography.Text type="secondary">当前为只读权限，不能修改状态与备注。</Typography.Text>
        </Space>}
        {issue.status_changed_at && <Typography.Paragraph type="secondary" style={{ margin: '8px 0 0' }}>状态最近修改于 {formatTime(issue.status_changed_at)}</Typography.Paragraph>}
      </section>
      {canRecheck && <section aria-label="重新核查">
        <Typography.Title level={5}>重新核查</Typography.Title>
        <Typography.Paragraph type="secondary">对这个问题涉及的现存章节重新运行一次检查，会调用模型（使用默认文本模型）。</Typography.Paragraph>
        {blocked ? <Tooltip title={blocked}><Button disabled>重新核查</Button></Tooltip>
          : <Popconfirm title="重新核查这个问题？" description="将对涉及章节重新抽取事实并比对，会调用模型。" okText="开始" cancelText="取消" onConfirm={onRecheck}>
            <Button loading={rechecking}>重新核查</Button>
          </Popconfirm>}
      </section>}
    </Space>}
  </Drawer>
}
