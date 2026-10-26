import { Alert, App, Button, Popconfirm, Progress, Space, Spin, Typography } from 'antd'
import { lazy, Suspense, useEffect, useRef, useState, type ReactNode } from 'react'
import { errorMessage } from '../../api/client'
import { queueLabel, TASK_STATUS, taskApi } from '../../api/tasks'
import type { BatchChapterStart, BatchItem, Task } from '../../api/types'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useTasksStore } from '../../stores/tasks'
import { batchBannerState, batchCounts, batchCurrentPhase, batchResumeIndex, batchStageIndex } from '../../utils/batch'

// 清单 / 实时输出只在展开时加载（章节页在入口 chunk 中）
const BatchProgressView = lazy(() => import('./BatchProgressView'))

interface Props {
  task: Task
  /** 有章节开始 / 结束、任务结束或状态变化时调用（刷新章节列表） */
  onChaptersChanged: () => void
  /** 关闭失败 / 中断的横幅（不影响任务） */
  onDismiss: () => void
}

/**
 * 章节页顶部的批量生成横幅：
 * - 排队 / 运行中：进度、当前章、取消；订阅实时输出与章节事件，章节完成时刷新章节列表；
 * - 待审核：已处理 x/y、继续 / 结束；
 * - 失败 / 中断：错误原因与「从第 N 章续跑」（重试，沿用原范围与断点）。
 * 可展开章节清单（BatchProgressView）。卸载只断开订阅，不取消任务。
 */
export default function BatchBanner({ task, onChaptersChanged, onDismiss }: Props) {
  const { message } = App.useApp()
  const track = useTasksStore((s) => s.track)
  const refreshTasks = useTasksStore((s) => s.refresh)
  const stream = useTaskStream()
  const { follow, detach } = stream
  const state = batchBannerState(task)
  const live = state === 'running'
  const [liveItems, setLiveItemsState] = useState<BatchItem[] | null>(null)
  // 同步镜像：chapter_end 时据此判断本地断点是否齐全
  const liveRef = useRef<BatchItem[] | null>(null)
  const setLiveItems = (items: BatchItem[] | null) => {
    liveRef.current = items
    setLiveItemsState(items)
  }
  const [current, setCurrent] = useState<BatchChapterStart | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [busy, setBusy] = useState(false)
  const changedRef = useRef(onChaptersChanged)
  useEffect(() => {
    changedRef.current = onChaptersChanged
  })

  useEffect(() => {
    if (!live) return
    let alive = true
    const reloadCheckpoint = () => {
      taskApi
        .get(task.id)
        .then((t) => {
          if (alive && t.checkpoint) setLiveItems(t.checkpoint.items)
        })
        .catch(() => {
          // 取断点失败：清单沿用轮询数据
        })
    }
    void follow(task, {
      onSnapshot: (t) => {
        if (t?.checkpoint) setLiveItems(t.checkpoint.items)
      },
      onChapterStart: (e) => {
        setCurrent(e)
        changedRef.current()
      },
      onChapterEnd: (e) => {
        const prev = liveRef.current
        if (!prev || e.index >= prev.length) {
          reloadCheckpoint()
        } else {
          const next = [...prev]
          next[e.index] = e.item
          setLiveItems(next)
        }
        setCurrent((c) => (c?.index === e.index ? null : c))
        changedRef.current()
      },
    }).then((outcome) => {
      if (!alive) return
      setLiveItems(null)
      setCurrent(null)
      if (outcome.status === 'detached') return
      if (outcome.status !== 'lost' && outcome.task) track(outcome.task)
      else void refreshTasks()
      changedRef.current()
      if (outcome.status === 'paused') message.info('批量生成已到审核点，检查修改后可点「继续」')
      else if (outcome.status === 'succeeded') message.success('批量生成完成')
    })
    return () => {
      alive = false
      detach()
    }
    // 只按任务 id 与是否运行订阅；task 对象每次轮询都会变化
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id, live, follow, detach])

  const act = async (fn: () => Promise<Task>, done: string) => {
    setBusy(true)
    try {
      track(await fn())
      message.success(done)
      onChaptersChanged()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  if (state === 'none') return null

  const view = live && stream.task?.id === task.id ? stream.task : task
  const items = liveItems ?? view.checkpoint?.items ?? task.checkpoint?.items ?? []
  const counts = batchCounts(items)
  const resumeIndex = state === 'resumable' ? batchResumeIndex(task) : null
  const resumeNo = resumeIndex !== null ? items[resumeIndex]?.chapter_no : null
  const currentIndex = current?.index ?? (view.status === 'running' ? batchStageIndex(view.stage) : null)
  const currentView = currentIndex !== null ? { index: currentIndex, phase: batchCurrentPhase(view.stage) } : null

  let type: 'info' | 'warning' | 'error' = 'info'
  let title: ReactNode
  let actions: ReactNode
  if (state === 'running') {
    title = (
      <Space wrap size={6}>
        <Typography.Text strong>{task.title}</Typography.Text>
        <Typography.Text type="secondary">
          {view.status === 'queued' ? queueLabel(view) : view.stage || TASK_STATUS[view.status].label}
        </Typography.Text>
        {current && (
          <Typography.Text type="secondary">
            当前：第 {current.chapter_no ?? current.index + 1} 章 {current.title}
          </Typography.Text>
        )}
        {stream.reconnecting && <Typography.Text type="warning">连接中断，正在重连…</Typography.Text>}
      </Space>
    )
    actions = (
      <Popconfirm
        title="取消批量生成？"
        description="已完成的章节保留，正在写的章节恢复原状态。"
        okText="取消任务"
        cancelText="不取消"
        onConfirm={() => act(() => taskApi.cancel(task.id), '已取消')}
      >
        <Button size="small" danger loading={busy}>
          取消
        </Button>
      </Popconfirm>
    )
  } else if (state === 'paused') {
    type = 'warning'
    title = (
      <Space wrap size={6}>
        <Typography.Text strong>{task.title} · 待审核</Typography.Text>
        <Typography.Text type="secondary">
          已处理 {counts.processed}/{counts.total} 章。可以先检查、修改已写章节，继续后下一章会用上修改。
        </Typography.Text>
      </Space>
    )
    actions = (
      <Space>
        <Button size="small" type="primary" loading={busy} onClick={() => void act(() => taskApi.resume(task.id), '已继续')}>
          继续
        </Button>
        <Popconfirm
          title="结束批量生成？"
          description="已完成的章节保留，剩余章节不再生成。"
          okText="结束"
          cancelText="不结束"
          onConfirm={() => act(() => taskApi.finish(task.id), '已结束')}
        >
          <Button size="small" disabled={busy}>
            结束
          </Button>
        </Popconfirm>
      </Space>
    )
  } else {
    type = 'error'
    title = (
      <Space wrap size={6}>
        <Typography.Text strong>
          {task.title} · {TASK_STATUS[task.status].label}
        </Typography.Text>
        {task.error && <Typography.Text type="danger">{task.error}</Typography.Text>}
        {counts.total > 0 && (
          <Typography.Text type="secondary">
            已处理 {counts.processed}/{counts.total} 章
          </Typography.Text>
        )}
      </Space>
    )
    actions = (
      <Space>
        <Button size="small" type="primary" loading={busy} onClick={() => void act(() => taskApi.retry(task.id), '已重新提交')}>
          {resumeNo !== null && resumeNo !== undefined ? `从第 ${resumeNo} 章续跑` : '续跑'}
        </Button>
        <Button size="small" type="text" onClick={onDismiss}>
          关闭
        </Button>
      </Space>
    )
  }

  return (
    <Alert
      type={type}
      showIcon
      style={{ marginBottom: 16 }}
      message={title}
      action={actions}
      description={
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          {state === 'running' && (
            <Progress
              percent={view.progress}
              size="small"
              status={view.status === 'running' ? 'active' : 'normal'}
              style={{ marginBottom: 0 }}
            />
          )}
          <Button type="link" size="small" style={{ padding: 0 }} onClick={() => setExpanded((v) => !v)}>
            {expanded ? '收起章节清单' : '展开章节清单'}
          </Button>
          {expanded && (
            <Suspense fallback={<Spin size="small" />}>
              <BatchProgressView
                items={items}
                current={live ? currentView : null}
                text={live ? stream.text : undefined}
                running={live && stream.running}
              />
            </Suspense>
          )}
        </Space>
      }
    />
  )
}
