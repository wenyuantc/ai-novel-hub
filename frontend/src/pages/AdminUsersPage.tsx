import { LockOutlined, PlusOutlined, UserOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Modal, Result, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { adminApi } from '../api/admin'
import { errorMessage } from '../api/client'
import type { AdminUser } from '../api/types'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import AdminUserActionButtons from '../components/adminUsers/AdminUserActionButtons'
import AdminUserActionDialog from '../components/adminUsers/AdminUserActionDialog'
import { useAdminUserActions } from '../components/adminUsers/useAdminUserActions'
import RoleTags from '../components/adminUsers/RoleTags'
import UserRolesButton from '../components/adminUsers/UserRolesButton'
import { can } from '../utils/permissions'
import { useAuthorizationScope } from '../hooks/useAuthorizationScope'
import { useAuthorizationCleanup } from '../hooks/useAuthorizationCleanup'
import { formatTime } from '../utils/time'
import { confirmPasswordRules, passwordRules, usernameRules } from '../utils/validation'

interface CreateValues {
  username: string
  password: string
  confirm: string
}

/** 用户管理（仅管理员）：只展示账号信息与项目数，不涉及任何小说内容。 */
function AdminUsers({ actorId }: { actorId: number }) {
  const { message } = App.useApp()
  const actor = useAuthStore((state) => state.user)
  const currentUserId = actorId
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)

  const loader = useCallback(
    () => adminApi.listUsers({ q: query, page, page_size: pageSize }),
    [query, page, pageSize],
  )
  const { data, loading, error, reload } = useLoader(loader)
  const users = data?.items ?? []

  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createForm] = Form.useForm<CreateValues>()
  const createScope = useAuthorizationScope('users.create')
  const pending = useRef(false)
  useAuthorizationCleanup(() => { setCreateOpen(false); setCreating(false); pending.current = false })

  const handleDeleted = useCallback(() => {
    if (users.length === 1 && page > 1) setPage(page - 1)
    else reload()
  }, [users.length, page, reload])
  const { state: actionState, controller: actionController } = useAdminUserActions({ actorId, onChanged: reload, onDeleted: handleDeleted })

  const handleCreate = async () => {
    if (pending.current || !createScope.allowed()) return
    pending.current = true
    const ticket = createScope.begin()
    const valid = () => createScope.current(ticket)
    setCreating(true)
    try {
      const v = await createForm.validateFields()
      if (!valid()) return
      const u = await adminApi.createUser({ username: v.username.trim(), password: v.password })
      if (!valid()) return
      message.success(`已创建用户「${u.username}」`)
      setCreateOpen(false)
      reload()
    } catch (err) {
      if (valid() && err instanceof Error) message.error(errorMessage(err))
    } finally {
      if (valid()) { pending.current = false; setCreating(false) }
    }
  }

  const columns: TableColumnsType<AdminUser> = [
    {
      title: '用户名',
      dataIndex: 'username',
      render: (name: string, u) => (
        <Space>
          <Link to={`/admin/users/${u.id}`}>{name}</Link>
          {u.id === currentUserId && <Tag color="blue">我</Tag>}
        </Space>
      ),
    },
    {
      title: '角色',
      key: 'roles',
      width: 180,
      render: (_, row) => <RoleTags user={row} />,
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 100,
      render: (v: boolean) => (v ? <Tag color="green">启用</Tag> : <Tag color="red">已禁用</Tag>),
    },
    { title: '作品数（含回收站）', dataIndex: 'project_count', width: 190, render: (count: number, user) => <Space direction="vertical" size={0}><Typography.Text>{count}</Typography.Text>{count > 0 && <Space size={8}>{can(actor, 'projects.read') && <Link to={`/admin/projects?owner_user_id=${user.id}`}>正常作品</Link>}{can(actor, 'recycle_bin.read') && <Link to={`/admin/recycle-bin?owner_user_id=${user.id}`}>回收站</Link>}</Space>}</Space> },
    { title: '注册时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
    {
      title: '操作',
      key: 'actions',
      width: 400,
      render: (_, u) => <Space size={4} wrap><Link to={`/admin/users/${u.id}`}>详情</Link><UserRolesButton user={u} onChanged={reload} disabled={loading} /><AdminUserActionButtons user={u} actorId={actorId} disabled={loading || actionState.target !== null} onAction={actionController.open} /></Space>,
    },
  ]

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16, gap: 16, flexWrap: 'wrap' }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          用户管理
        </Typography.Title>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="按用户名搜索"
            style={{ width: 240 }}
            onSearch={(v) => {
              setQuery(v.trim())
              setPage(1)
            }}
          />
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={!can(actor, 'users.create')}
            onClick={() => setCreateOpen(true)}
          >
            新建用户
          </Button>
        </Space>
      </div>
      <Alert type="info" showIcon message="名下还有作品时禁止删号（包括回收站及待清理作品）。可以先禁用账号，彻底清理全部作品后再删除用户。" style={{ marginBottom: 16 }} />
      {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
      <Table<AdminUser>
        rowKey="id"
        columns={columns}
        dataSource={users}
        loading={loading}
        scroll={{ x: 900 }}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 个用户`,
          onChange: (p, size) => {
            if (size !== pageSize) {
              setPageSize(size)
              setPage(1)
            } else {
              setPage(p)
            }
          },
        }}
      />

      <Modal
        title="新建用户"
        open={createOpen && can(actor, 'users.create')}
        onOk={() => void handleCreate()}
        confirmLoading={creating}
        onCancel={creating ? undefined : () => setCreateOpen(false)}
        closable={!creating}
        maskClosable={!creating}
        okText="创建"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={createForm} layout="vertical" preserve={false} disabled={creating}>
          <Form.Item name="username" label="用户名" rules={usernameRules}>
            <Input prefix={<UserOutlined />} autoComplete="off" placeholder="3–32 个字符" />
          </Form.Item>
          <Form.Item name="password" label="初始密码" rules={passwordRules}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="至少 6 位" />
          </Form.Item>
          <Form.Item name="confirm" label="确认密码" dependencies={['password']} rules={confirmPasswordRules()}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="再次输入密码" />
          </Form.Item>
          <Alert type="info" message="新账号默认为普通用户；创建后由超管通过分配角色授权。" />
        </Form>
      </Modal>

      <AdminUserActionDialog state={actionState} controller={actionController} />
    </div>
  )
}

/** 管理员身份变化时丢弃旧页面和危险操作目标。 */
export default function AdminUsersPage() {
  const user = useAuthStore((state) => state.user)
  return user && can(user, 'users.read') ? <AdminUsers key={user.id} actorId={user.id} /> : <Result status="403" title="需要管理员权限" />
}
