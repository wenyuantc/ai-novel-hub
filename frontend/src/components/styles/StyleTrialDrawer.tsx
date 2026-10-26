import { StopOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Alert, App, Button, Drawer, Form, Input, InputNumber, Space, Typography } from 'antd'
import { useState } from 'react'
import { countWords } from '../../api/chapters'
import { trialStyle } from '../../api/styles'
import type { StyleDraft, StyleTrialRequest } from '../../api/types'
import { useStreamTask } from '../../hooks/useStreamTask'
import AIConfigSelect from '../AIConfigSelect'
import StreamOutput from '../StreamOutput'

export type TrialTarget = { kind: 'saved'; id: number; name: string } | { kind: 'draft'; draft: StyleDraft }

interface Props {
  target: TrialTarget | null
  onClose: () => void
}

/** 风格试写抽屉：填写场景描述，用该风格流式写一段（同步 SSE，不写入任何章节）。 */
export default function StyleTrialDrawer({ target, onClose }: Props) {
  const title =
    target?.kind === 'saved' ? `试写：${target.name}` : `试写：${target?.draft.name || '未保存的风格'}（按表单当前内容）`
  return (
    <Drawer title={title} width={640} open={target !== null} onClose={onClose} destroyOnHidden>
      {target && <Inner target={target} />}
    </Drawer>
  )
}

function Inner({ target }: { target: TrialTarget }) {
  const { message } = App.useApp()
  const task = useStreamTask()
  const [scene, setScene] = useState('')
  const [words, setWords] = useState(500)
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)

  const handleStart = async () => {
    const body: StyleTrialRequest = {
      scene: scene.trim(),
      words,
      ai_config_id: aiConfigId,
      ...(target.kind === 'saved' ? { style_id: target.id } : { style: target.draft }),
    }
    const outcome = await task.start((onDelta, signal) => trialStyle(body, onDelta, signal))
    if (outcome.status === 'aborted') message.info('已停止')
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Form layout="vertical" disabled={task.running}>
        <Form.Item label="场景描述" required extra="例如：雨夜，主角在废弃车站等一个不会来的人">
          <Input.TextArea
            value={scene}
            onChange={(e) => setScene(e.target.value)}
            maxLength={500}
            showCount
            autoSize={{ minRows: 3, maxRows: 6 }}
          />
        </Form.Item>
        <Space wrap>
          <Form.Item label="字数" style={{ marginBottom: 0 }}>
            <InputNumber min={200} max={1000} step={100} value={words} onChange={(v) => setWords(v ?? 500)} />
          </Form.Item>
          <Form.Item label="模型" style={{ marginBottom: 0, minWidth: 260 }}>
            <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} />
          </Form.Item>
        </Space>
      </Form>
      {task.running ? (
        <Button danger block icon={<StopOutlined />} onClick={task.stop}>
          停止（已输出 {countWords(task.text)} 字）
        </Button>
      ) : (
        <Button type="primary" block icon={<ThunderboltOutlined />} disabled={!scene.trim()} onClick={() => void handleStart()}>
          开始试写
        </Button>
      )}
      {(task.running || task.text) && <StreamOutput text={task.text} running={task.running} maxHeight={480} />}
      {task.error && !task.running && <Alert type="error" showIcon message="试写失败" description={task.error} />}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        试写只用于感受风格，不会写入任何章节，也不写生成记录。
      </Typography.Text>
    </Space>
  )
}
