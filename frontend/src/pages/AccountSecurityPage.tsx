import { Alert, Button, Card, Form, Input, List, Popconfirm, QRCode, Space, Spin, Typography } from 'antd'
import { useState } from 'react'
import { useLocation } from 'react-router-dom'
import { accountSecurityApi as security, providerName, type LoginProvider } from '../api/accountSecurity'
import { errorMessage } from '../api/client'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'

async function loadSettings() {
  const [status, identities, configured] = await Promise.all([security.status(), security.identities(), security.providers()])
  return { enabled: status.enabled, identities, configured: configured.providers }
}

function Settings() {
  const { data, loading, error, reload } = useLoader(loadSettings)
  const location = useLocation()
  const [failure, setFailure] = useState<string | null>(location.state?.bindingError ?? null)
  const [busy, setBusy] = useState(false)
  const [setup, setSetup] = useState<Awaited<ReturnType<typeof security.setup>> | null>(null)
  const [codes, setCodes] = useState<string[]>([])
  const [disabling, setDisabling] = useState(false)
  const run = async (action: () => Promise<void>) => {
    if (busy) return
    setBusy(true)
    setFailure(null)
    try { await action() }
    catch (err) { setFailure(errorMessage(err)) }
    finally { setBusy(false) }
  }
  const startBinding = (provider: LoginProvider) => run(async () => {
    const { url } = await security.start(provider, 'bind')
    window.location.assign(url)
  })
  return <main style={{ maxWidth: 720, margin: '0 auto', padding: 20 }}>
    <Typography.Title level={2}>账号安全</Typography.Title>
    <Space direction="vertical" style={{ width: '100%' }} size="large">
      {(failure || error) && <Alert role="alert" type="error" showIcon message={failure || error} action={error ? <Button onClick={reload}>重新加载</Button> : undefined} />}
      {!data && loading && <Spin />}
      {data && <>
        <Card title="第三方账号">
          <List locale={{ emptyText: '此实例尚未配置第三方登录' }} dataSource={[...new Set([...data.configured, ...data.identities.providers])]} renderItem={provider => {
            const bound = data.identities.providers.includes(provider)
            const otherLogin = data.identities.has_local_password || data.identities.providers.some(p => p !== provider && data.configured.includes(p))
            return <List.Item actions={[bound
              ? <Popconfirm key="unbind" title={`解除 ${providerName[provider]} 绑定？`} onConfirm={() => run(async () => { await security.unbind(provider); reload() })}><Button disabled={busy || loading || !otherLogin}>解除绑定</Button></Popconfirm>
              : <Button key="bind" disabled={busy || loading} onClick={() => void startBinding(provider)}>绑定</Button>]}>
              <List.Item.Meta title={providerName[provider]} description={bound ? (otherLogin ? '已绑定' : '已绑定，当前唯一登录方式') : '未绑定'} />
            </List.Item>
          }} />
        </Card>
        <Card title="两步验证">
          {codes.length > 0 ? <Space direction="vertical">
            <Alert type="success" showIcon message="两步验证已开启" description="请把恢复码保存到安全的地方。每枚只能使用一次，离开后无法再次查看。" />
            <pre aria-label="一次性恢复码">{codes.join('\n')}</pre>
            <Button onClick={() => setCodes([])}>我已保存恢复码</Button>
          </Space> : setup ? <>
            <Typography.Paragraph>用验证器扫描二维码，或在验证器中手动输入设置密钥。</Typography.Paragraph>
            <QRCode value={setup.otpauth_uri} />
            <Typography.Paragraph style={{ overflowWrap: 'anywhere' }}>设置密钥 <Typography.Text code>{setup.secret}</Typography.Text></Typography.Paragraph>
            <Form layout="vertical" aria-label="绑定验证器" disabled={busy} onFinish={({ code }) => run(async () => {
              const result = await security.enable(code.trim())
              setSetup(null)
              setCodes(result.recovery_codes)
              reload()
            })}>
              <Form.Item name="code" label="验证器动态码" rules={[{ pattern: /^\d{6}$/, required: true, message: '请输入六位动态码' }]}><Input autoComplete="one-time-code" inputMode="numeric" maxLength={6} /></Form.Item>
              <Space><Button type="primary" htmlType="submit" loading={busy}>验证并开启</Button><Button onClick={() => setSetup(null)}>取消</Button></Space>
            </Form>
          </> : disabling ? <Form layout="vertical" aria-label="关闭两步验证" disabled={busy} onFinish={({ code }) => run(async () => {
            await security.disable(code.trim())
            setDisabling(false)
            reload()
          })}>
            <Form.Item name="code" label="动态码或恢复码" rules={[{ required: true, whitespace: true }]}><Input autoComplete="one-time-code" spellCheck={false} /></Form.Item>
            <Space><Button danger htmlType="submit" loading={busy}>确认关闭</Button><Button onClick={() => setDisabling(false)}>取消</Button></Space>
          </Form> : <>
            <Typography.Paragraph>{data.enabled ? '已开启。登录时需要动态码或恢复码。' : '未开启。启用后，密码和第三方登录都需要完成第二步验证。'}</Typography.Paragraph>
            {data.enabled ? <Button danger disabled={busy || loading} onClick={() => setDisabling(true)}>关闭两步验证</Button>
              : <Button disabled={busy || loading} onClick={() => void run(async () => setSetup(await security.setup()))}>绑定验证器</Button>}
          </>}
        </Card>
      </>}
    </Space>
  </main>
}

export default function AccountSecurityPage() {
  const actor = useAuthStore(s => s.user)
  return actor ? <Settings key={actor.id} /> : null
}
