import { CheckOutlined, EditOutlined, ReloadOutlined, StopOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Collapse, Empty, Form, Input, Select, Space, Tag, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { generateOptions, inspirationApi, retryText } from '../../api/inspiration'
import type { InspirationRound, InspirationSession, InspirationStepKey, InspirationStepUpdate } from '../../api/types'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useStreamTask } from '../../hooks/useStreamTask'
import {
  FEEDBACK_MAX,
  FLOW_LABELS,
  shouldSuggestGenerate,
  staleText,
  STEP_HINTS,
  STEP_SOURCE_LABELS,
  STEP_VALUE_LIMITS,
  stepOf,
  tagsSource,
  valueText,
} from '../../utils/inspiration'
import { normalizeTags, tagsError } from '../../utils/projectFields'
import AIConfigSelect from '../AIConfigSelect'
import StreamOutput from '../StreamOutput'

interface Props {
  session: InspirationSession
  step: InspirationStepKey
  onSession: (s: InspirationSession) => void
  /** 决定 / 跳过 / 保持不变之后前进（参数为最新会话） */
  onAdvance: (s: InspirationSession) => void
  onPrev: () => void
  /** 生成中时父组件禁止切换步骤 */
  onRunningChange?: (running: boolean) => void
}

/**
 * 灵感模式的一个 AI 步骤：生成 / 换一批（可附反馈，流式显示原文，retry 时清空并提示）、选项卡片（直接选 / 编辑后选；类型标签多选 + 自定义）、
 * 历史轮次、自己填、跳过、可能需要更新时的「保持不变」。卸载时中止进行中的生成（后端不保存半截选项）。
 */
export default function OptionStep({ session, step, onSession, onAdvance, onPrev, onRunningChange }: Props) {
  const { message } = App.useApp()
  const entry = stepOf(session.state, step)
  const label = FLOW_LABELS[step]
  const stream = useStreamTask()
  const hasConfig = useHasAIConfig()
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  const [feedback, setFeedback] = useState('')
  const [retryMsg, setRetryMsg] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [manualText, setManualText] = useState<string | null>(null)
  const [selectedTags, setSelectedTags] = useState<string[]>(
    entry.status === 'decided' && Array.isArray(entry.value) ? entry.value : [],
  )
  const rounds = entry.rounds
  const latest: InspirationRound | undefined = rounds[rounds.length - 1]
  const history = rounds.slice(0, -1).map((r, i) => ({ r, n: i + 1 })).reverse()
  const busy = stream.running || saving

  const handleGenerate = async () => {
    setRetryMsg(null)
    onRunningChange?.(true)
    const outcome = await stream.start((onDelta, signal) =>
      generateOptions(
        session.id,
        step,
        { feedback: feedback.trim(), ai_config_id: aiConfigId },
        {
          onDelta,
          signal,
          onRetry: (ev) => {
            stream.reset()
            setRetryMsg(retryText(ev))
          },
        },
      ),
    )
    onRunningChange?.(false)
    setRetryMsg(null)
    if (outcome.status === 'done') {
      onSession(outcome.value.session)
      setFeedback('')
      stream.reset()
      message.success(`已生成 ${outcome.value.round.options.length} 个选项`)
    } else if (outcome.status === 'aborted') {
      message.info('已停止，本次选项未保存')
    }
  }

  const patchStep = async (body: InspirationStepUpdate, advance: boolean, okText?: string): Promise<boolean> => {
    setSaving(true)
    try {
      const s = await inspirationApi.updateStep(session.id, step, body)
      onSession(s)
      if (okText) message.success(okText)
      if (advance) onAdvance(s)
      return true
    } catch (err) {
      message.error(errorMessage(err))
      return false
    } finally {
      setSaving(false)
    }
  }

  const decide = (value: string | string[], source: 'ai' | 'edited' | 'manual') =>
    patchStep({ action: 'decide', value, source }, true, `${label}已定`)

  const decideTags = () => {
    const tags = normalizeTags(selectedTags)
    if (tags.length === 0) {
      message.warning('请至少选择一个类型标签')
      return
    }
    const err = tagsError(tags)
    if (err) {
      message.warning(err)
      return
    }
    void decide(tags, tagsSource(tags, rounds))
  }

  const toggleTag = (tag: string, checked: boolean) =>
    setSelectedTags((prev) => (checked ? normalizeTags([...prev, tag]) : prev.filter((t) => t !== tag)))

  const renderOptions = (round: InspirationRound) => {
    if (step === 'tags') {
      return (
        <Space wrap size={[8, 8]}>
          {round.options.map((opt) => (
            <Tag.CheckableTag
              key={opt}
              checked={selectedTags.includes(opt)}
              onChange={(c) => toggleTag(opt, c)}
              style={{ fontSize: 14, padding: '4px 12px', border: '1px solid #d9d9d9' }}
            >
              {opt}
            </Tag.CheckableTag>
          ))}
        </Space>
      )
    }
    return (
      <Space direction="vertical" style={{ width: '100%' }}>
        {round.options.map((opt, i) => (
          <OptionCard
            key={`${i}-${opt}`}
            text={opt}
            chosen={entry.status === 'decided' && entry.value === opt}
            disabled={busy}
            limit={STEP_VALUE_LIMITS[step as Exclude<InspirationStepKey, 'tags'>]}
            onPick={(text, edited) => void decide(text, edited ? 'edited' : 'ai')}
          />
        ))}
      </Space>
    )
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={4} style={{ marginTop: 0, marginBottom: 4 }}>
          {label}
        </Typography.Title>
        <Typography.Text type="secondary">{STEP_HINTS[step]}</Typography.Text>
      </div>

      {entry.status === 'decided' && (
        <Alert
          type={entry.stale ? 'warning' : 'success'}
          showIcon
          message={
            <span>
              已定{entry.source ? `（${STEP_SOURCE_LABELS[entry.source]}）` : ''}：
              <Typography.Text strong style={{ whiteSpace: 'pre-wrap' }}>
                {valueText(entry.value)}
              </Typography.Text>
            </span>
          }
          description={entry.stale ? staleText(step) : undefined}
          action={
            <Space direction="vertical">
              {entry.stale && (
                <Button size="small" type="primary" disabled={busy} onClick={() => void patchStep({ action: 'keep' }, true, '已保持不变')}>
                  保持不变
                </Button>
              )}
              <Button size="small" disabled={busy} onClick={() => void patchStep({ action: 'clear' }, false, '已改回待定')}>
                改回待定
              </Button>
            </Space>
          }
        />
      )}
      {entry.status === 'skipped' && (
        <Alert
          type="info"
          showIcon
          message="这一步已跳过"
          action={
            <Button size="small" disabled={busy} onClick={() => void patchStep({ action: 'clear' }, false)}>
              撤销跳过
            </Button>
          }
        />
      )}

      <Card size="small" title="AI 选项">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Form layout="vertical" disabled={stream.running}>
            <Form.Item label="AI 配置" style={{ marginBottom: 8 }}>
              <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} disabled={stream.running} />
            </Form.Item>
            {(rounds.length > 0 || entry.status !== 'pending') && (
              <Form.Item label="对这一批的意见（可选，换一批时带上；之前各轮的意见也会一起带上）" style={{ marginBottom: 8 }}>
                <Input.TextArea
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  maxLength={FEEDBACK_MAX}
                  showCount
                  autoSize={{ minRows: 1, maxRows: 4 }}
                  placeholder="例如：更短一些、不要太中二、突出悬疑感"
                />
              </Form.Item>
            )}
          </Form>
          <Space wrap>
            {stream.running ? (
              <Button danger icon={<StopOutlined />} onClick={stream.stop}>
                停止
              </Button>
            ) : shouldSuggestGenerate(session.state, step) ? (
              <Button type="primary" icon={<ThunderboltOutlined />} disabled={busy || hasConfig === false} onClick={() => void handleGenerate()}>
                让 AI 给出选项
              </Button>
            ) : (
              <Button icon={<ReloadOutlined />} disabled={busy || hasConfig === false} onClick={() => void handleGenerate()}>
                {rounds.length > 0 ? '换一批' : '生成选项'}
              </Button>
            )}
          </Space>
          {retryMsg && stream.running && <Alert type="warning" showIcon message={retryMsg} />}
          {stream.running && <StreamOutput text={stream.text} running placeholder="AI 正在构思选项…" maxHeight={240} />}
          {stream.error && !stream.running && <Alert type="error" showIcon message="生成失败" description={stream.error} />}
          {!stream.running &&
            (latest ? (
              <div>
                {latest.feedback && (
                  <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
                    本轮意见：{latest.feedback}
                  </Typography.Paragraph>
                )}
                {renderOptions(latest)}
              </div>
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={entry.status === 'decided' ? '这一步已经定下，需要时可以「生成选项」看看别的' : '还没有选项'}
              />
            ))}
          {history.length > 0 && !stream.running && (
            <Collapse
              size="small"
              items={history.map(({ r, n }) => ({
                key: String(n),
                label: (
                  <span>
                    第 {n} 轮{r.feedback ? `（意见：${r.feedback}）` : ''}
                  </span>
                ),
                children: renderOptions(r),
              }))}
            />
          )}
        </Space>
      </Card>

      {step === 'tags' && (
        <Card size="small" title="已选标签（可以自定义输入，回车添加）">
          <Space direction="vertical" style={{ width: '100%' }}>
            <Select
              mode="tags"
              value={selectedTags}
              onChange={(v: string[]) => setSelectedTags(normalizeTags(v))}
              open={false}
              suffixIcon={null}
              tokenSeparators={[',', '，', '、']}
              placeholder="点选上面的候选标签，或者输入自己的标签"
              style={{ width: '100%' }}
              disabled={busy}
            />
            {tagsError(selectedTags) && <Typography.Text type="danger">{tagsError(selectedTags)}</Typography.Text>}
            <Button type="primary" icon={<CheckOutlined />} disabled={busy} onClick={decideTags}>
              确定这些标签
            </Button>
          </Space>
        </Card>
      )}

      {step !== 'tags' && manualText !== null && (
        <Card size="small" title={`自己填写${label}`}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <Input.TextArea
              value={manualText}
              onChange={(e) => setManualText(e.target.value)}
              maxLength={STEP_VALUE_LIMITS[step]}
              showCount
              autoSize={{ minRows: 2, maxRows: 10 }}
            />
            <Space>
              <Button
                type="primary"
                disabled={busy || !manualText.trim()}
                onClick={() => void decide(manualText.trim(), 'manual').then((ok) => ok && setManualText(null))}
              >
                确定
              </Button>
              <Button onClick={() => setManualText(null)}>取消</Button>
            </Space>
          </Space>
        </Card>
      )}

      <Space wrap>
        <Button disabled={stream.running} onClick={onPrev}>
          上一步
        </Button>
        {step !== 'tags' && manualText === null && (
          <Button
            icon={<EditOutlined />}
            disabled={busy}
            onClick={() => setManualText(entry.status === 'decided' ? valueText(entry.value) : '')}
          >
            自己填
          </Button>
        )}
        <Button disabled={busy} onClick={() => void patchStep({ action: 'skip' }, true, '已跳过')}>
          跳过这一步
        </Button>
        {entry.status !== 'pending' && !entry.stale && (
          <Button type="link" disabled={stream.running} onClick={() => onAdvance(session)}>
            下一步
          </Button>
        )}
      </Space>
    </Space>
  )
}

/** 文本选项卡片：直接选，或者编辑后再选 */
function OptionCard({
  text,
  chosen,
  disabled,
  limit,
  onPick,
}: {
  text: string
  chosen: boolean
  disabled: boolean
  limit: number
  onPick: (text: string, edited: boolean) => void
}) {
  const [draft, setDraft] = useState<string | null>(null)
  if (draft !== null) {
    return (
      <Card size="small" style={{ borderColor: '#1677ff' }}>
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input.TextArea value={draft} onChange={(e) => setDraft(e.target.value)} maxLength={limit} showCount autoSize={{ minRows: 1, maxRows: 10 }} />
          <Space>
            <Button
              type="primary"
              size="small"
              disabled={disabled || !draft.trim()}
              onClick={() => {
                const t = draft.trim()
                onPick(t, t !== text)
              }}
            >
              用这个
            </Button>
            <Button size="small" onClick={() => setDraft(null)}>
              取消
            </Button>
          </Space>
        </Space>
      </Card>
    )
  }
  return (
    <Card size="small" style={chosen ? { borderColor: '#52c41a', background: '#f6ffed' } : undefined}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
        <Typography.Paragraph style={{ flex: 1, marginBottom: 0, whiteSpace: 'pre-wrap' }}>{text}</Typography.Paragraph>
        <Space direction="vertical" size={4}>
          <Button type={chosen ? 'default' : 'primary'} size="small" disabled={disabled} onClick={() => onPick(text, false)}>
            {chosen ? '已选' : '选这个'}
          </Button>
          <Button size="small" disabled={disabled} onClick={() => setDraft(text)}>
            编辑后选
          </Button>
        </Space>
      </div>
    </Card>
  )
}
