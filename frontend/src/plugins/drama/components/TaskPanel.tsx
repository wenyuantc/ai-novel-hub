import { Alert, Button, Card, Popconfirm, Progress, Space, Typography } from 'antd'
import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { errorMessage } from '../../../api/client'
import type { Task } from '../../../api/types'
import { useAIModelSource } from '../../../hooks/useAIModelSource'
import type { AIModelSource } from '../../../utils/projectAccess'
import { useHasAIConfig } from '../../../hooks/useHasAIConfig'
import { useAIConfigStore } from '../../../stores/aiConfigs'
import { useTaskStream } from '../../../hooks/useTaskStream'
import { dramaApi, type DramaTaskKind } from '../api'
import type { EpisodeModelBinding } from '../useEpisodeModelSettings'
import type { Access } from './PageScope'

export type RunDramaTask = (kind: Exclude<DramaTaskKind, 'drama.image'>, params?: Record<string, unknown>) => void

export interface LookImageAction {
  model: AIModelSource
  pendingLookId: number | null
  generate?: (lookId: number) => void
}

export default function TaskPanel({
  sid,
  eid,
  access,
  refresh,
  modelBinding,
  children,
}: {
  sid: number
  eid?: number
  access: Access
  refresh: () => void
  modelBinding?: EpisodeModelBinding
  children: (run?: RunDramaTask, image?: LookImageAction) => ReactNode
}) {
  const config = useHasAIConfig()
  const imageModel = useAIModelSource('image')
  const imageAvailable = imageModel === 'own' || imageModel === 'owner'
  const [pendingLookId, setPendingLookId] = useState<number | null>(null)
  const loadConfigs = useAIConfigStore((store) => store.load)
  const stream = useTaskStream()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [imageSaved, setImageSaved] = useState(false)
  const [draft, setDraft] = useState<{ task: Task; value: unknown } | null>(null)
  useEffect(() => {
    if (!access.canUseAI) return
    let current = true
    void loadConfigs().catch((e) => {
      if (current) setError(errorMessage(e))
    })
    return () => {
      current = false
    }
  }, [access.canUseAI, loadConfigs])
  const start = async (kind: DramaTaskKind, params: Record<string, unknown> = {}, lookId?: number) => {
    const isImage = kind === 'drama.image'
    if (pending || !(isImage ? imageAvailable : config) || !access.canUseAINow() || !access.canEditNow()) return
    const configId = modelBinding?.configFor(isImage ? 'image' : 'text')
    if (modelBinding && configId === undefined) return
    const taskParams = modelBinding ? { ...params, ai_config_id: configId ?? undefined } : params
    setPending(true)
    setPendingLookId(lookId ?? null)
    setImageSaved(false)
    setError('')
    stream.reset()
    try {
      const task = await dramaApi.task(sid, eid, kind, taskParams)
      if (!access.canUseAINow()) return
      if (isImage) refresh()
      const end = await stream.follow(task)
      if (!access.canUseAINow()) return
      if (isImage) {
        setImageSaved(end.status === 'succeeded' && !end.unpublished)
        refresh()
        return
      }
      if (end.status !== 'succeeded') return
      if (end.result && typeof end.result === 'object' && 'applied' in end.result && end.result.applied === false) {
        setDraft({ task, value: 'draft' in end.result ? end.result.draft : end.result })
      }
      refresh()
    } catch (e) {
      if (access.canUseAINow()) {
        setError(errorMessage(e))
        if (configId != null) modelBinding?.reload?.()
      }
    } finally {
      setPendingLookId(null)
      setPending(false)
    }
  }
  const apply = async () => {
    if (!draft || pending || !access.canEditNow() || !access.canUseAINow()) return
    setPending(true)
    try {
      await dramaApi.apply(sid, eid, draft.task)
      if (access.canUseAINow()) {
        setDraft(null)
        refresh()
      }
    } catch (e) {
      if (access.canUseAINow()) setError(errorMessage(e))
    } finally {
      setPending(false)
    }
  }
  const allowed = access.canEdit && access.canUseAI
  const available = allowed && !pending && !draft && (!modelBinding || modelBinding.ready)
  const imageAction: LookImageAction | undefined = allowed
    ? {
        model: imageModel,
        pendingLookId,
        generate:
          available && imageAvailable && !modelBinding?.unavailable?.includes('image')
            ? (id) => start('drama.image', { targets: [{ type: 'look', id }] }, id)
            : undefined,
      }
    : undefined
  return (
    <>
      {!modelBinding && access.canUseAI && config === false && (
        <Alert showIcon type="info" message="改写剧本、提取资产和生成提示词需要文本模型" action={<Link to="/settings/ai">配置模型</Link>} />
      )}
      {children(available && config && !modelBinding?.unavailable?.includes('text') ? start : undefined, imageAction)}
      {imageSaved && <Alert type="success" showIcon message="造型图已保存，可在图片历史中查看和选用。" closable onClose={() => setImageSaved(false)} />}
      {(error || stream.error) && <Alert type="error" showIcon message={error || stream.error} />}
      {stream.running && access.canUseAI && (
        <Card
          size="small"
          title={stream.task?.title ?? 'AI 任务'}
          extra={<Button onClick={() => void stream.cancel().catch((e) => setError(errorMessage(e)))}>取消任务</Button>}
        >
          <div role="status">{stream.reconnecting ? '正在重新连接…' : stream.task?.stage}</div>
          <Progress percent={stream.task?.progress ?? 0} />
          {stream.text && <pre className="drama-output">{stream.text}</pre>}
        </Card>
      )}
      {draft && access.canUseAI && (
        <Alert
          type="warning"
          message="内容已被修改，AI 结果已保留为草稿"
          description={
            <>
              <details>
                <summary>查看 AI 草稿</summary>
                <Typography.Paragraph>
                  <pre className="drama-output">
                    {typeof draft.value === 'string' ? draft.value : JSON.stringify(draft.value, null, 2)}
                  </pre>
                </Typography.Paragraph>
              </details>
              <Space>
                <Button onClick={() => setDraft(null)}>保留当前内容</Button>
                <Popconfirm title="采用 AI 草稿并更新当前内容？" onConfirm={apply}>
                  <Button disabled={pending}>采用 AI 结果</Button>
                </Popconfirm>
              </Space>
            </>
          }
        />
      )}
    </>
  )
}
