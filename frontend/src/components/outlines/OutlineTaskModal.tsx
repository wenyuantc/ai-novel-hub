import { Alert, App, Button, Form, Input, InputNumber, Modal, Progress, Space, Spin, Switch, Typography } from 'antd'
import { Suspense, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { keptPlans, outlineProApi } from '../../api/outlines'
import { queueLabel, TASK_KIND_LABELS, taskApi } from '../../api/tasks'
import type { OutlineDetail, Task } from '../../api/types'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useTasksStore } from '../../stores/tasks'
import AIConfigSelect from '../AIConfigSelect'
import { TASK_RESULT_VIEWERS } from '../entities/taskResultViewers'
import StreamOutput from '../StreamOutput'

export type OutlineTaskKind = 'outline.continue' | 'outline.expand'

interface Props {
  projectId: number
  kind: OutlineTaskKind
  open: boolean
  onClose: () => void
  /** outline.expand 的目标大纲 */
  outline?: OutlineDetail | null
  /** 采纳（写入了数据）后回调 */
  onSaved?: () => void
}

interface FormValues {
  count: number
  direction: string
  new_volume: boolean
  volume_title: string
  instruction: string
}

/**
 * 大纲 AI 任务弹窗（续写 / 展开）：填写参数 → 创建后台任务并订阅 → 完成后展示注册表中的可编辑预览 → 确认采纳。
 * 运行中可以关闭（任务在后台继续，完成后可在任务面板「查看结果」）；打开时接上活动任务：续写按 project + kind，
 * 展开再按 TaskOut.outline_id 区分大纲。
 */
export default function OutlineTaskModal(props: Props) {
  if (!props.open) return null
  return <Inner {...props} />
}

function Inner({ projectId, kind, onClose, outline, onSaved }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  const [starting, setStarting] = useState(false)
  const [result, setResult] = useState<unknown>(undefined)
  const stream = useTaskStream()
  const trackTask = useTasksStore((s) => s.track)
  const hasConfig = useHasAIConfig()
  const running = stream.running || starting
  const Viewer = TASK_RESULT_VIEWERS[kind]
  const outlineId = outline?.id
  const keptCount = outline ? keptPlans(outline.plans).length : 0
  const minCount = Math.max(2, keptCount + 1)
  const newVolume = Form.useWatch('new_volume', form)

  const followTask = async (t: Task) => {
    setResult(undefined)
    const outcome = await stream.follow(t)
    if (outcome.status === 'succeeded') setResult(outcome.result)
    else if (outcome.status === 'cancelled') message.info('已停止，未写入任何数据')
  }
  const followRef = useRef(followTask)
  useEffect(() => {
    followRef.current = followTask
  })

  useEffect(() => {
    if (kind === 'outline.expand' && outlineId === undefined) return
    let alive = true
    taskApi
      .list({ project_id: projectId, kind, active: true, limit: kind === 'outline.expand' ? 50 : 1 })
      .then((list) => {
        const found = kind === 'outline.expand' ? list.find((t) => t.outline_id === outlineId) : list[0]
        if (alive && found) void followRef.current(found)
      })
      .catch(() => {
        // 查询失败按无活动任务处理
      })
    return () => {
      alive = false
    }
  }, [projectId, kind, outlineId])

  const handleStart = async () => {
    const v = await form.validateFields()
    setStarting(true)
    let created: Task
    try {
      if (kind === 'outline.continue') {
        created = await outlineProApi.continue(projectId, {
          ai_config_id: aiConfigId,
          count: v.count,
          direction: v.direction?.trim() ?? '',
          new_volume: v.new_volume ? { title: v.volume_title?.trim() ?? '' } : null,
        })
      } else {
        created = await outlineProApi.expand(projectId, outlineId as number, {
          ai_config_id: aiConfigId,
          count: v.count,
          instruction: v.instruction?.trim() ?? '',
        })
      }
    } catch (err) {
      message.error(errorMessage(err))
      setStarting(false)
      return
    }
    setStarting(false)
    trackTask(created)
    await followTask(created)
  }

  const handleCancel = async () => {
    try {
      await stream.cancel()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const handleClose = () => {
    if (running) message.info('任务在后台继续，完成后可在右下角任务面板「查看结果」')
    onClose()
  }

  const title = kind === 'outline.expand' ? `AI 展开细纲 · 第 ${outline?.chapter_no ?? ''} 章 ${outline?.title ?? ''}` : TASK_KIND_LABELS[kind]
  const showForm = result === undefined

  return (
    <Modal open title={title} footer={null} width={960} maskClosable={false} onCancel={handleClose} destroyOnHidden>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {showForm && (
          <>
            {kind === 'outline.expand' && outline && (
              <Alert
                type="info"
                showIcon
                message={outline.summary || '（该大纲没有梗概）'}
                description={
                  outline.plan_count > 0
                    ? `已有 ${outline.plan_count} 章细纲${keptCount > 0 ? `，其中 ${keptCount} 章已有正文会保留` : ''}；采纳后替换其余细纲（替换前自动保存快照）。`
                    : '把这一条章纲展开成多章细纲，采纳后可在大纲页建章。'
                }
              />
            )}
            <Form
              form={form}
              layout="vertical"
              disabled={running}
              initialValues={{
                count: kind === 'outline.expand' ? Math.max(3, minCount) : 5,
                direction: '',
                new_volume: false,
                volume_title: '',
                instruction: '',
              }}
            >
              <Form.Item label="AI 配置">
                <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} disabled={running} />
              </Form.Item>
              {kind === 'outline.continue' ? (
                <>
                  <Space size="large" wrap align="start">
                    <Form.Item name="count" label="续写条数" rules={[{ required: true, message: '请输入条数' }]}>
                      <InputNumber min={1} max={20} style={{ width: 120 }} />
                    </Form.Item>
                    <Form.Item name="new_volume" label="开新卷" valuePropName="checked">
                      <Switch />
                    </Form.Item>
                    {newVolume && (
                      <Form.Item name="volume_title" label="新卷名（可选）" rules={[{ max: 200, message: '不超过 200 字' }]}>
                        <Input style={{ width: 240 }} placeholder="留空由 AI 拟定" />
                      </Form.Item>
                    )}
                  </Space>
                  <Form.Item name="direction" label="发展方向（可选）" rules={[{ max: 2000, message: '不超过 2000 字' }]}>
                    <Input.TextArea
                      autoSize={{ minRows: 2, maxRows: 6 }}
                      placeholder="例如：主角离开宗门历练，途中遭遇旧敌，本卷末回收玉佩伏笔"
                    />
                  </Form.Item>
                  <Typography.Paragraph type="secondary">
                    AI 会参考前文的章节摘要、最近的章纲与未回收的伏笔，生成可编辑的预览，确认后才追加到大纲末尾。
                  </Typography.Paragraph>
                </>
              ) : (
                <>
                  <Form.Item
                    name="count"
                    label="展开为几章（含保留的有正文细纲）"
                    rules={[{ required: true, message: '请输入章数' }]}
                  >
                    <InputNumber min={minCount} max={10} style={{ width: 120 }} />
                  </Form.Item>
                  <Form.Item name="instruction" label="要求（可选）" rules={[{ max: 2000, message: '不超过 2000 字' }]}>
                    <Input.TextArea
                      autoSize={{ minRows: 2, maxRows: 6 }}
                      placeholder="例如：前两章铺垫，第三章正面冲突，节奏紧凑"
                    />
                  </Form.Item>
                </>
              )}
            </Form>
            <Space>
              {running ? (
                <Button danger disabled={!stream.running} onClick={() => void handleCancel()}>
                  {stream.task?.status === 'queued' ? '取消排队' : '停止'}
                </Button>
              ) : (
                <Button
                  type="primary"
                  onClick={() => void handleStart()}
                  disabled={hasConfig === false || (kind === 'outline.expand' && minCount > 10)}
                >
                  {stream.text ? '重新开始' : '开始'}
                </Button>
              )}
              {kind === 'outline.expand' && minCount > 10 && (
                <Typography.Text type="danger">已有正文的细纲过多，无法再展开</Typography.Text>
              )}
            </Space>
          </>
        )}
        {stream.running && stream.task?.status === 'queued' && <Alert type="info" showIcon message={queueLabel(stream.task)} />}
        {stream.running && stream.task?.status === 'running' && (
          <div>
            <Progress percent={stream.task.progress} size="small" status="active" style={{ marginBottom: 0 }} />
            {stream.task.stage && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {stream.task.stage}
              </Typography.Text>
            )}
          </div>
        )}
        {stream.reconnecting && <Alert type="warning" showIcon message="连接中断，正在重连…（任务仍在后台运行）" />}
        {stream.error && !running && <Alert type="error" showIcon message="生成失败" description={stream.error} />}
        {showForm && (stream.running || stream.text) && <StreamOutput text={stream.text} running={stream.running} maxHeight={240} />}
        {!showForm && Viewer && (
          <>
            <Suspense fallback={<Spin />}>
              <Viewer projectId={projectId} result={result} onSaved={onSaved} />
            </Suspense>
            <Button onClick={() => setResult(undefined)}>返回重新生成</Button>
          </>
        )}
      </Space>
    </Modal>
  )
}
