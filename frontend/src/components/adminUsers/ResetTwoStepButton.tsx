import { Alert, Button, Checkbox, Modal, Typography } from 'antd'
import { useState } from 'react'
import { accountSecurityApi } from '../../api/accountSecurity'
import { errorMessage } from '../../api/client'
import type { AdminUserDetail } from '../../api/types'

export default function ResetTwoStepButton({ user, onChanged, disabled }: { user: AdminUserDetail; onChanged: () => void; disabled: boolean }) {
  const [open, setOpen] = useState(false)
  const [verified, setVerified] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submit = async () => {
    if (!verified || busy || disabled) return
    setBusy(true)
    try {
      await accountSecurityApi.reset(user.id)
      setOpen(false)
      onChanged()
    } catch (err) { setError(errorMessage(err)) }
    finally { setBusy(false) }
  }
  return <>
    <Button danger disabled={disabled} onClick={() => { setOpen(true); setVerified(false); setError(null) }}>重置两步验证</Button>
    <Modal open={open} title="重置两步验证" onCancel={() => { if (!busy) setOpen(false) }} onOk={() => void submit()} okText="确认重置" cancelText="取消" confirmLoading={busy} okButtonProps={{ danger: true, disabled: !verified || disabled }}>
      <Typography.Paragraph>重置后，{user.username}（#{user.id}）的旧验证器、恢复码和已登录会话将失效。此次操作将写入审计日志。</Typography.Paragraph>
      <Checkbox disabled={busy} checked={verified} onChange={e => setVerified(e.target.checked)}>我已核实该用户的身份</Checkbox>
      {error && <Alert role="alert" type="error" showIcon message={error} />}
    </Modal>
  </>
}
