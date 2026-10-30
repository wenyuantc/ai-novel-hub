import { Alert, App, Button, Form, Input, InputNumber, Modal, Progress, Select, Space, Spin, Typography } from 'antd'
import { Suspense, useEffect, useRef, useState } from 'react'
import { CAREER_KIND_LABELS, careerApi } from '../../api/careers'
import { characterExtraApi, ROLE_TYPES } from '../../api/characters'
import { errorMessage } from '../../api/client'
import { missingRelationPairCount } from '../../api/entityTasks'
import { organizationApi } from '../../api/organizations'
import { relationshipApi } from '../../api/relationships'
import { queueLabel, TASK_KIND_LABELS, taskApi } from '../../api/tasks'
import type { CareerKind, Character, Relationship, Task } from '../../api/types'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useTasksStore } from '../../stores/tasks'
import AIConfigSelect from '../AIConfigSelect'
import StreamOutput from '../StreamOutput'
import { TASK_RESULT_VIEWERS } from './taskResultViewers'

export type EntityTaskKind = 'character.generate' | 'organization.generate' | 'relations.parse' | 'relations.generate' | 'career.generate'

interface Props {
  projectId: number
  kind: EntityTaskKind
  open: boolean
  onClose: () => void
  /** relations.parse 的主体角色 */
  characterId?: number
  characterName?: string
  /** relations.generate 的可选角色与已有关系 */
  characters?: Character[]
  relationships?: Relationship[]
  /** 保存成功（写入了数据）后回调 */
  onSaved?: () => void
}

interface FormValues {
  instruction: string
  role_type?: string
  /** career.generate：1–5；relations.generate：1–50 */
  count?: number
  character_ids?: number[]
  career_kind?: CareerKind
}

const PLACEHOLDERS: Record<Exclude<EntityTaskKind, 'relations.parse'>, string> = {
  'character.generate': '例如：主角的师父，外冷内热，与反派有旧怨',
  'organization.generate': '例如：主角所在城市的地下情报组织，与官府若即若离',
  'career.generate': '例如：与炼丹相关的副修职业，阶段用「品」划分',
  'relations.generate': '例如：围绕主角的成长，建立亦师亦友的关系，保留门派间的隔阂',
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

function Inner({ projectId, kind, onClose, characterId, characterName, characters = [], relationships = [], onSaved }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  const [starting, setStarting] = useState(false)
  const startingRef = useRef(false)
  const [recovering, setRecovering] = useState(true)
  const [result, setResult] = useState<unknown>(undefined)
  const stream = useTaskStream()
  const trackTask = useTasksStore((s) => s.track)
  const hasConfig = useHasAIConfig()
  const running = stream.running || starting
  const busy = running || recovering
  const selectedIds: number[] = Form.useWatch('character_ids', form) ?? []
  const missingPairs = missingRelationPairCount(selectedIds, relationships)
  const noMissingPairs = kind === 'relations.generate' && selectedIds.length >= 2 && missingPairs === 0
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
      .finally(() => { if (alive) setRecovering(false) })
    return () => {
      alive = false
    }
  }, [projectId, kind, characterId])

  const handleStart = async () => {
    if (startingRef.current || stream.running || recovering || noMissingPairs) return
    startingRef.current = true
    let v: FormValues
    try {
      v = await form.validateFields()
    } catch {
      startingRef.current = false
      return
    }
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
      } else if (kind === 'relations.generate') {
        created = await relationshipApi.generate(projectId, {
          character_ids: v.character_ids ?? [],
          count: v.count ?? 10,
          instruction,
          ai_config_id: aiConfigId,
        })
      } else {
        created = await characterExtraApi.parseRelations(projectId, characterId as number, { ai_config_id: aiConfigId })
      }
    } catch (err) {
      message.error(errorMessage(err))
      setStarting(false)
      startingRef.current = false
      return
    }
    setStarting(false)
    startingRef.current = false
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
            <Form form={form} layout="vertical" disabled={busy} initialValues={{ instruction: '', role_type: undefined, count: kind === 'relations.generate' ? 10 : 3, career_kind: undefined, character_ids: [] }}>
              <Form.Item label="AI 配置">
                <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} disabled={busy} />
              </Form.Item>
              {kind === 'character.generate' && (
                <Form.Item name="role_type" label="期望定位（可选）">
                  <Select allowClear options={ROLE_TYPES.map((r) => ({ value: r, label: r }))} style={{ width: 200 }} />
                </Form.Item>
              )}
              {kind === 'relations.generate' && (
                <>
                  <Form.Item name="character_ids" label="选择角色（2–20 名）" rules={[{ required: true, type: 'array', min: 2, max: 20, message: '请选择 2–20 名角色' }]}>
                    <Select
                      mode="multiple"
                      showSearch
                      allowClear
                      maxCount={20}
                      maxTagCount="responsive"
                      optionFilterProp="label"
                      placeholder="按名字、定位或别名查找角色"
                      options={characters.map((c) => ({
                        value: c.id,
                        label: `${c.name} · ${c.role_type || '未设定'}${c.aliases.length ? ` · ${c.aliases.join('、')}` : ''}（ID=${c.id}）`,
                      }))}
                    />
                  </Form.Item>
                  <Form.Item name="count" label="期望条数" rules={[{ required: true, type: 'number', min: 1, max: 50, message: '请输入 1–50 的整数' }]}>
                    <InputNumber min={1} max={50} precision={0} style={{ width: 120 }} />
                  </Form.Item>
                  <Typography.Paragraph type="secondary">
                    只补充所选角色之间的缺失关系，生成后可预览、编辑和勾选，再确认保存。
                    {selectedIds.length >= 2 && `目前可补充 ${missingPairs} 个角色对；实际建议条数不超过缺失对数。`}
                  </Typography.Paragraph>
                  {noMissingPairs && <Alert type="info" showIcon message="所选角色之间已无缺失关系，无需生成" style={{ marginBottom: 16 }} />}
                </>
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
                <Button type="primary" loading={recovering} onClick={() => void handleStart()} disabled={hasConfig === false || recovering || noMissingPairs}>
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
