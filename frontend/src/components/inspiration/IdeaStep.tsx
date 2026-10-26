import { App, Button, Input, Space, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { inspirationApi } from '../../api/inspiration'
import type { InspirationSession } from '../../api/types'
import { IDEA_MAX, INSPIRATION_STEPS, stepOf } from '../../utils/inspiration'

interface Props {
  session: InspirationSession
  onSession: (s: InspirationSession) => void
  onNext: (s: InspirationSession) => void
}

/** 第 0 步「想法」：修改想法后，所有已定的步骤会被标为「可能需要更新」（保留原值）。 */
export default function IdeaStep({ session, onSession, onNext }: Props) {
  const { message, modal } = App.useApp()
  const [idea, setIdea] = useState(session.state.idea)
  const [saving, setSaving] = useState(false)
  const changed = idea.trim() !== session.state.idea
  const decided = INSPIRATION_STEPS.filter((k) => stepOf(session.state, k).status === 'decided').length

  const save = async (): Promise<InspirationSession | null> => {
    setSaving(true)
    try {
      const s = await inspirationApi.update(session.id, { idea: idea.trim() })
      onSession(s)
      message.success('想法已保存')
      return s
    } catch (err) {
      message.error(errorMessage(err))
      return null
    } finally {
      setSaving(false)
    }
  }

  const handleNext = async () => {
    if (!idea.trim()) {
      message.warning('请写下你的想法')
      return
    }
    if (!changed) {
      onNext(session)
      return
    }
    const run = async () => {
      const s = await save()
      if (s) onNext(s)
    }
    if (decided > 0) {
      modal.confirm({
        title: '修改想法？',
        content: `已定的 ${decided} 个步骤会保留，但都会标为「可能需要更新」。`,
        okText: '保存并继续',
        cancelText: '取消',
        onOk: run,
      })
    } else {
      await run()
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={4} style={{ marginTop: 0, marginBottom: 4 }}>
          想法
        </Typography.Title>
        <Typography.Text type="secondary">
          一句话的点子就够了。之后每一步 AI 都会参考它和你的补充想法给出选项。
        </Typography.Text>
      </div>
      {session.state.genre && (
        <Typography.Text>
          题材（来自项目）：<Typography.Text strong>{session.state.genre}</Typography.Text>
        </Typography.Text>
      )}
      <Input.TextArea
        value={idea}
        onChange={(e) => setIdea(e.target.value)}
        maxLength={IDEA_MAX}
        showCount
        autoSize={{ minRows: 4, maxRows: 12 }}
      />
      <Button type="primary" loading={saving} onClick={() => void handleNext()}>
        {changed ? '保存并下一步' : '下一步'}
      </Button>
    </Space>
  )
}
