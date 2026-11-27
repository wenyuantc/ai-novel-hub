import { Alert, Button, Modal, Progress, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useEffect, useId, useState } from 'react'
import { isActiveTask, queueLabel, taskApi, TASK_STATUS } from '../../api/tasks'
import type { AIConfig, Task } from '../../api/types'
import { videoProbeApi } from '../../api/videoProbe'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useTasksStore } from '../../stores/tasks'
import { useAiUsageMutation, useAiUsageRequest, type UsageReadOptions, type UsageSession } from '../aiUsage/useAiUsageRequest'
import MediaVideo from './MediaVideo'
import { watchVideoProbe } from './watchVideoProbe'

interface Props { config: AIConfig; session: UsageSession; onClose: () => void }

function ProbeForm({ config, session, initial, reload }: Omit<Props, 'onClose'> & { initial: Task | null; reload: () => void }) {
  const inputId = useId()
  const [file, setFile] = useState<File>()
  const [selected, setSelected] = useState(initial)
  const [unavailable, setUnavailable] = useState(false)
  const { task: streamed, error, reconnecting, follow, detach } = useTaskStream()
  const track = useTasksStore((s) => s.track)
  const mutation = useAiUsageMutation(session)
  const current = selected && isActiveTask(selected.status) && streamed?.task_key === selected.task_key ? streamed : selected
  const active = !!current && isActiveTask(current.status)
  const needsConfirmation = active || (current?.status === 'succeeded' && !current.video_object_id)
  const invalidFile = file && (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)
    || file.size > 10 * 1024 * 1024 || file.size === 0)

  useEffect(() => {
    if (!selected || !isActiveTask(selected.status)) return
    void follow(selected)
    return detach
  }, [selected, follow, detach])

  useEffect(() => {
    if (!selected || !isActiveTask(selected.status) || !needsConfirmation) return
    return watchVideoProbe(selected, document, (latest) => {
      if (session.current()) setSelected(latest)
    }, () => {
      if (session.current()) setUnavailable(true)
    })
  }, [selected, needsConfirmation, session])

  if (unavailable) return <Alert type="warning" showIcon message="测试任务已失效，请重新读取。"
    action={<Button onClick={reload}>刷新状态</Button>} />

  const start = () => void mutation.commit(async (scope) => {
    const image = file ? await videoProbeApi.uploadReference(config.id, file, scope) : undefined
    if (!scope.isCurrent()) throw new DOMException('已停止', 'AbortError')
    return videoProbeApi.start(config.id, image ? [image.object_id] : [], scope)
  }, (created) => {
    track(created)
    setFile(undefined)
    setSelected(created)
  }, '视频测试已受理', reload)

  const stop = () => {
    if (!current) return
    void mutation.commit(() => taskApi.cancel(current), (updated) => {
      track(updated)
      setSelected(updated)
    }, '取消请求已受理', reload)
  }

  return <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    <Typography.Text>将用「{config.name}」生成一段最短时长的视频，计入个人 AI 用量与费用。测试文件保留 24 小时。</Typography.Text>
    {current && <section aria-label="最近一次视频测试">
      <Space wrap role="status">
        <Tag color={TASK_STATUS[current.status].color}>{TASK_STATUS[current.status].label}</Tag>
        {current.status === 'queued' ? queueLabel(current) : current.stage}
        {reconnecting && '正在重新连接'}
      </Space>
      {active && current.progress > 0 && <Progress percent={current.progress} size="small" />}
      {(error || current.error) && <Alert type="error" showIcon message={error || current.error}
        action={error && <Button size="small" onClick={reload}>刷新状态</Button>} />}
      {current.status === 'succeeded' && (current.video_object_id
        ? <MediaVideo objectId={current.video_object_id} label={config.name + ' 测试视频'} />
        : <Typography.Paragraph type="secondary">测试视频暂不可用。</Typography.Paragraph>)}
    </section>}
    {active
      ? <Button danger loading={mutation.loading} onClick={stop}>取消生成</Button>
      : <>
        <label htmlFor={inputId}>参考图（可选，JPEG、PNG 或 WebP，最多 10 MiB）</label>
        <input id={inputId} type="file" accept="image/jpeg,image/png,image/webp" disabled={mutation.loading}
          onChange={(event) => setFile(event.target.files?.[0])} />
        {invalidFile && <Alert type="warning" showIcon message="请选择有效的参考图，文件大小须在 10 MiB 以内。" />}
        <Button type="primary" loading={mutation.loading} disabled={!!invalidFile} onClick={start}>开始生成</Button>
      </>}
  </Space>
}

export default function VideoProbeModal({ config, session, onClose }: Props) {
  const width = useModalWidth(540)
  const load = useCallback((scope: UsageReadOptions) => videoProbeApi.latest(config.id, scope), [config.id])
  const latest = useAiUsageRequest(load, session)
  return <Modal open title="视频测试生成" width={width} onCancel={onClose} footer={null} destroyOnHidden>
    {latest.loading ? <Spin aria-label="正在读取测试任务" /> : latest.error
      ? <Alert type="error" showIcon message={latest.error} action={<Button onClick={latest.refresh}>重试</Button>} />
      : <ProbeForm config={config} session={session} initial={latest.data ?? null} reload={latest.refresh} />}
  </Modal>
}
