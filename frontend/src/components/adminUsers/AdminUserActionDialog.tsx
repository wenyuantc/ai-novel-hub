import { LockOutlined } from '@ant-design/icons'
import { Alert, Form, Input, Modal, Typography } from 'antd'
import { useModalWidth } from '../../hooks/useIsMobile'
import { userDeleteBlockReason } from '../../utils/adminUsers'
import { confirmPasswordRules, passwordRules } from '../../utils/validation'
import type { AdminUserActionsController, AdminUserActionsState, AdminUserActionTarget } from './adminUserActionsController'

interface Props { state: AdminUserActionsState; controller: AdminUserActionsController }
interface InnerProps extends Props { target: AdminUserActionTarget }
interface ResetValues { new_password: string; confirm: string }

function Confirmation({ target, state, controller }: InnerProps) {
  const width = useModalWidth(540)
  const [form] = Form.useForm<ResetValues>()
  const { user, action } = target
  const resetting = action === 'reset'
  const deleting = action === 'delete'
  const label = resetting ? '重置密码' : deleting ? '永久删除' : action === 'active' ? user.is_active ? '禁用' : '启用' : user.is_admin ? '取消管理员' : '设为管理员'
  const submit = () => controller.submit(target, resetting ? async () => {
    try { const values = await form.validateFields(); return { new_password: values.new_password } }
    catch { return null }
  } : undefined)
  return <Modal open title={`${label}「${user.username}」？`} width={width} onOk={() => void submit()} onCancel={controller.close} confirmLoading={state.busy} closable={!state.busy} maskClosable={!state.busy} keyboard={!state.busy} cancelButtonProps={{ disabled: state.busy }} okText={label} cancelText="取消" okButtonProps={{ danger: deleting || (action === 'active' && user.is_active) || (action === 'admin' && user.is_admin), disabled: !controller.canSubmit() }}>
    {resetting ? <>
      <Typography.Paragraph type="secondary">重置后该用户所有已登录会话立即失效，需用新密码重新登录。</Typography.Paragraph>
      <Form form={form} layout="vertical" preserve={false} disabled={state.busy}>
        <Form.Item name="new_password" label="新密码" rules={passwordRules}><Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="至少 6 位" /></Form.Item>
        <Form.Item name="confirm" label="确认新密码" dependencies={['new_password']} rules={confirmPasswordRules('new_password')}><Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="再次输入新密码" /></Form.Item>
      </Form>
    </> : deleting ? <>
      <Alert showIcon type="error" style={{ marginBottom: 16 }} message={userDeleteBlockReason(user) ?? '将永久删除该用户及其模型配置、私人资源等账号数据，且无法恢复。服务器会再次确认名下没有作品（含回收站）。'} />
      <Typography.Paragraph>请输入用户名 <Typography.Text code>{user.username}</Typography.Text> 以确认：</Typography.Paragraph>
      <Input aria-label="确认删除的用户名" value={state.confirmation} onChange={(event) => controller.setConfirmation(event.target.value)} placeholder={user.username} autoComplete="off" disabled={state.busy || user.project_count > 0} onPressEnter={() => void submit()} />
    </> : <Typography.Paragraph>{action === 'active' ? user.is_active ? '禁用后其已登录会话立即失效，且无法再登录，数据保留。' : '启用后可正常登录。' : user.is_admin ? '对方将失去管理员权限，无法再访问管理页面。' : '对方将获得用户管理、作品管理、回收站、任务监控、系统设置和操作日志等管理员权限。'}</Typography.Paragraph>}
    {state.error && <Alert showIcon type="error" message={state.error} style={{ marginTop: 16 }} />}
  </Modal>
}

/** 声明式目标确认框：离页自动卸载，不留下可提交旧目标的 imperative portal。 */
export default function AdminUserActionDialog({ state, controller }: Props) {
  return state.target ? <Confirmation key={`${state.target.user.id}:${state.target.action}`} target={state.target} state={state} controller={controller} /> : null
}
