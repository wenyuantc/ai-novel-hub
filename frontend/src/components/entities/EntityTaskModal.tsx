import { Alert, App, Button, Form, Input, InputNumber, Modal, Progress, Select, Space, Spin, Typography } from 'antd'
import { Suspense, useEffect, useRef, useState } from 'react'
import { CAREER_KIND_LABELS, careerApi } from '../../api/careers'
import { characterExtraApi, ROLE_TYPES } from '../../api/characters'
import { errorMessage } from '../../api/client'
import { organizationApi } from '../../api/organizations'
import { queueLabel, TASK_KIND_LABELS, taskApi } from '../../api/tasks'
import type { CareerKind, Task } from '../../api/types'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useTasksStore } from '../../stores/tasks'
import AIConfigSelect from '../AIConfigSelect'
import StreamOutput from '../StreamOutput'
import { TASK_RESULT_VIEWERS } from './taskResultViewers'

export type EntityTaskKind = 'character.generate' | 'organization.generate' | 'relations.parse' | 'career.generate'

interface Props {
  projectId: number
  kind: EntityTaskKind
  open: boolean
  onClose: () => void
  /** relations.parse 的主体角色 */
  characterId?: number
  characterName?: string
  /** 保存成功（写入了数据）后回调 */
  onSaved?: () => void
}

interface FormValues {
  instruction: string
  role_type?: string
  /** career.generate：1–5 */
  count?: number
  career_kind?: CareerKind
}

const PLACEHOLDERS: Record<Exclude<EntityTaskKind, 'relations.parse'>, string> = {
  'character.generate': '例如：主角的师父，外冷内热，与反派有旧怨',
  'organization.generate': '例如：主角所在城市的地下情报组织，与官府若即若离',
  'career.generate': '例如：与炼丹相关的副修职业，阶段用「品」划分',
}

/**
 * 设定类 AI 任务弹窗：填写参数 → 创建后台任务并订阅 → 完成后展示可编辑预览（注册表中的结果组件）→ 确认保存。
 * 运行中可以关闭：任务在后台继续，完成后可在任务面板「查看结果」打开同一预览。
 * 打开时自动接上活动任务：生成类按 project + kind，解析任务再按 character_id 区分角色。
 */
export default function EntityTaskModal(props: Props) {
  if (!props.open) return null
  return <Inner {...props} />
}

function Inner({ projectId, kind, onClose, characterId, characterName, onSaved }: Props) {
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
  const label = TASK_KIND_LABELS[kind]

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

  // 打开时接上活动任务：生成类取本项目同类最新一个；解析任务按 TaskOut.character_id 匹配本角色
  useEffect(() => {
    if (kind === 'relations.parse' && characterId === undefined) return
    let alive = true
    taskApi
      .list({ project_id: projectId, kind, active: true, limit: kind === 'relations.parse' ? 50 : 1 })
      .then((list) => {
        const found = kind === 'relations.parse' ? list.find((t) => t.character_id === characterId) : list[0]
        if (alive && found) void followRef.current(found)
      })
      .catch(() => {
        // 查询失败按无活动任务处理
      })
    return () => {
      alive = false
    }
  }, [projectId, kind, characterId])

  const handleStart = async () => {
    const v = await form.validateFields()
    setStarting(true)
    let created: Task
    try {
      const instruction = v.instruction?.trim() ?? ''
      if (kind === 'character.generate') {
        created = await characterExtraApi.generate(projectId, {
          ai_config_id: aiConfigId,
          instruction,
          role_type: v.role_type?.trim() ?? '',
        })
      } else if (kind === 'organization.generate') {
        created = await organizationApi.generate(projectId, { ai_config_id: aiConfigId, instruction })
      } else if (kind === 'career.generate') {
        created = await careerApi.generate(projectId, {
          ai_config_id: aiConfigId,
          instruction,
          count: v.count ?? 3,
          kind: v.career_kind ?? null,
        })
      } else {
        created = await characterExtraApi.parseRelations(projectId, characterId as number, { ai_config_id: aiConfigId })
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

  const title = kind === 'relations.parse' ? `AI 解析旧关系 · ${characterName ?? ''}` : label
  const showForm = result === undefined

  return (
    <Modal open title={title} footer={null} width={900} maskClosable={false} onCancel={handleClose} destroyOnHidden>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {showForm && (
          <>
            <Form form={form} layout="vertical" disabled={running} initialValues={{ instruction: '', role_type: undefined, count: 3, career_kind: undefined }}>
              <Form.Item label="AI 配置">
                <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} disabled={running} />
              </Form.Item>
              {kind === 'character.generate' && (
                <Form.Item name="role_type" label="期望定位（可选）">
                  <Select allowClear options={ROLE_TYPES.map((r) => ({ value: r, label: r }))} style={{ width: 200 }} />
                </Form.Item>
              )}
              {kind === 'career.generate' && (
                <Space size="large" wrap>
                  <Form.Item name="count" label="数量" rules={[{ required: true, message: '请输入数量' }]}>
                    <InputNumber min={1} max={5} style={{ width: 120 }} />
                  </Form.Item>
                  <Form.Item name="career_kind" label="类别（可选）">
                    <Select
                      allowClear
                      placeholder="由 AI 决定"
                      style={{ width: 160 }}
                      options={(['main', 'sub'] as CareerKind[]).map((k) => ({ value: k, label: CAREER_KIND_LABELS[k] }))}
                    />
                  </Form.Item>
                </Space>
              )}
              {kind !== 'relations.parse' ? (
                <Form.Item label="要求（可选）" name="instruction" rules={[{ max: 2000, message: '不超过 2000 字' }]}>
                  <Input.TextArea
                    autoSize={{ minRows: 2, maxRows: 6 }}
                    placeholder={PLACEHOLDERS[kind]}
                  />
                </Form.Item>
              ) : (
                <Typography.Paragraph type="secondary">
                  AI 会把该角色的旧关系备注拆成结构化关系，预览确认后才写入；人名对不上已有角色的条目会被标出，不能写入。
                </Typography.Paragraph>
              )}
            </Form>
            <Space>
              {running ? (
                <Button danger disabled={!stream.running} onClick={() => void handleCancel()}>
                  {stream.task?.status === 'queued' ? '取消排队' : '停止'}
                </Button>
              ) : (
                <Button type="primary" onClick={() => void handleStart()} disabled={hasConfig === false}>
                  {stream.text ? '重新开始' : '开始'}
                </Button>
              )}
            </Space>
          </>
        )}
        {stream.running && stream.task?.status === 'queued' && <Alert type="info" showIcon message={queueLabel(stream.task)} />}
        {stream.running && stream.task?.status === 'running' && (
          <Progress percent={stream.task.progress} size="small" status="active" />
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
