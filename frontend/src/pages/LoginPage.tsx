import { Alert, Button, Form, Input, Space, Tabs } from 'antd'
import { useState } from 'react'
import { Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { accountSecurityApi, providerName, type LoginProvider } from '../api/accountSecurity'
import { useLoader } from '../hooks/useLoader'
import { loginReturnPath, readLoginReturn, rememberLoginReturn } from '../utils/loginReturn'
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
  const callback = searchParams.get('oauth')
  const challenge = callback === 'challenge' || searchParams.get('step') === 'verify'
  const verify = useAuthStore((s) => s.verify)
  const providers = useLoader(accountSecurityApi.providers)
  const from = loginReturnPath(location.state?.from ?? (callback ? readLoginReturn() : '/projects'))
  const callbackError = callback === 'error' ? searchParams.get('reason')?.slice(0, 180) || '授权未完成，请重试。' : null
  const missingSession = callback === 'success' && status === 'anonymous' ? '第三方授权已返回，但登录会话未建立，请重新登录。' : null
  const loginError = error || callbackError || missingSession
  const activeMode: Mode = registrationEnabled ? mode : 'login'
  const registering = activeMode === 'register'

  if (status === 'unknown') return <FullPageSpin />
  if (status === 'authenticated') {
    if (callback === 'bound' || callbackError) return <Navigate to="/settings/security" replace state={{ bindingError: callbackError }} />
    return <Navigate to={from} replace />
  }
  const startProvider = async (provider: LoginProvider) => {
    if (submitting) return
    setSubmitting(true)
    setError(null)
    try {
      const { url } = await accountSecurityApi.start(provider, 'login')
      rememberLoginReturn(from)
      window.location.assign(url)
    } catch (err) { setError(errorMessage(err)); setSubmitting(false) }
  }
  const finishChallenge = async ({ code }: { code: string }) => {
    if (submitting) return
    setSubmitting(true)
    setError(null)
    try { await verify(code.trim()) }
    catch (err) { setError(errorMessage(err)) }
    finally { setSubmitting(false) }
  }

  const submit = async (values: Credentials) => {
    if (submitting) return
    setSubmitting(true)
    setError(null)
    if (callback) {
      setSearchParams(registering ? { tab: 'register' } : {}, { replace: true, state: { from } })
    }
    try {
      const cred = { username: values.username.trim(), password: values.password }
      if (activeMode === 'login') {
        const result = await login(cred)
        if (result === 'challenge') {
          setSearchParams({ step: 'verify' }, { replace: true, state: { from } })
          return
        }
        if (result !== 'complete') return
      } else await register(cred)
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
        <div className="auth-form-heading"><h2 id="auth-heading">{challenge ? '两步验证' : registering ? '打开新的篇章' : '回到你的故事'}</h2><p>{challenge ? '输入验证器中的动态码，或一枚未使用的恢复码。' : registering ? '创建一个只属于你的创作空间。' : '登录后，继续上次未完的那一页。'}</p></div>
        {loginError && <Alert className="auth-error" role="alert" type="error" showIcon message={loginError} />}
        {challenge ? <Form layout="vertical" aria-label="两步验证" className="auth-form" disabled={submitting} onFinish={finishChallenge}>
          <Form.Item name="code" label="动态码或恢复码" rules={[{ required: true, whitespace: true, message: '请输入动态码或恢复码' }]}>
            <Input autoFocus autoComplete="one-time-code" autoCapitalize="none" spellCheck={false} maxLength={128} />
          </Form.Item>
          <Button block type="primary" htmlType="submit" loading={submitting}>验证并登录</Button>
          <Button type="link" onClick={() => { setError(null); setSearchParams({}, { replace: true, state: { from } }) }}>返回登录</Button>
        </Form> : <>
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
        <Form<AuthValues> className="auth-form" key={activeMode} layout="vertical" onFinish={(values) => void submit(values)} disabled={submitting} requiredMark={false}>
          <Form.Item name="username" label="用户名" rules={usernameRules} extra={registering ? '3–32 位，不能包含空白字符' : undefined}><Input autoComplete="username" placeholder="请输入用户名" /></Form.Item>
          <Form.Item name="password" label="密码" rules={passwordRules} extra={registering ? '至少 6 个字符，最多 72 字节，请避免使用容易猜到的密码' : undefined}><Input.Password autoComplete={registering ? 'new-password' : 'current-password'} placeholder="请输入密码" /></Form.Item>
          {registering && <Form.Item name="confirm" label="确认密码" dependencies={['password']} rules={confirmPasswordRules()}><Input.Password autoComplete="new-password" placeholder="再次输入密码" /></Form.Item>}
          <Button className="auth-submit" type="primary" htmlType="submit" block loading={submitting} disabled={submitting}>{registering ? '创建账户' : '登录知页'}</Button>
        </Form>
        <Space wrap style={{ marginTop: 12 }}>
          {providers.data?.providers.map((provider) => <Button key={provider} disabled={submitting} onClick={() => void startProvider(provider)}>{providerName[provider]} 登录</Button>)}
          {providers.error && <Button onClick={providers.reload}>重新加载第三方登录方式</Button>}
        </Space>
        {!registrationEnabled && <p className="auth-registration-closed">注册已关闭，如需账号请联系管理员。</p>}
        <p className="auth-help">账户由当前实例管理。忘记密码时，请联系实例管理员重置。</p>
        </>}
      </div>
    </section>
  </main>
}
