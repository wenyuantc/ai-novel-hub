import { App, Button, Form, Space, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { inspirationApi } from '../../api/inspiration'
import type { InspirationSession } from '../../api/types'
import { initialScaleValues, toScale, type ScaleFormValues } from '../../utils/inspiration'
import ScaleFields from './ScaleFields'

interface Props {
  session: InspirationSession
  onSession: (s: InspirationSession) => void
  onNext: () => void
  onPrev: () => void
}

/** 规模：按目标总字数建议卷数、每卷章数与角色数（不调用 AI），可修改后保存进会话。 */
export default function ScaleStep({ session, onSession, onNext, onPrev }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<ScaleFormValues>()
  const [saving, setSaving] = useState(false)

  const handleSave = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      onSession(await inspirationApi.update(session.id, { scale: toScale(values) }))
      message.success('规模已保存')
      onNext()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={4} style={{ marginTop: 0, marginBottom: 4 }}>
          规模
        </Typography.Title>
        <Typography.Text type="secondary">
          这些数字会作为创作向导的初始参数（卷数、每卷章数、角色数），目标总字数用于项目概览的进度显示。
        </Typography.Text>
      </div>
      <Form form={form} layout="vertical" initialValues={initialScaleValues(session.state.scale)} style={{ maxWidth: 560 }}>
        <ScaleFields form={form} disabled={saving} />
      </Form>
      <Space>
        <Button onClick={onPrev}>上一步</Button>
        <Button type="primary" loading={saving} onClick={() => void handleSave()}>
          保存并下一步
        </Button>
      </Space>
    </Space>
  )
}
