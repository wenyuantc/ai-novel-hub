import { StopOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Alert, App, Button, Form, Input, Modal, Space, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { emptyStyleDraft, EXTRACT_MAX_CHARS, EXTRACT_MIN_CHARS, extractStyle, styleApi } from '../../api/styles'
import type { StyleDraft, StyleExtractDone, WritingStyle } from '../../api/types'
import { useStreamTask } from '../../hooks/useStreamTask'
import { draftToStyleBody } from '../../utils/styles'
import AIConfigSelect from '../AIConfigSelect'
import StreamOutput from '../StreamOutput'
import StyleFormFields from './StyleFormFields'

interface Props {
  open: boolean
  onClose: () => void
  onSaved: (style: WritingStyle) => void
}

/** 从样文提取风格：粘贴样文 → 流式显示模型输出 → 可编辑的草稿 → 确认后保存为新风格（提取本身不落库）。 */
export default function StyleExtractModal({ open, onClose, onSaved }: Props) {
  return (
    <Modal open={open} title="从样文提取风格" onCancel={onClose} footer={null} width={820} maskClosable={false} destroyOnHidden>
      {open && <Inner onSaved={onSaved} />}
    </Modal>
  )
}

function Inner({ onSaved }: Pick<Props, 'onSaved'>) {
  const { message } = App.useApp()
  const task = useStreamTask()
  const [sample, setSample] = useState('')
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  const [done, setDone] = useState<StyleExtractDone | null>(null)
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm<StyleDraft>()

  const chars = Array.from(sample.trim()).length
  const lengthError =
    chars < EXTRACT_MIN_CHARS
      ? `样文至少 ${EXTRACT_MIN_CHARS} 字（当前 ${chars} 字）`
      : chars > EXTRACT_MAX_CHARS
        ? `样文不超过 ${EXTRACT_MAX_CHARS} 字（当前 ${chars} 字）`
        : null

  const handleStart = async () => {
    setDone(null)
    const outcome = await task.start((onDelta, signal) =>
      extractStyle({ sample_text: sample.trim(), ai_config_id: aiConfigId }, onDelta, signal),
    )
    if (outcome.status === 'done') setDone(outcome.value)
    else if (outcome.status === 'aborted') message.info('已停止')
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    setSaving(true)
    try {
      const saved = await styleApi.create(draftToStyleBody({ ...emptyStyleDraft(), ...v }))
      message.success(`已保存为新风格「${saved.name}」`)
      onSaved(saved)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Form layout="vertical" disabled={task.running}>
        <Form.Item
          label="样文"
          required
          validateStatus={sample && lengthError ? 'warning' : undefined}
          help={sample ? lengthError ?? `${chars} 字` : `粘贴一段 ${EXTRACT_MIN_CHARS}–${EXTRACT_MAX_CHARS} 字的文字`}
        >
          <Input.TextArea value={sample} onChange={(e) => setSample(e.target.value)} autoSize={{ minRows: 6, maxRows: 14 }} />
        </Form.Item>
        <Form.Item label="模型" style={{ maxWidth: 360 }}>
          <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} />
        </Form.Item>
      </Form>
      {task.running ? (
        <Button danger block icon={<StopOutlined />} onClick={task.stop}>
          停止
        </Button>
      ) : (
        <Button type={done ? 'default' : 'primary'} block icon={<ThunderboltOutlined />} disabled={lengthError !== null} onClick={() => void handleStart()}>
          {done ? '重新提取' : '开始提取'}
        </Button>
      )}
      {task.running && <StreamOutput text={task.text} running placeholder="模型输出将在这里实时显示…" maxHeight={240} />}
      {task.error && !task.running && <Alert type="error" showIcon message="提取失败" description={task.error} />}
      {done && !task.running && (
        <>
          {done.warnings.length > 0 && (
            <Alert type="warning" showIcon message="提取提示" description={done.warnings.join('；')} />
          )}
          <Typography.Text strong>提取结果（可修改，保存后才会入库）</Typography.Text>
          <Form form={form} layout="vertical" initialValues={done.draft}>
            <StyleFormFields />
          </Form>
          <Button type="primary" loading={saving} onClick={() => void handleSave()}>
            保存为新风格
          </Button>
        </>
      )}
    </Space>
  )
}
