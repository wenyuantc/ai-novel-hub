import { Alert, App, Button, Col, Form, Input, InputNumber, Progress, Row, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../api/client'
import { queueLabel, taskApi, wizardTaskKind } from '../api/tasks'
import type { Task, WizardItemMap, WizardKind, WizardRequest } from '../api/types'
import { parseWizardDone, parseWizardWarnings, runWizard, WIZARD_KIND_LABELS } from '../api/wizard'
import { useHasAIConfig } from '../hooks/useHasAIConfig'
import { useTaskStream } from '../hooks/useTaskStream'
import { useTasksStore } from '../stores/tasks'
import AIConfigSelect from './AIConfigSelect'
import StreamOutput from './StreamOutput'
import { CareerPreview, CharacterPreview, OutlinePreview, WorldPreview } from './StoryPreview'

interface Props<K extends WizardKind> {
  projectId: number
  kind: K
  /** 当前已有条目数：大于 0 时提示生成会覆盖 */
  existingCount?: number
  /** 生成完成后是否在下方展示结构化结果 */
  showResult?: boolean
  onDone?: (items: WizardItemMap[K][]) => void
  onRunningChange?: (running: boolean) => void
}

interface FormValues {
  instruction: string
  volume_count: number
  chapters_per_volume: number
  character_count: number
  career_count: number
}

const PLACEHOLDERS: Record<WizardKind, string> = {
  world: '例如：修仙世界，灵气复苏背景，突出宗门势力与等级体系',
  careers: '例如：主修以剑修、体修为主，九个大境界；副修包含炼丹、炼器、阵法',
  characters: '例如：主角是隐忍型，反派要有可信的动机，加入一位亦正亦邪的导师',
  outline: '例如：前期节奏快，第一卷结尾安排一次重大反转',
}

/** AI 向导生成器（世界观 / 职业体系 / 角色 / 大纲）：创建后台任务并订阅实时原文，挂载时自动接上进行中的同类任务，完成后回调结构化结果。 */
export default function WizardGenerator<K extends WizardKind>({
  projectId,
  kind,
  existingCount = 0,
  showResult = false,
  onDone,
  onRunningChange,
}: Props<K>) {
  const { message, modal } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const [warnings, setWarnings] = useState<string[]>([])
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  const [result, setResult] = useState<WizardItemMap[K][] | null>(null)
  const task = useTaskStream()
  const [starting, setStarting] = useState(false)
  const trackTask = useTasksStore((s) => s.track)
  const hasConfig = useHasAIConfig()
  const label = WIZARD_KIND_LABELS[kind]
  const running = task.running || starting

  // onRunningChange 用 ref 保存最新回调，避免父组件内联函数导致 effect 反复执行
  const runningCbRef = useRef(onRunningChange)
  useEffect(() => {
    runningCbRef.current = onRunningChange
  })
  useEffect(() => {
    runningCbRef.current?.(running)
  }, [running])

  const followTask = async (t: Task) => {
    const outcome = await task.follow(t)
    if (outcome.status === 'succeeded') {
      const items = parseWizardDone(kind, outcome.result) ?? []
      const warns = parseWizardWarnings(outcome.result)
      setResult(items)
      setWarnings(warns)
      message.success(`${label}生成完成，共 ${items.length} 条`)
      // 弹窗模式（showResult=false）完成即关闭，警告用对话框展示，避免丢失
      if (warns.length > 0 && !showResult) {
        modal.warning({
          title: `${label}已生成，但有 ${warns.length} 条提示`,
          content: (
            <ul style={{ paddingInlineStart: 20, marginBottom: 0 }}>
              {warns.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          ),
        })
      }
      onDone?.(items)
    } else if (outcome.status === 'cancelled') {
      message.info('已停止生成，原有数据未改变')
    }
  }
  const followRef = useRef(followTask)
  useEffect(() => {
    followRef.current = followTask
  })

  // 挂载时接上同项目同类向导的活动任务（关闭页面 / 弹窗后重新打开）
  useEffect(() => {
    let alive = true
    taskApi
      .list({ project_id: projectId, kind: wizardTaskKind(kind), active: true, limit: 1 })
      .then((list) => {
        if (alive && list[0]) void followRef.current(list[0])
      })
      .catch(() => {
        // 忽略：查询失败时按无活动任务处理
      })
    return () => {
      alive = false
    }
  }, [projectId, kind])

  const handleCancel = async () => {
    try {
      await task.cancel()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const handleGenerate = async () => {
    const values = await form.validateFields()
    const body: WizardRequest = { ai_config_id: aiConfigId, instruction: values.instruction?.trim() ?? '' }
    if (kind === 'outline') {
      body.volume_count = values.volume_count
      body.chapters_per_volume = values.chapters_per_volume
    } else if (kind === 'characters') {
      body.character_count = values.character_count
    } else if (kind === 'careers') {
      body.career_count = values.career_count
    }
    setResult(null)
    setWarnings([])
    setStarting(true)
    let created: Task
    try {
      created = await runWizard(projectId, kind, body)
    } catch (err) {
      message.error(errorMessage(err))
      setStarting(false)
      return
    }
    setStarting(false)
    trackTask(created)
    await followTask(created)
  }

  const renderResult = () => {
    if (!result) return null
    if (kind === 'world') return <WorldPreview items={result as WizardItemMap['world'][]} />
    if (kind === 'characters') return <CharacterPreview items={result as WizardItemMap['characters'][]} />
    if (kind === 'careers') return <CareerPreview items={result as WizardItemMap['careers'][]} />
    return <OutlinePreview items={result as WizardItemMap['outline'][]} />
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      {existingCount > 0 && (
        <Alert
          type="info"
          showIcon
          message={`当前已有 ${existingCount} 条${label}，生成成功后将整体替换为新结果。`}
          description={
            kind === 'careers'
              ? '覆盖会删除全部现有职业，并清除角色已挂的职业、阶段与阶段历史。'
              : kind === 'characters'
                ? '覆盖会删除全部现有角色，并清除其人物关系、组织成员与职业挂接。'
                : undefined
          }
        />
      )}
      <Form
        form={form}
        layout="vertical"
        disabled={running}
        initialValues={{ instruction: '', volume_count: 2, chapters_per_volume: 10, character_count: 5, career_count: 4 }}
      >
        <Form.Item label="AI 配置">
          <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} disabled={running} />
        </Form.Item>
        {kind === 'outline' && (
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item label="卷数" name="volume_count" rules={[{ required: true, message: '请输入卷数' }]}>
                <InputNumber min={1} max={20} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                label="每卷章数"
                name="chapters_per_volume"
                rules={[{ required: true, message: '请输入每卷章数' }]}
              >
                <InputNumber min={1} max={100} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
        )}
        {kind === 'characters' && (
          <Form.Item label="角色数量" name="character_count" rules={[{ required: true, message: '请输入角色数量' }]}>
            <InputNumber min={3} max={30} style={{ width: '100%' }} />
          </Form.Item>
        )}
        {kind === 'careers' && (
          <Form.Item label="职业数量" name="career_count" rules={[{ required: true, message: '请输入职业数量' }]}>
            <InputNumber min={1} max={12} style={{ width: '100%' }} />
          </Form.Item>
        )}
        <Form.Item label="补充指令（可选）" name="instruction" rules={[{ max: 2000, message: '不超过 2000 字' }]}>
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 6 }} placeholder={PLACEHOLDERS[kind]} />
        </Form.Item>
      </Form>
      <Space>
        {running ? (
          <Button danger disabled={!task.running} onClick={() => void handleCancel()}>
            {task.task?.status === 'queued' ? '取消排队' : '停止'}
          </Button>
        ) : (
          <Button type="primary" onClick={() => void handleGenerate()} disabled={hasConfig === false}>
            {result || task.text ? `重新生成${label}` : `AI 生成${label}`}
          </Button>
        )}
      </Space>
      {task.running && task.task?.status === 'queued' && (
        <Alert type="info" showIcon message={queueLabel(task.task)} description="任务在后台排队，可离开本页，稍后返回会自动接上。" />
      )}
      {task.running && task.task?.status === 'running' && (
        <div>
          <Progress percent={task.task.progress} size="small" status="active" style={{ marginBottom: 0 }} />
          {task.task.stage && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {task.task.stage}
            </Typography.Text>
          )}
        </div>
      )}
      {task.reconnecting && <Alert type="warning" showIcon message="连接中断，正在重连…（任务仍在后台运行）" />}
      {task.error && !running && <Alert type="error" showIcon message="生成失败" description={task.error} />}
      {(task.running || task.text) && <StreamOutput text={task.text} running={task.running} />}
      {showResult && warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`有 ${warnings.length} 条提示`}
          description={
            <ul style={{ paddingInlineStart: 20, marginBottom: 0 }}>
              {warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          }
        />
      )}
      {showResult && renderResult()}
    </Space>
  )
}
