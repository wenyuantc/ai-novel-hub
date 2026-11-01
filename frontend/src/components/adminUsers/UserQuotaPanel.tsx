import { draftOf, quotaConfig, quotaDescription } from '../../utils/quotas'
import type { Draft } from '../../utils/quotas'
import { Alert, App, Button, Card, InputNumber, Modal, Select, Space, Table, Typography } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { roleApi } from '../../api/roles'
import { errorMessage } from '../../api/client'
import type { QuotaItem, UserQuota } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useAuthorizationScope } from '../../hooks/useAuthorizationScope'
import { useAuthorizationCleanup } from '../../hooks/useAuthorizationCleanup'
import { useAuthStore } from '../../stores/auth'
import { can, isSuperAdmin } from '../../utils/permissions'


function QuotaEditor({ quota, onClose, onSaved }: { quota: UserQuota; onClose: () => void; onSaved: () => void }) {
  const { message } = App.useApp()
  const scope = useAuthorizationScope('quotas.update')
  const [project, setProject] = useState(() => draftOf(quota.project_count.config))
  const [tasks, setTasks] = useState(() => draftOf(quota.task_concurrency.config))
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const [error, setError] = useState<string | null>(null)
  const permitted = () => scope.allowed() && (!quota.is_management_account || isSuperAdmin(useAuthStore.getState().user))
  const submit = async () => {
    if (pending.current || !permitted()) return
    const projectConfig = quotaConfig(project)
    const taskConfig = quotaConfig(tasks)
    if (!projectConfig || !taskConfig) { setError('指定额度必须是0至2147483647之间的整数'); return }
    pending.current = true
    setBusy(true)
    setError(null)
    const ticket = scope.begin()
    try {
      await roleApi.updateQuotas(quota.user_id, { authorization_revision: quota.authorization_revision, revision: quota.revision, changes: { project_count: projectConfig, task_concurrency: taskConfig } })
      if (!scope.current(ticket) || !permitted()) return
      void message.success('用户配额已更新')
      onSaved()
      onClose()
    } catch (err) { if (scope.current(ticket)) setError(errorMessage(err)) }
    finally { if (scope.current(ticket)) { pending.current = false; setBusy(false) } }
  }
  const field = (label: string, draft: Draft, update: (next: Draft) => void) => <section><Typography.Paragraph strong>{label}</Typography.Paragraph><Space wrap><Select aria-label={`${label}模式`} value={draft.mode} disabled={busy} style={{ width: 190 }} options={[{ value: 'inherit', label: '继承系统默认' }, { value: 'limited', label: '指定数值' }, { value: 'unlimited', label: '不限额' }]} onChange={(mode) => update({ ...draft, mode })} />{draft.mode === 'limited' && <InputNumber aria-label={`${label}上限`} min={0} max={2_147_483_647} precision={0} value={draft.value} disabled={busy} onChange={(value) => update({ ...draft, value })} />}</Space></section>
  return <Modal open title={`配置「${quota.username}」的资源配额`} width={640} onOk={() => void submit()} onCancel={onClose} okText="保存配额" cancelText="取消" confirmLoading={busy} closable={!busy} maskClosable={!busy} cancelButtonProps={{ disabled: busy }}>
    <Space direction="vertical" size={20} style={{ width: '100%' }}>
      <Alert showIcon type="info" message="0表示禁止新建作品或启动新后台任务，不是不限额。降低配额不会删除数据或中断运行任务。" />
      {error && <Alert type="error" showIcon message={error} />}
      {field('作品总数', project, setProject)}{field('后台任务并发', tasks, setTasks)}
      <Typography.Text type="secondary">作品包含回收站与删除中记录。用户设置不限额也不能绕过全站任务并发上限（{quota.global_task_limit}）。版本冲突或结果未知时请先刷新核实，不自动重发。</Typography.Text>
    </Space>
  </Modal>
}

function Quotas({ userId }: { userId: number }) {
  const user = useAuthStore((state) => state.user)
  const loader = useCallback(() => roleApi.quotas(userId), [userId])
  const { data, loading, error, reload } = useLoader(loader)
  const [open, setOpen] = useState(false)
  useAuthorizationCleanup(() => setOpen(false))
  const editable = !!data && can(user, 'quotas.update') && (!data.is_management_account || isSuperAdmin(user))
  return <Card title="用户资源配额" extra={<Space><Button size="small" onClick={reload} loading={loading}>刷新用量</Button>{editable && <Button size="small" onClick={() => setOpen(true)}>配置配额</Button>}</Space>}>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
    <Table<{ key: string; label: string; item: QuotaItem }> rowKey="key" loading={loading} pagination={false} scroll={{ x: 480 }} dataSource={data ? [{ key: 'project_count', label: '作品总数（含回收站/删除中）', item: data.project_count }, { key: 'task_concurrency', label: '后台并发占用', item: data.task_concurrency }] : []} columns={[{ title: '资源', dataIndex: 'label' }, { title: '当前占用', key: 'used', render: (_, row) => row.item.used }, { title: '有效上限与来源', key: 'limit', render: (_, row) => quotaDescription(row.item) }]} />
    {data && <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>后台任务达到上限后继续排队；即使超管也遵守配额。全站并发硬上限：{data.global_task_limit}。</Typography.Paragraph>}
    {data?.is_management_account && !isSuperAdmin(user) && <Alert type="info" showIcon message="该账号具有管理权限，仅超管可修改其配额。" style={{ marginTop: 12 }} />}
    {open && editable && data && <QuotaEditor quota={data} onClose={() => setOpen(false)} onSaved={reload} />}
  </Card>
}

export default function UserQuotaPanel({ userId }: { userId: number }) {
  const user = useAuthStore((state) => state.user)
  return can(user, 'quotas.read') ? <Quotas key={`${userId}:${user?.id}`} userId={userId} /> : null
}
