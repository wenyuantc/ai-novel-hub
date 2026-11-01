import { LockOutlined, PlusOutlined, UserOutlined } from '@ant-design/icons'
import { Alert, App, Button, Checkbox, Form, Input, Modal, Result, Space, Table, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { adminApi } from '../api/admin'
import { errorMessage } from '../api/client'
import type { AdminUser } from '../api/types'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import AdminUserActionButtons from '../components/adminUsers/AdminUserActionButtons'
import AdminUserActionDialog from '../components/adminUsers/AdminUserActionDialog'
import { useAdminUserActions } from '../components/adminUsers/useAdminUserActions'
import { formatTime } from '../utils/time'
import { confirmPasswordRules, passwordRules, usernameRules } from '../utils/validation'

interface CreateValues {
  username: string
  password: string
  confirm: string
  is_admin: boolean
}

/** 用户管理（仅管理员）：只展示账号信息与项目数，不涉及任何小说内容。 */
function AdminUsers({ actorId }: { actorId: number }) {
  const { message } = App.useApp()
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

  const handleDeleted = useCallback(() => {
    if (users.length === 1 && page > 1) setPage(page - 1)
    else reload()
  }, [users.length, page, reload])
  const { state: actionState, controller: actionController } = useAdminUserActions({ actorId, onChanged: reload, onDeleted: handleDeleted })

  const handleCreate = async () => {
    const v = await createForm.validateFields()
    setCreating(true)
    try {
      const u = await adminApi.createUser({ username: v.username.trim(), password: v.password, is_admin: !!v.is_admin })
      message.success(`已创建用户「${u.username}」`)
      setCreateOpen(false)
      reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setCreating(false)
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
      dataIndex: 'is_admin',
      width: 110,
      render: (v: boolean) => (v ? <Tag color="gold">管理员</Tag> : <Tag>普通用户</Tag>),
    },
    {
      title: '状态',
      dataIndex: 'is_active',
      width: 100,
      render: (v: boolean) => (v ? <Tag color="green">启用</Tag> : <Tag color="red">已禁用</Tag>),
    },
    { title: '作品数（含回收站）', dataIndex: 'project_count', width: 190, render: (count: number, user) => <Space direction="vertical" size={0}><Typography.Text>{count}</Typography.Text>{count > 0 && <Space size={8}><Link to={`/admin/projects?owner_user_id=${user.id}`}>正常作品</Link><Link to={`/admin/recycle-bin?owner_user_id=${user.id}`}>回收站</Link></Space>}</Space> },
    { title: '注册时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
    {
      title: '操作',
      key: 'actions',
      width: 400,
      render: (_, u) => <Space size={4} wrap><Link to={`/admin/users/${u.id}`}>详情</Link><AdminUserActionButtons user={u} actorId={actorId} disabled={loading || actionState.target !== null} onAction={actionController.open} /></Space>,
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
        open={createOpen}
        onOk={() => void handleCreate()}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        okText="创建"
        cancelText="取消"
        destroyOnHidden
      >
        <Form form={createForm} layout="vertical" preserve={false} disabled={creating} initialValues={{ is_admin: false }}>
          <Form.Item name="username" label="用户名" rules={usernameRules}>
            <Input prefix={<UserOutlined />} autoComplete="off" placeholder="3–32 个字符" />
          </Form.Item>
          <Form.Item name="password" label="初始密码" rules={passwordRules}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="至少 6 位" />
          </Form.Item>
          <Form.Item name="confirm" label="确认密码" dependencies={['password']} rules={confirmPasswordRules()}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="再次输入密码" />
          </Form.Item>
          <Form.Item name="is_admin" valuePropName="checked" style={{ marginBottom: 0 }}>
            <Checkbox>设为管理员</Checkbox>
          </Form.Item>
        </Form>
      </Modal>

      <AdminUserActionDialog state={actionState} controller={actionController} />
    </div>
  )
}

/** 管理员身份变化时丢弃旧页面和危险操作目标。 */
export default function AdminUsersPage() {
  const user = useAuthStore((state) => state.user)
  return user?.is_admin ? <AdminUsers key={user.id} actorId={user.id} /> : <Result status="403" title="需要管理员权限" />
}
