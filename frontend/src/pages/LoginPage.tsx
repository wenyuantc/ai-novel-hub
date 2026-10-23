import { LockOutlined, UserOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Form, Input, Tabs, Typography } from 'antd'
import { useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { errorMessage } from '../api/client'
import type { Credentials } from '../api/types'
import { FullPageSpin } from '../components/RequireAuth'
import { useAuthStore } from '../stores/auth'
import { confirmPasswordRules, passwordRules, usernameRules } from '../utils/validation'

type Mode = 'login' | 'register'

interface RegisterValues extends Credentials {
  confirm: string
}

export default function LoginPage() {
  const { status, registrationEnabled, login, register } = useAuthStore()
  const [mode, setMode] = useState<Mode>('login')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/projects'

  if (status === 'unknown') return <FullPageSpin />
  if (status === 'authenticated') return <Navigate to={from} replace />

  const activeMode: Mode = registrationEnabled ? mode : 'login'

  const submit = async (values: Credentials) => {
    setSubmitting(true)
    setError(null)
    try {
      const cred = { username: values.username.trim(), password: values.password }
      if (activeMode === 'login') await login(cred)
      else await register(cred)
      navigate(from, { replace: true })
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'linear-gradient(135deg, #e6f4ff 0%, #f9f0ff 100%)',
        padding: 16,
      }}
    >
      <Card style={{ width: 400, maxWidth: '100%' }}>
        <Typography.Title level={3} style={{ textAlign: 'center', marginBottom: 4 }}>
          AI 小说平台
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ textAlign: 'center' }}>
          从一个点子到长篇连载
        </Typography.Paragraph>
        {registrationEnabled ? (
          <Tabs
            centered
            activeKey={activeMode}
            onChange={(k) => {
              setMode(k as Mode)
              setError(null)
            }}
            items={[
              { key: 'login', label: '登录' },
              { key: 'register', label: '注册' },
            ]}
          />
        ) : null}
        {error && <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />}
        {activeMode === 'login' ? (
          <Form<Credentials> key="login" layout="vertical" onFinish={(v) => void submit(v)} disabled={submitting}>
            <Form.Item name="username" label="用户名" rules={usernameRules}>
              <Input prefix={<UserOutlined />} autoComplete="username" placeholder="用户名" />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={passwordRules}>
              <Input.Password prefix={<LockOutlined />} autoComplete="current-password" placeholder="密码" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              登录
            </Button>
          </Form>
        ) : (
          <Form<RegisterValues>
            key="register"
            layout="vertical"
            onFinish={(v) => void submit(v)}
            disabled={submitting}
          >
            <Form.Item name="username" label="用户名" rules={usernameRules}>
              <Input prefix={<UserOutlined />} autoComplete="username" placeholder="3–32 个字符" />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={passwordRules}>
              <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="至少 6 位" />
            </Form.Item>
            <Form.Item
              name="confirm"
              label="确认密码"
              dependencies={['password']}
              rules={confirmPasswordRules()}
            >
              <Input.Password prefix={<LockOutlined />} autoComplete="new-password" placeholder="再次输入密码" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={submitting}>
              注册并登录
            </Button>
          </Form>
        )}
        {!registrationEnabled && (
          <Typography.Paragraph type="secondary" style={{ textAlign: 'center', marginTop: 16, marginBottom: 0 }}>
            注册已关闭，如需账号请联系管理员
          </Typography.Paragraph>
        )}
      </Card>
    </div>
  )
}
