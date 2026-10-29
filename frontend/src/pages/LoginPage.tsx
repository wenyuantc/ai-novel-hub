import { Alert, Button, Form, Input, Tabs } from 'antd'
import { useState } from 'react'
import { Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { errorMessage } from '../api/client'
import type { Credentials } from '../api/types'
import mountainStory from '../assets/auth-mountain-story.jpg'
import { FullPageSpin } from '../components/RequireAuth'
import { StudioBrand } from '../components/StudioUI'
import { useAuthStore } from '../stores/auth'
import { confirmPasswordRules, passwordRules, usernameRules } from '../utils/validation'
import './LoginPage.css'

type Mode = 'login' | 'register'
interface AuthValues extends Credentials { confirm?: string }

/** 独立纸面认证页：双栏装帧，真实登录/注册与共享校验，保留受保护页面的返回目标。 */
export default function LoginPage() {
  const status = useAuthStore((s) => s.status)
  const registrationEnabled = useAuthStore((s) => s.registrationEnabled)
  const login = useAuthStore((s) => s.login)
  const register = useAuthStore((s) => s.register)
  const [searchParams, setSearchParams] = useSearchParams()
  const mode: Mode = searchParams.get('tab') === 'register' ? 'register' : 'login'
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/projects'
  const activeMode: Mode = registrationEnabled ? mode : 'login'
  const registering = activeMode === 'register'

  if (status === 'unknown') return <FullPageSpin />
  if (status === 'authenticated') return <Navigate to={from} replace />

  const submit = async (values: Credentials) => {
    if (submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const cred = { username: values.username.trim(), password: values.password }
      if (activeMode === 'login') await login(cred)
      else await register(cred)
      navigate(from, { replace: true })
    } catch (err) { setError(errorMessage(err)) }
    finally { setSubmitting(false) }
  }

  return <main className="auth-page">
    <section className="auth-story" aria-labelledby="auth-story-heading">
      <StudioBrand />
      <div className="auth-story-visual">
        <div className="auth-story-book" aria-hidden="true"><img src={mountainStory} alt="" /><div><span className="auth-story-book-title">故事初章</span><span className="auth-story-book-caption">创作手稿 · 装帧示意</span></div></div>
        <h1 id="auth-story-heading">让故事，在这一页生长。</h1>
        <p>从一闪而过的念头，到属于你的万千世界。</p>
      </div>
      <p className="auth-story-footer">知页小说 · 你的私人创作书房</p>
    </section>
    <section className="auth-form-panel" aria-labelledby="auth-heading">
      <div className="auth-form-container">
        <div className="auth-form-heading"><h2 id="auth-heading">{registering ? '打开新的篇章' : '回到你的故事'}</h2><p>{registering ? '创建一个只属于你的创作空间。' : '登录后，继续上次未完的那一页。'}</p></div>
        <Tabs className="auth-tabs" activeKey={activeMode} onChange={(key) => {
          if (submitting) return
          const next = new URLSearchParams(searchParams)
          if (key === 'register' && registrationEnabled) next.set('tab', 'register')
          else next.delete('tab')
          setSearchParams(next, { replace: true, state: location.state })
          setError(null)
        }} items={[
          { key: 'login', label: '登录', disabled: submitting },
          ...(registrationEnabled ? [{ key: 'register', label: '注册', disabled: submitting }] : []),
        ]} />
        {error && <Alert className="auth-error" type="error" showIcon message={error} />}
        <Form<AuthValues> className="auth-form" key={activeMode} layout="vertical" onFinish={(values) => void submit(values)} disabled={submitting} requiredMark={false}>
          <Form.Item name="username" label="用户名" rules={usernameRules} extra={registering ? '3–32 位，不能包含空白字符' : undefined}><Input autoComplete="username" placeholder="请输入用户名" /></Form.Item>
          <Form.Item name="password" label="密码" rules={passwordRules} extra={registering ? '至少 6 个字符，最多 72 字节，请避免使用容易猜到的密码' : undefined}><Input.Password autoComplete={registering ? 'new-password' : 'current-password'} placeholder="请输入密码" /></Form.Item>
          {registering && <Form.Item name="confirm" label="确认密码" dependencies={['password']} rules={confirmPasswordRules()}><Input.Password autoComplete="new-password" placeholder="再次输入密码" /></Form.Item>}
          <Button className="auth-submit" type="primary" htmlType="submit" block loading={submitting} disabled={submitting}>{registering ? '创建账户' : '登录知页'}</Button>
        </Form>
        {!registrationEnabled && <p className="auth-registration-closed">注册已关闭，如需账号请联系管理员。</p>}
        <p className="auth-help">账户由当前实例管理。忘记密码时，请联系实例管理员重置。</p>
      </div>
    </section>
  </main>
}
