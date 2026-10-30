import { LockOutlined, PlusOutlined, UserOutlined } from '@ant-design/icons'
import { Alert, App, Button, Checkbox, Form, Input, Modal, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useState } from 'react'
import type { ReactElement } from 'react'
import { adminApi } from '../api/admin'
import { errorMessage } from '../api/client'
import type { AdminUser, AdminUserUpdate } from '../api/types'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { canConfirmDelete, rowActions } from '../utils/adminUsers'
import { formatTime } from '../utils/time'
import { confirmPasswordRules, passwordRules, usernameRules } from '../utils/validation'

const SELF_TIP = '不能对自己执行该操作'

interface CreateValues {
  username: string
  password: string
  confirm: string
  is_admin: boolean
}

interface ResetValues {
  new_password: string
  confirm: string
}

/** 用户管理（仅管理员）：只展示账号信息与项目数，不涉及任何小说内容。 */
export default function AdminUsersPage() {
  const { message, modal } = App.useApp()
  const currentUserId = useAuthStore((s) => s.user?.id)
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

  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [resetting, setResetting] = useState(false)
  const [resetForm] = Form.useForm<ResetValues>()

  const [deleteTarget, setDeleteTarget] = useState<AdminUser | null>(null)
  const [deleteInput, setDeleteInput] = useState('')
  const [deleting, setDeleting] = useState(false)

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

  const handleReset = async () => {
    if (!resetTarget) return
    const v = await resetForm.validateFields()
    setResetting(true)
    try {
      await adminApi.resetPassword(resetTarget.id, { new_password: v.new_password })
      message.success(`已重置「${resetTarget.username}」的密码，其已登录会话已失效`)
      setResetTarget(null)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setResetting(false)
    }
  }

  const confirmUpdate = (u: AdminUser, body: AdminUserUpdate, title: string, content: string, okText: string) => {
    modal.confirm({
      title,
      content,
      okText,
      cancelText: '取消',
      okButtonProps: { danger: body.is_active === false || body.is_admin === false },
      onOk: async () => {
        try {
          await adminApi.updateUser(u.id, body)
          message.success('已更新')
          reload()
        } catch (err) {
          message.error(errorMessage(err))
        }
      },
    })
  }

  const openDelete = (u: AdminUser) => {
    setDeleteInput('')
    setDeleteTarget(u)
  }

  const handleDelete = async () => {
    if (!deleteTarget || !canConfirmDelete(deleteInput, deleteTarget.username)) return
    setDeleting(true)
    try {
      await adminApi.deleteUser(deleteTarget.id)
      message.success(`已删除用户「${deleteTarget.username}」`)
      setDeleteTarget(null)
      // 删掉当前页最后一条时回退一页
      if (users.length === 1 && page > 1) setPage(page - 1)
      else reload()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setDeleting(false)
    }
  }

  /** 自己的行禁用按钮并给出提示 */
  const guarded = (allowed: boolean, button: ReactElement) =>
    allowed ? button : <Tooltip title={SELF_TIP}>{button}</Tooltip>

  const columns: TableColumnsType<AdminUser> = [
    {
      title: '用户名',
      dataIndex: 'username',
      render: (name: string, u) => (
        <Space>
          {name}
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
    { title: '项目数', dataIndex: 'project_count', width: 90 },
    { title: '注册时间', dataIndex: 'created_at', width: 170, render: (v: string) => formatTime(v) },
    {
      title: '操作',
      key: 'actions',
      width: 360,
      render: (_, u) => {
        const a = rowActions(u, currentUserId)
        return (
          <Space size={0} wrap>
            {guarded(
              a.canResetPassword,
              <Button
                type="link"
                size="small"
                disabled={!a.canResetPassword}
                onClick={() => setResetTarget(u)}
              >
                重置密码
              </Button>,
            )}
            {guarded(
              a.canToggleActive,
              <Button
                type="link"
                size="small"
                danger={u.is_active}
                disabled={!a.canToggleActive}
                onClick={() =>
                  u.is_active
                    ? confirmUpdate(
                        u,
                        { is_active: false },
                        `禁用「${u.username}」？`,
                        '禁用后其已登录会话立即失效，且无法再登录，数据保留。',
                        '禁用',
                      )
                    : confirmUpdate(u, { is_active: true }, `启用「${u.username}」？`, '启用后可正常登录。', '启用')
                }
              >
                {u.is_active ? '禁用' : '启用'}
              </Button>,
            )}
            {guarded(
              a.canToggleAdmin,
              <Button
                type="link"
                size="small"
                disabled={!a.canToggleAdmin}
                onClick={() =>
                  u.is_admin
                    ? confirmUpdate(
                        u,
                        { is_admin: false },
                        `取消「${u.username}」的管理员身份？`,
                        '对方将无法再访问用户管理和作品管理。',
                        '取消管理员',
                      )
                    : confirmUpdate(
                        u,
                        { is_admin: true },
                        `将「${u.username}」设为管理员？`,
                        '对方将获得用户管理和作品管理的全部权限。',
                        '设为管理员',
                      )
                }
              >
                {u.is_admin ? '取消管理员' : '设为管理员'}
              </Button>,
            )}
            {guarded(
              a.canDelete,
              <Button type="link" size="small" danger disabled={!a.canDelete} onClick={() => openDelete(u)}>
                删除
              </Button>,
            )}
          </Space>
        )
      },
    },
  ]

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16, gap: 16 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          用户管理
        </Typography.Title>
        <Space>
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

      <Modal
        title={resetTarget ? `重置「${resetTarget.username}」的密码` : '重置密码'}
        open={resetTarget !== null}
        onOk={() => void handleReset()}
        confirmLoading={resetting}
        onCancel={() => setResetTarget(null)}
        okText="重置"
        cancelText="取消"
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary">重置后该用户所有已登录会话立即失效，需用新密码重新登录。</Typography.Paragraph>
        <Form form={resetForm} layout="vertical" preserve={false} disabled={resetting}>
          <Form.Item name="new_password" label="新密码" rules={passwordRules}>
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="至少 6 位" />
          </Form.Item>
          <Form.Item
            name="confirm"
            label="确认新密码"
            dependencies={['new_password']}
            rules={confirmPasswordRules('new_password')}
          >
            <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="再次输入新密码" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={deleteTarget ? `删除用户「${deleteTarget.username}」？` : '删除用户'}
        open={deleteTarget !== null}
        onOk={() => void handleDelete()}
        confirmLoading={deleting}
        onCancel={() => setDeleteTarget(null)}
        okText="永久删除"
        cancelText="取消"
        okButtonProps={{
          danger: true,
          disabled: !deleteTarget || !canConfirmDelete(deleteInput, deleteTarget.username),
        }}
        destroyOnHidden
      >
        {deleteTarget && (
          <>
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 16 }}
              message={`将永久删除该用户及其全部 ${deleteTarget.project_count} 个项目、章节与模型配置，且无法恢复。`}
            />
            <Typography.Paragraph>
              请输入用户名 <Typography.Text code>{deleteTarget.username}</Typography.Text> 以确认：
            </Typography.Paragraph>
            <Input
              value={deleteInput}
              onChange={(e) => setDeleteInput(e.target.value)}
              placeholder={deleteTarget.username}
              autoComplete="off"
              onPressEnter={() => void handleDelete()}
            />
          </>
        )}
      </Modal>
    </div>
  )
}
