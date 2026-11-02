import { permissionSelection } from '../utils/roles'
import { PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Checkbox, Form, Input, List, Modal, Pagination, Result, Space, Table, Tag, Typography } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { roleApi } from '../api/roles'
import { errorMessage } from '../api/client'
import type { PermissionCode, PermissionDefinition, Role } from '../api/types'
import { useLoader } from '../hooks/useLoader'
import { useAuthorizationScope } from '../hooks/useAuthorizationScope'
import { useIsMobile } from '../hooks/useIsMobile'
import { useAuthStore } from '../stores/auth'
import { authorizationKey, isSuperAdmin } from '../utils/permissions'
import { formatTime } from '../utils/time'

type Target = { action: 'create' | 'copy' | 'edit' | 'delete'; role?: Role }
interface Values { name: string; description: string; permissions: PermissionCode[] }


function RoleEditor({ target, definitions, onClose, onSaved }: { target: Target; definitions: PermissionDefinition[]; onClose: () => void; onSaved: () => void }) {
  const { message } = App.useApp()
  const scope = useAuthorizationScope(undefined, true)
  const [form] = Form.useForm<Values>()
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const [error, setError] = useState<string | null>(null)
  const role = target.role
  const deleting = target.action === 'delete'
  const copying = target.action === 'copy'
  const title = deleting ? `删除角色「${role?.name}」` : target.action === 'edit' ? `编辑角色「${role?.name}」` : copying ? `复制角色「${role?.name}」` : '新建自定义角色'
  const submit = async () => {
    if (pending.current || !scope.allowed()) return
    pending.current = true
    setBusy(true)
    const ticket = scope.begin()
    setError(null)
    try {
      const values = deleting ? null : await form.validateFields()
      if (!scope.current(ticket)) return
      if (deleting && role) await roleApi.delete(role)
      else if (values) {
        const body = { name: values.name.trim(), description: values.description ?? '', permissions: values.permissions ?? [] }
        if (target.action === 'edit' && role) await roleApi.update(role.id, { ...body, revision: role.revision, authorization_revision: role.authorization_revision })
        else await roleApi.create({ ...body, ...(copying && role ? { source_role_id: role.id, source_revision: role.revision } : {}) })
      }
      if (!scope.current(ticket)) return
      void message.success(deleting ? '角色已删除' : '角色已保存')
      onSaved()
      onClose()
      void useAuthStore.getState().refreshAuthorization()
    } catch (err) {
      if (scope.current(ticket) && err instanceof Error) setError(errorMessage(err))
      // 表单校验对象由Form就地展示；409保留草稿，不自动重发。
    } finally { if (scope.current(ticket)) { pending.current = false; setBusy(false) } }
  }
  const groups = [...new Set(definitions.map((item) => item.group))]
  return <Modal open title={title} width={760} onCancel={onClose} onOk={() => void submit()} confirmLoading={busy} okText={deleting ? '删除' : '保存'} cancelText="取消" maskClosable={!busy} closable={!busy} keyboard={!busy} okButtonProps={{ danger: deleting, disabled: deleting && !!role?.member_count }} cancelButtonProps={{ disabled: busy }}>
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 12 }} />}
    {deleting ? <Alert type={role?.member_count ? 'warning' : 'error'} showIcon message={role?.member_count ? `仍分配给 ${role.member_count} 个账号，请先撤回全部分配。` : '删除后不可恢复；服务器会再次检查是否仍有成员。'} /> : <Form form={form} layout="vertical" disabled={busy} initialValues={{ name: copying ? `${role?.name} 副本` : role?.name ?? '', description: role?.description ?? '', permissions: role?.permissions ?? [] }}>
      {target.action === 'edit' && <Alert showIcon type="warning" message={`权限变更将影响全部 ${role?.member_count ?? 0} 个成员，后续请求立即按新权限校验。`} style={{ marginBottom: 12 }} />}
      {copying && <Alert showIcon type="info" message="仅复制可委派权限，不复制超级管理员身份或角色授权能力。" style={{ marginBottom: 12 }} />}
      <Form.Item name="name" label="角色名称" rules={[{ required: true, whitespace: true, message: '请输入角色名称' }, { max: 100, message: '最多100个字符' }]}><Input maxLength={100} /></Form.Item>
      <Form.Item name="description" label="说明" rules={[{ max: 500, message: '最多500个字符' }]}><Input.TextArea rows={2} maxLength={500} /></Form.Item>
      <Form.Item name="permissions" label="管理权限（危险操作须谨慎授予）" getValueFromEvent={(values: PermissionCode[]) => permissionSelection(values, definitions)}>
        <Checkbox.Group style={{ width: '100%' }}><Space direction="vertical" size={16} style={{ width: '100%' }}>{groups.map((group) => <section key={group}><Typography.Text strong>{group}</Typography.Text><div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, paddingTop: 8 }}>{definitions.filter((item) => item.group === group).map((item) => <Checkbox key={item.code} value={item.code}><span>{item.label}{item.dangerous && <Tag color="red" style={{ marginInlineStart: 8 }}>危险</Tag>}</span>{item.requires.length > 0 && <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>依赖：{item.requires.map((code) => definitions.find((definition) => definition.code === code)?.label ?? code).join('、')}</Typography.Text>}</Checkbox>)}</div></section>)}</Space></Checkbox.Group>
      </Form.Item>
    </Form>}
  </Modal>
}

function Roles() {
  const mobile = useIsMobile()
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [target, setTarget] = useState<Target | null>(null)
  const loader = useCallback(async () => ({ roles: await roleApi.list({ q: query, page, page_size: 20 }), definitions: (await roleApi.permissions()).items }), [query, page])
  const { data, loading, error, reload } = useLoader(loader)
  const actions = (role: Role) => <Space wrap size={4}><Button type="link" onClick={() => setTarget({ action: 'copy', role })}>复制</Button><Button type="link" disabled={role.is_builtin} onClick={() => setTarget({ action: 'edit', role })}>编辑</Button><Button type="link" danger disabled={role.is_builtin || role.member_count > 0} onClick={() => setTarget({ action: 'delete', role })}>删除</Button></Space>
  const permissionCount = (role: Role) => role.system_key === 'super_admin' ? '全部' : role.permissions.length
  return <div style={{ maxWidth: 1300, minWidth: 0, margin: '0 auto', padding: mobile ? 12 : 24 }}>
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
      <Typography.Title level={3} style={{ margin: 0 }}>角色与权限</Typography.Title>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, minWidth: 0, maxWidth: '100%', width: mobile ? '100%' : undefined }}>
        <Input.Search aria-label="搜索角色" placeholder="按角色名称搜索" allowClear style={{ width: mobile ? '100%' : 240, maxWidth: '100%' }} onSearch={(value) => { setQuery(value.trim()); setPage(1) }} />
        <Button icon={<ReloadOutlined />} loading={loading} onClick={reload}>刷新</Button>
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setTarget({ action: 'create' })}>新建角色</Button>
      </div>
    </div>
    <Alert type="info" showIcon message="一个账号可拥有多个角色，权限取并集。角色与授权管理仅限超管；自定义角色即使拥有所有可委派权限，也不是超管。" style={{ marginBottom: 16 }} />
    {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
    {mobile ? <>
      <List aria-label="角色移动列表" loading={loading} dataSource={data?.roles.items ?? []} locale={{ emptyText: '没有匹配的角色' }} renderItem={(role) => <List.Item key={role.id} data-role-id={role.id} style={{ display: 'block', minWidth: 0, overflowWrap: 'anywhere' }}>
        <Space wrap size={[8, 4]}><Typography.Text strong>{role.name}</Typography.Text><Tag>{role.is_builtin ? '内置只读' : '自定义'}</Tag></Space>
        <Typography.Paragraph type="secondary" style={{ margin: '8px 0', whiteSpace: 'pre-wrap' }}>{role.description || '暂无说明'}</Typography.Paragraph>
        <Space wrap size={[16, 4]}><Typography.Text>权限数：{permissionCount(role)}</Typography.Text><Typography.Text>成员：{role.member_count}</Typography.Text></Space>
        <Typography.Paragraph type="secondary" style={{ margin: '4px 0 8px' }}>更新于：{formatTime(role.updated_at)}</Typography.Paragraph>
        {actions(role)}
      </List.Item>} />
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12, justifyContent: 'space-between', alignItems: 'center', marginTop: 16 }}>
        <Typography.Text type="secondary">共 {data?.roles.total ?? 0} 个角色</Typography.Text>
        <Pagination aria-label="角色分页" current={page} total={data?.roles.total ?? 0} pageSize={20} simple showSizeChanger={false} onChange={setPage} />
      </div>
    </> : <Table<Role> rowKey="id" loading={loading} dataSource={data?.roles.items ?? []} scroll={{ x: 760 }} pagination={{ current: page, total: data?.roles.total ?? 0, pageSize: 20, onChange: setPage }} columns={[
      { title: '角色', key: 'name', render: (_, role) => <Space direction="vertical" size={0}><Space><Typography.Text strong>{role.name}</Typography.Text><Tag>{role.is_builtin ? '内置只读' : '自定义'}</Tag></Space><Typography.Text type="secondary">{role.description || '暂无说明'}</Typography.Text></Space> },
      { title: '权限数', key: 'permissions', width: 90, render: (_, role) => permissionCount(role) },
      { title: '成员', dataIndex: 'member_count', width: 70 },
      { title: '更新于', key: 'updated_at', width: 170, render: (_, role) => formatTime(role.updated_at) },
      { title: '操作', key: 'actions', width: 230, render: (_, role) => actions(role) },
    ]} />}
    {target && data && <RoleEditor target={target} definitions={data.definitions} onClose={() => setTarget(null)} onSaved={reload} />}
  </div>
}

export default function AdminRolesPage() {
  const user = useAuthStore((state) => state.user)
  return isSuperAdmin(user) ? <Roles key={`${user?.id}:${authorizationKey(user)}`} /> : <Result status="403" title="只有超级管理员可管理角色" />
}
