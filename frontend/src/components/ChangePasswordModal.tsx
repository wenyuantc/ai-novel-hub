import { LockOutlined } from '@ant-design/icons'
import { App, Form, Input, Modal } from 'antd'
import { useState } from 'react'
import { authApi } from '../api/auth'
import { errorMessage } from '../api/client'
import { confirmPasswordRules, passwordRules } from '../utils/validation'

interface FormValues {
  old_password: string
  new_password: string
  confirm: string
}

/** 自助修改密码：成功后当前会话保持登录，其他会话失效。 */
export default function ChangePasswordModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const [saving, setSaving] = useState(false)

  const handleOk = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      await authApi.changePassword({ old_password: v.old_password, new_password: v.new_password })
      message.success('密码已修改，其他设备上的登录已失效')
      onClose()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title="修改密码"
      open={open}
      onOk={() => void handleOk()}
      confirmLoading={saving}
      onCancel={onClose}
      okText="确认修改"
      cancelText="取消"
      destroyOnHidden
    >
      <Form form={form} layout="vertical" preserve={false} disabled={saving}>
        <Form.Item name="old_password" label="原密码" rules={[{ required: true, message: '请输入原密码' }]}>
          <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
        </Form.Item>
        <Form.Item name="new_password" label="新密码" rules={passwordRules}>
          <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="至少 6 位" />
        </Form.Item>
        <Form.Item name="confirm" label="确认新密码" dependencies={['new_password']} rules={confirmPasswordRules('new_password')}>
          <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="再次输入新密码" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
