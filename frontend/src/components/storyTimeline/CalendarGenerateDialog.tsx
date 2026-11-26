import { Alert, Button, Form, Input, Modal, Space } from 'antd'
import { useCallback, useState } from 'react'
import { ApiError } from '../../api/client'
import { storyTimelineApi, type GeneratedCalendar, type StoryCalendar } from '../../api/storyTimeline'
import { useAIModelSource } from '../../hooks/useAIModelSource'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import AIConfigSelect from '../AIConfigSelect'
import { useSerializationAction } from '../serialization/useSerializationAction'

interface Props {
  pid: number
  revision: number
  onClose: () => void
  onBaseChanged: (latest: StoryCalendar) => void
  onAdopt: (draft: GeneratedCalendar) => void
}

export default function CalendarGenerateDialog({ pid, revision, onClose, onBaseChanged, onAdopt }: Props) {
  const { canEditNow, canUseAINow } = useProjectAccess(pid)
  const canGenerate = useCallback(() => canEditNow() && canUseAINow(), [canEditNow, canUseAINow])
  const generate = useSerializationAction(canGenerate)
  const source = useAIModelSource('text')
  const [configId, setConfigId] = useState<number | null>(null)
  const [instruction, setInstruction] = useState('')
  const [suggestion, setSuggestion] = useState<GeneratedCalendar>()
  const [stale, setStale] = useState(false)
  const available = source === 'own' || source === 'owner'

  function requestDraft() {
    if (!available || !canGenerate()) return
    setStale(false)
    setSuggestion(undefined)
    return generate.run(async (opts) => {
      try {
        const draft = await storyTimelineApi.generateCalendar(pid, {
          expected_revision: revision, ai_config_id: source === 'owner' ? null : configId,
          instruction: instruction.trim(),
        }, opts)
        if (draft.revision === revision) return { draft }
      } catch (err) {
        if (!(err instanceof ApiError) || err.body?.code !== 'story_timeline_changed') throw err
      }
      return { latest: await storyTimelineApi.calendar(pid, opts) }
    }, ({ draft, latest }) => {
      if (draft) setSuggestion(draft)
      else if (latest) { onBaseChanged(latest); setStale(true) }
    })
  }

  function adopt() {
    if (suggestion?.revision === revision && canGenerate()) onAdopt(suggestion)
  }

  return <Modal open title="AI 生成历法" footer={null} onCancel={onClose}>
    <Form layout="vertical" onFinish={requestDraft}>
      <Form.Item label="生成要求（可选）" htmlFor="calendar-instruction"
        extra="模型会参考书名、题材、故事简介和已保存的历法。采用草稿后仍可编辑。">
        <Input.TextArea id="calendar-instruction" autoFocus rows={3} maxLength={2000} disabled={generate.busy}
          placeholder="例如：一年四个月，按潮汐命名，每月 30 天" value={instruction}
          onChange={(e) => { setInstruction(e.target.value); setSuggestion(undefined) }} />
      </Form.Item>
      <Form.Item label="文本模型" htmlFor="calendar-ai-config">
        <AIConfigSelect id="calendar-ai-config" value={configId} onChange={(id) => { setConfigId(id); setSuggestion(undefined) }}
          disabled={generate.busy} />
      </Form.Item>
      <Button htmlType="submit" disabled={!available} loading={generate.busy}>{suggestion ? '重新生成' : '生成草稿'}</Button>
    </Form>
    {generate.error && <Alert type="error" showIcon message={generate.error} />}
    {stale && <Alert type="warning" showIcon message="作品信息或历法已更新，请重新生成草稿。" />}
    {suggestion && <section className="story-calendar-suggestion" aria-label="历法草稿" aria-live="polite">
      <h3>{suggestion.era_name}</h3>
      <ol>{suggestion.months.map((month) => <li key={month.id}>{month.name} · {month.days} 天</li>)}</ol>
      <Space wrap>
        <Button type="primary" onClick={adopt}>采用草稿</Button>
        <Button onClick={onClose}>保留原输入</Button>
      </Space>
    </section>}
  </Modal>
}
