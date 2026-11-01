import { Alert, App, Button, Modal, Select, Space, Typography } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { adminApi } from '../../api/admin'
import { roleApi } from '../../api/roles'
import { errorMessage } from '../../api/client'
import type { AdminUser, RoleBrief } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useAuthorizationScope } from '../../hooks/useAuthorizationScope'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey, isSuperAdmin } from '../../utils/permissions'

function Assignment({ user, onClose, onChanged }: { user: AdminUser; onClose: () => void; onChanged: () => void }) {
  const { message } = App.useApp()
  const scope = useAuthorizationScope(undefined, true)
  const [query, setQuery] = useState('')
  const targetLoader = useCallback(() => adminApi.getUser(user.id), [user.id])
  const rolesLoader = useCallback(() => roleApi.list({ q: query, page_size: 100 }), [query])
  const { data: target, loading: targetLoading, error: targetError } = useLoader(targetLoader)
  const { data: roles, loading, error } = useLoader(rolesLoader)
  const [chosen, setChosen] = useState<RoleBrief[]>([])
  const [selected, setSelected] = useState<number[] | null>(null)
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const [failure, setFailure] = useState<string | null>(null)
  const roleIds = selected ?? target?.roles?.map((role) => role.id) ?? []
  const options = [...chosen, ...(target?.roles ?? []), ...(roles?.items ?? [])].filter((role, index, rows) => rows.findIndex((other) => other.id === role.id) === index)
  const selfSuper = target?.id === useAuthStore.getState().user?.id && isSuperAdmin(target)
  const superId = options.find((role) => role.system_key === 'super_admin')?.id
  const ownSuperRemoved = selfSuper && (superId === undefined || !roleIds.includes(superId))
  const submit = async () => {
    if (!target || pending.current || !scope.allowed() || ownSuperRemoved) return
    pending.current = true
    setBusy(true)
    setFailure(null)
    const ticket = scope.begin()
    try {
      await roleApi.assign(target.id, { authorization_revision: target.authorization_revision, role_ids: roleIds })
      if (!scope.current(ticket)) return
      void message.success('角色分配已保存，后续请求按新权限校验')
      onChanged()
      onClose()
      void useAuthStore.getState().refreshAuthorization()
    } catch (err) { if (scope.current(ticket)) setFailure(errorMessage(err)) }
    finally { if (scope.current(ticket)) { pending.current = false; setBusy(false) } }
  }
  return <Modal open title={`分配「${user.username}」的角色`} onCancel={onClose} onOk={() => void submit()} okText="保存分配" cancelText="取消" confirmLoading={busy} closable={!busy} maskClosable={!busy} cancelButtonProps={{ disabled: busy }} okButtonProps={{ disabled: loading || targetLoading || !target || ownSuperRemoved }}>
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Alert type="info" showIcon message="可同时拥有多个角色，权限取并集。清空选择后保留普通用户的个人资源能力。" />
      {(error || targetError || failure) && <Alert type="error" showIcon message={failure ?? error ?? targetError} />}
      <Select mode="multiple" style={{ width: '100%' }} aria-label="用户角色" placeholder="选择角色（可多选）" value={roleIds} loading={loading} disabled={busy || !target} options={options.map((role) => ({ label: role.name, value: role.id }))} showSearch filterOption={false} onSearch={setQuery} onChange={(ids) => { setSelected(ids); setChosen(options.filter((role) => ids.includes(role.id))) }} allowClear />
      {(roles?.total ?? 0) > 100 && <Typography.Text type="secondary">角色较多，请输入名称搜索；已选择的角色不会因分页被丢弃。</Typography.Text>}
      {ownSuperRemoved && <Alert type="warning" message="不能移除自己的超级管理员身份。" />}
      <Typography.Text type="secondary">服务器会保护最后一名启用超管。出现版本冲突时请关闭后重新核实，不会自动重发。</Typography.Text>
    </Space>
  </Modal>
}

export default function UserRolesButton({ user, onChanged, disabled = false }: { user: AdminUser; onChanged: () => void; disabled?: boolean }) {
  const actor = useAuthStore((state) => state.user)
  const identity = `${actor?.id}:${authorizationKey(actor)}`
  const [openedAt, setOpenedAt] = useState<string | null>(null)
  if (!isSuperAdmin(actor)) return null
  return <><Button type="link" size="small" disabled={disabled} onClick={() => setOpenedAt(identity)}>分配角色</Button>{openedAt === identity && <Assignment user={user} onChanged={onChanged} onClose={() => setOpenedAt(null)} />}</>
}
