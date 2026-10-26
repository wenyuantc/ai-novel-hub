import { RocketOutlined, SyncOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Descriptions, Form, Input, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { inspirationApi } from '../../api/inspiration'
import type { InspirationSession } from '../../api/types'
import {
  decidedTitle,
  draftSummary,
  initialScaleValues,
  STEP_DISPLAY,
  staleSummary,
  toScale,
  type ScaleFormValues,
} from '../../utils/inspiration'
import ApplyToProjectModal from './ApplyToProjectModal'
import ScaleFields from './ScaleFields'

interface Props {
  session: InspirationSession
  onSession: (s: InspirationSession) => void
  onPrev: () => void
}

/** 确认一步：汇总已定内容；向导参数可在这里调整；「创建项目并进入向导」或「应用到已有项目」（逐字段对比）。 */
export default function ConfirmStep({ session, onSession, onPrev }: Props) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const state = session.state
  const title = decidedTitle(state)
  const [titleOverride, setTitleOverride] = useState('')
  const [form] = Form.useForm<ScaleFormValues>()
  const [converting, setConverting] = useState(false)
  const [applyOpen, setApplyOpen] = useState(false)
  const [savingScale, setSavingScale] = useState(false)
  const stale = staleSummary(state)
  const summary = draftSummary(state)

  const handleConvert = async () => {
    const values = await form.validateFields()
    if (!title && !titleOverride.trim()) {
      message.warning('请先定下书名，或在这里填写书名')
      return
    }
    setConverting(true)
    try {
      const p = await inspirationApi.convert(session.id, {
        title_override: title ? null : titleOverride.trim(),
        scale: toScale(values),
      })
      message.success(`已创建《${p.title}》，进入创作向导`)
      navigate(`/projects/${p.id}/wizard`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setConverting(false)
    }
  }

  /** 先把确认页的规模存回会话（应用对比里的目标总字数取自会话），再打开对比 */
  const handleOpenApply = async () => {
    const values = await form.validateFields()
    setSavingScale(true)
    try {
      onSession(await inspirationApi.update(session.id, { scale: toScale(values) }))
      setApplyOpen(true)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSavingScale(false)
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        确认立项
      </Typography.Title>
      {session.status === 'converted' && session.converted_project_id !== null && (
        <Alert
          type="success"
          showIcon
          message="这个会话已经转为项目"
          description={
            <span>
              <Link to={`/projects/${session.converted_project_id}/wizard`}>去项目的创作向导</Link>
              ；项目被删除后才能再次创建。之后在这里的修改可以用「应用到已有项目」同步过去。
            </span>
          }
        />
      )}
      {stale && <Alert type="warning" showIcon message={`${stale}，可以回到对应步骤处理后再确认`} />}
      <Card size="small" title="立项信息">
        <Descriptions column={1} size="small" styles={{ label: { width: 100 } }}>
          <Descriptions.Item label="想法">
            <span style={{ whiteSpace: 'pre-wrap' }}>{state.idea}</span>
          </Descriptions.Item>
          {state.genre && <Descriptions.Item label="题材">{state.genre}</Descriptions.Item>}
          {summary.map((it) => (
            <Descriptions.Item key={it.key} label={it.label}>
              {it.text ? (
                <span style={{ whiteSpace: 'pre-wrap' }}>
                  {it.text}
                  {it.status === 'stale' && (
                    <Tag color={STEP_DISPLAY.stale.color} style={{ marginLeft: 8 }}>
                      {STEP_DISPLAY.stale.label}
                    </Tag>
                  )}
                </span>
              ) : (
                <Tag color={STEP_DISPLAY[it.status].color}>{STEP_DISPLAY[it.status].label}</Tag>
              )}
            </Descriptions.Item>
          ))}
        </Descriptions>
        {!title && (
          <Form layout="vertical" style={{ marginTop: 12 }}>
            <Form.Item label="书名（还没有定下书名，创建项目前需要填写）" required>
              <Input value={titleOverride} maxLength={200} onChange={(e) => setTitleOverride(e.target.value)} />
            </Form.Item>
          </Form>
        )}
      </Card>
      <Card size="small" title="向导参数（创建项目后预填到创作向导）">
        <Form form={form} layout="vertical" initialValues={initialScaleValues(state.scale)}>
          <ScaleFields form={form} disabled={converting} />
        </Form>
      </Card>
      <Space wrap>
        <Button onClick={onPrev}>上一步</Button>
        <Button type="primary" icon={<RocketOutlined />} loading={converting} onClick={() => void handleConvert()}>
          创建项目并进入向导
        </Button>
        <Button icon={<SyncOutlined />} loading={savingScale} onClick={() => void handleOpenApply()}>
          应用到已有项目
        </Button>
      </Space>
      <ApplyToProjectModal
        sessionId={session.id}
        defaultProjectId={session.project_id ?? session.converted_project_id}
        open={applyOpen}
        onClose={() => setApplyOpen(false)}
      />
    </Space>
  )
}
