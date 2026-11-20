import { ClearOutlined, ReloadOutlined, UnorderedListOutlined } from '@ant-design/icons'
import { App, Button, Drawer, Empty, FloatButton, List, Modal, Popconfirm, Progress, Space, Spin, Tabs, Tag, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useNavigate } from 'react-router-dom'
import { currentRequestGeneration, errorMessage, watchRequestGeneration } from '../api/client'
import { canRetryTask, canStreamTask, hasTaskIdentity, isActiveTask, queueLabel, TASK_STATUS, taskApi, taskLink, taskReviewIdentity } from '../api/tasks'
import type { Task } from '../api/types'
import { useLoader } from '../hooks/useLoader'
import { useTaskStream } from '../hooks/useTaskStream'
import { useTasksStore } from '../stores/tasks'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../stores/project'
import { useAuthStore } from '../stores/auth'
import { authorizationKey } from '../utils/permissions'
import {
  projectAccess, taskRetryBlockedHint, taskControlsAllowed, taskOutputMode,
} from '../utils/projectAccess'
import { bookImportBatches, bookImportPause, bookImportPauseText, canSkipBookImport } from '../utils/bookImportState'
import { formatTime } from '../utils/time'
import { hasResultViewer } from './entities/taskResultViewers'
import TaskResultModal from './entities/TaskResultModal'
import StreamOutput from './StreamOutput'
import RichReviewModal from './scenes/RichReviewModal'
import TaskActionConfirm from './TaskActionConfirm'
import { AGENT_DOCK_WIDTH_VAR } from '../utils/agentPreferences'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

/** 右下角后台任务面板：活动任务数徽标 + 抽屉（进行中 / 最近），支持取消、重试、查看输出、跳转、删除、清理。 */
export default function TaskPanel({ open: controlledOpen, onOpenChange, hideTrigger = false }: { open?: boolean; onOpenChange?: (open: boolean) => void; hideTrigger?: boolean }) {
  const { message } = App.useApp()
  const active = useTasksStore((s) => s.active)
  const version = useTasksStore((s) => s.version)
  const startPolling = useTasksStore((s) => s.startPolling)
  const track = useTasksStore((s) => s.track)
  const bump = useTasksStore((s) => s.bump)
  const refresh = useTasksStore((s) => s.refresh)
  const [internalOpen, setInternalOpen] = useState(false)
  const open = controlledOpen ?? internalOpen
  const setOpen = onOpenChange ?? setInternalOpen
  const [tab, setTab] = useState('active')
  const [reviewing, setReviewing] = useState<Task | null>(null)
  const [viewing, setViewing] = useState<Task | null>(null)
  const [resultOf, setResultOf] = useState<Task | null>(null)
  const [clearing, setClearing] = useState(false)

  useEffect(() => startPolling(), [startPolling])

  const recentLoader = useCallback(
    // version 变化（有任务结束 / 本地操作）时重新加载
    () => (open && version >= 0 ? taskApi.list({ active: false, limit: 50 }) : Promise.resolve<Task[]>([])),
    [open, version],
  )
  const { data: recent = [], loading: recentLoading, reload: reloadRecent } = useLoader(recentLoader)

  const handleClear = async () => {
    setClearing(true)
    try {
      const r = await taskApi.clearFinished()
      message.success(`已清理 ${r.deleted} 个已结束任务`)
      bump()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setClearing(false)
    }
  }

  return (
    <>
      {!hideTrigger && <FloatButton
        icon={<UnorderedListOutlined />}
        tooltip="后台任务"
        badge={{ count: active.length, color: 'blue' }}
        onClick={() => setOpen(true)}
        style={{ insetInlineEnd: `calc(24px + var(${AGENT_DOCK_WIDTH_VAR}, 0px))` }}
      />}
      <Drawer
        title="后台任务"
        open={open}
        onClose={() => setOpen(false)}
        width={520}
        extra={
          <Space>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => {
                void refresh()
                reloadRecent()
              }}
            >
              刷新
            </Button>
            <Popconfirm title="清理全部已结束的任务？" okText="清理" cancelText="取消" onConfirm={() => handleClear()}>
              <Button size="small" icon={<ClearOutlined />} loading={clearing}>
                清理已结束
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: 'active',
              label: `进行中（${active.length}）`,
              children: (
                <TaskList tasks={active} onView={setViewing} onViewResult={setResultOf} onReview={setReviewing} onTrack={track} onRemoved={bump} onNavigate={() => setOpen(false)} />
              ),
            },
            {
              key: 'recent',
              label: '最近',
              children: (
                <Spin spinning={recentLoading}>
                  <TaskList tasks={recent} onView={setViewing} onViewResult={setResultOf} onReview={setReviewing} onTrack={track} onRemoved={bump} onNavigate={() => setOpen(false)} />
                </Spin>
              ),
            },
          ]}
        />
      </Drawer>
      {viewing && <TaskOutputModal key={`${viewing.id}:${viewing.task_key}`} task={viewing} onClose={() => setViewing(null)} />}
      {resultOf && <TaskResultModal key={`${resultOf.id}:${resultOf.task_key}`} task={resultOf} onClose={() => setResultOf(null)} />}
      {reviewing && <RichReviewModal key={`${reviewing.id}:${reviewing.task_key}:${reviewing.rich_review?.run_key}`} task={reviewing} onClose={() => setReviewing(null)} onChanged={(updated) => { track(updated); bump() }} />}
    </>
  )
}

interface ListProps {
  tasks: Task[]
  onView: (t: Task) => void
  /** 已成功、且 kind 注册了结果查看组件的任务：打开可编辑预览 */
  onViewResult: (t: Task) => void
  onReview: (t: Task) => void
  onTrack: (t: Task) => void
  onRemoved: () => void
  onNavigate: () => void
}

function TaskList({ tasks, onView, onViewResult, onReview, onTrack, onRemoved, onNavigate }: ListProps) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [pending, setPending] = useState<string | null>(null)
  const project = useProjectStore((state) => state.project)
  const user = useAuthStore((state) => state.user)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const actor = () => {
    const auth = useAuthStore.getState()
    return `${auth.status}:${auth.user?.id ?? ''}:${authorizationKey(auth.user)}:${currentRequestGeneration()}`
  }

  const act = async (id: string, fn: (current: () => boolean) => Promise<void>) => {
    const identity = actor()
    const current = () => mounted.current && actor() === identity
    setPending(id)
    try {
      await fn(current)
    } catch (err) {
      if (current()) message.error(errorMessage(err))
    } finally {
      if (current()) setPending(null)
    }
  }

  if (tasks.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无任务" />

  return (
    <List
      dataSource={tasks}
      renderItem={(t) => {
        // 拆书反推因 AI 失败暂停不是「待审核」：状态与阶段都显示为「已暂停」（失败原因在下方单独一行）
        const failedPause = canSkipBookImport(t)
        const st = failedPause ? { label: '已暂停', color: 'gold' } : TASK_STATUS[t.status]
        const stage = failedPause ? '已暂停' : t.stage
        const activeTask = isActiveTask(t.status)
        const paused = t.status === 'paused'
        const identityReady = hasTaskIdentity(t)
        const taskKey = `${t.id}:${t.task_key ?? ''}`
        const link = taskLink(t)
        // 当前作品内已降为查看者等无 AI 授权时，继续 / 结束 / 取消 / 重试必然被后端拒绝，不显示
        const controls = taskControlsAllowed(t.project_id, project, user)
        // 服务端判定不可直接重试（如记忆条目绑定的向量任务）：不显示重试，改为补救说明（跨作品同样生效）
        const retryBlockedHint = canRetryTask(t.status) ? taskRetryBlockedHint(t) : null
        const actions = [
          <Button key="view" type="link" size="small" disabled={!identityReady} onClick={() => onView(t)}>
            查看输出
          </Button>,
        ]
        if (paused && t.rich_review?.phase === 'awaiting_review') {
          actions.unshift(<Button key="rich-review" type="link" size="small" disabled={!identityReady || t.rich_review.task_key !== t.task_key} onClick={() => onReview(t)}>核对候选</Button>)
        }
        if ((t.status === 'succeeded' || paused) && hasResultViewer(t.kind)) {
          actions.unshift(
            <Button key="result" type="link" size="small" disabled={!identityReady} onClick={() => onViewResult(t)}>
              查看结果
            </Button>,
          )
        }
        if (link && (t.kind !== 'scene.restructure' || controls && (activeTask || t.status === 'succeeded'))) {
          actions.push(
            <Button
              key="go"
              type="link"
              size="small"
              onClick={() => {
                onNavigate()
                navigate(link)
              }}
            >
              {t.kind === 'scene.restructure' ? (activeTask ? '查看进度' : '查看建议') : '跳转'}
            </Button>,
          )
        }
        if (paused && controls) {
          // 待审核：继续（同一任务从断点接着跑）/ 结束（以已完成收尾）/ 取消；不能删除、重试。
          // 拆书反推因 AI 失败暂停时另有「跳过本批」（通读批次不写摘要 / 汇总改用代码合并结果）
          if (canSkipBookImport(t)) {
            actions.push(
              <TaskActionConfirm
                key="skip"
                task={t}
                title="跳过当前步骤？"
                description="通读批次被跳过时这些章节不写摘要；汇总被跳过时直接使用各批结果的代码合并。"
                okText="跳过"
                cancelText="不跳过"
                onConfirm={(selected) =>
                  act(taskKey, async (current) => {
                    const next = await taskApi.skipBookImport(selected)
                    if (!current()) return
                    onTrack(next)
                    message.success('已跳过，任务继续')
                  })
                }
              >
                <Button type="link" size="small" disabled={!identityReady}>
                  跳过本批
                </Button>
              </TaskActionConfirm>,
            )
          }
          actions.push(
            ...(t.kind === 'agent.turn' ? [] : [<Button
              key="resume"
              type="link"
              size="small"
              disabled={!identityReady || t.rich_review?.phase === 'awaiting_review'}
              loading={pending === taskKey}
              onClick={() =>
                void act(taskKey, async (current) => {
                  const next = await taskApi.resume(t)
                  if (!current()) return
                  onTrack(next)
                  message.success('已继续')
                })
              }
            >
              继续
            </Button>,
            <TaskActionConfirm
              key="finish"
              task={t}
              title="结束该任务？"
              description={t.rich_review?.phase === 'awaiting_review' ? '当前候选将被放弃、原章保持不变；已完成章节保留。' : '已完成的内容保留，剩余部分不再处理。'}
              okText="结束"
              cancelText="不结束"
              onConfirm={(selected) =>
                act(taskKey, async (current) => {
                  const next = await taskApi.finish(selected, taskReviewIdentity(selected))
                  if (!current()) return
                  onTrack(next)
                  message.success('已结束')
                })
              }
            >
              <Button type="link" size="small" disabled={!identityReady}>
                结束
              </Button>
            </TaskActionConfirm>]),
            <TaskActionConfirm
              key="cancel"
              task={t}
              title="取消该任务？"
              description="已完成的内容保留，任务记为已取消。"
              okText="取消任务"
              cancelText="不取消"
              onConfirm={(selected) =>
                act(taskKey, async (current) => {
                  const next = await taskApi.cancel(selected, taskReviewIdentity(selected))
                  if (!current()) return
                  onTrack(next)
                  message.success('已取消')
                })
              }
            >
              <Button type="link" size="small" danger disabled={!identityReady}>
                取消
              </Button>
            </TaskActionConfirm>,
          )
        } else if (activeTask) {
          if (controls) actions.push(
            <TaskActionConfirm
              key="cancel"
              task={t}
              title="取消该任务？"
              description={
                t.kind === 'chapter.batch'
                  ? '已完成的章节保留，正在写的章节恢复原状态。'
                  : t.kind === 'book.import'
                    ? '已导入的项目、章节和已写入的设定都会保留。'
                    : t.kind === 'agent.turn'
                      ? '已执行的修改保留，可在会话中撤销。'
                    : t.kind === 'consistency.check'
                      ? '已完成章节的事实台账保留，重试时从未完成的章节继续。'
                    : t.kind === 'consistency.impact'
                      ? '尚未保存的评估结果会丢弃；已重新抽取的本章事实台账保留，正文与设定不受影响。'
                      : '已输出的内容不会保存，章节保持原样。'
              }
              okText="取消任务"
              cancelText="不取消"
              onConfirm={(selected) =>
                act(taskKey, async (current) => {
                  const next = await taskApi.cancel(selected, taskReviewIdentity(selected))
                  if (!current()) return
                  onTrack(next)
                  message.success('已取消')
                })
              }
            >
              <Button type="link" size="small" danger disabled={!identityReady} loading={pending === taskKey}>
                取消
              </Button>
            </TaskActionConfirm>,
          )
        } else if (!paused) {
          if (controls && canRetryTask(t.status) && !retryBlockedHint) {
            actions.push(
              <Button
                key="retry"
                type="link"
                size="small"
                loading={pending === taskKey}
                disabled={!identityReady}
                onClick={() =>
                  void act(taskKey, async (current) => {
                    const next = await taskApi.retry(t)
                    if (!current()) return
                    onTrack(next)
                    message.success('已重新提交')
                  })
                }
              >
                重试
              </Button>,
            )
          }
          actions.push(
            <TaskActionConfirm
              key="del"
              task={t}
              title="删除该任务记录？"
              okText="删除"
              cancelText="取消"
              onConfirm={(selected) =>
                act(taskKey, async (current) => {
                  await taskApi.remove(selected, taskReviewIdentity(selected, true))
                  if (!current()) return
                  onRemoved()
                })
              }
            >
              <Button type="link" size="small" danger disabled={!identityReady}>
                删除
              </Button>
            </TaskActionConfirm>,
          )
        }
        return (
          <List.Item key={taskKey} actions={actions} style={{ flexWrap: 'wrap' }}>
            <div style={{ width: '100%' }}>
              <Space wrap size={4}>
                <Typography.Text strong>{t.title}</Typography.Text>
                <Tag color={st.color}>{st.label}</Tag>
                {!identityReady && <Tag>任务身份不可确认</Tag>}
                {t.skill && <Tag title="本任务使用的 Skill（创建时的快照）">Skill：{t.skill.name}</Tag>}
              </Space>
              {activeTask && (
                <Progress
                  percent={t.progress}
                  size="small"
                  status={t.status === 'running' ? 'active' : 'normal'}
                  style={{ marginBottom: 0 }}
                />
              )}
              <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                {t.status === 'queued' ? queueLabel(t) : stage}
                {stage || t.status === 'queued' ? ' · ' : ''}
                {activeTask || paused
                  ? `创建于 ${formatTime(t.created_at)}`
                  : `结束于 ${formatTime(t.finished_at ?? t.created_at)}`}
              </Typography.Text>
              {paused && t.kind === 'book.import' && <BookImportPauseLine task={t} />}
              {retryBlockedHint && (
                <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                  {retryBlockedHint}
                </Typography.Text>
              )}
              {t.error && !activeTask && (
                <Typography.Text type={t.status === 'failed' ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
                  {t.error}
                </Typography.Text>
              )}
            </div>
          </List.Item>
        )
      }}
    />
  )
}

/** 拆书反推待处理时的暂停原因（取自断点摘要） */
function BookImportPauseLine({ task }: { task: Task }) {
  const r = bookImportPause(task.checkpoint)
  if (!r) return null
  return (
    <Typography.Text type="danger" style={{ fontSize: 12, display: 'block' }}>
      {bookImportPauseText(r)}
    </Typography.Text>
  )
}

/** 查看任务输出：进行中实时订阅，已结束读取 /output。 */
export function TaskOutputModal({ task, onClose }: { task: Task; onClose: () => void }) {
  const [boundSession] = useState(currentRequestGeneration)
  const [scope] = useState(() => task.project_id === null ? null : captureProjectScope(task.project_id))
  const scopeVersion = useProjectStore((state) => state.scopeVersion)
  const session = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const project = useProjectStore((state) => state.project)
  const user = useAuthStore((state) => state.user)
  const live = isActiveTask(task.status)
  const scopeCurrent = !!scope && scopeVersion === scope.version
  const mode = taskOutputMode({
    sessionCurrent: session === boundSession && (!scope || scopeCurrent),
    scoped: !!scope,
    live,
    kind: task.kind,
    canRead: scopeCurrent && isProjectScopeCurrent(scope, 'canRead'),
    canUseAI: scopeCurrent && isProjectScopeCurrent(scope, 'canUseAI'),
    isCollaborator: !!scope && projectAccess(project, user, scope.projectId).isCollaborator,
  })
  const allowed = mode !== 'denied'
  const stream = useTaskStream()
  const { follow } = stream
  const loader = useCallback(
    () => (mode !== 'output' ? Promise.resolve('') : taskApi.output({ id: task.id, task_key: task.task_key }).then((r) => r.text)),
    [mode, task.id, task.task_key],
  )
  const { data: finishedText = '', loading, error } = useLoader(loader)
  // 拆书反推：通读批次进度（初值取断点摘要，batch_end 事件推进）
  const [batches, setBatches] = useState(() => (task.kind === 'book.import' ? bookImportBatches(task.checkpoint) : null))

  useEffect(() => {
    if (mode === 'stream')
      void follow(task, {
        onBatchEnd: (e) => setBatches((prev) => ({ done: Math.max(prev?.done ?? 0, e.index + 1), total: e.total })),
      })
    // task 对象每次轮询都会变化；任务实例身份变化时必须重新订阅。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, task.id, task.task_key, follow])

  const text = mode === 'stream' ? stream.text : mode === 'output' ? finishedText : ''
  const current = live ? (stream.task ?? task) : task
  return (
    <Modal open title={task.title} footer={null} width={760} onCancel={onClose} destroyOnHidden>
      {!allowed ? <Typography.Text type="warning">会话或作品授权已变化，请重新打开任务。</Typography.Text> : <Space direction="vertical" style={{ width: '100%' }}>
        <Space wrap>
          <Tag color={TASK_STATUS[current.status].color}>{TASK_STATUS[current.status].label}</Tag>
          {task.skill && <Tag>Skill：{task.skill.name}</Tag>}
          {current.status === 'queued' && <Typography.Text type="secondary">{queueLabel(current)}</Typography.Text>}
          {current.stage && current.status !== 'queued' && <Typography.Text type="secondary">{current.stage}</Typography.Text>}
          {batches && (
            <Typography.Text type="secondary">
              通读 {batches.done}/{batches.total} 批
            </Typography.Text>
          )}
          {stream.reconnecting && <Typography.Text type="warning">连接中断，正在重连…</Typography.Text>}
        </Space>
        {mode === 'status' && <Typography.Text type="secondary">当前身份不能实时查看此任务的输出，任务结束后可在「最近」中查看。</Typography.Text>}
        {mode === 'stream' && !scope && stream.error && !canStreamTask(task.kind, true) && <Typography.Text type="secondary">若该作品由他人共享，此类任务暂不支持实时查看，任务结束后可在「最近」中查看输出。</Typography.Text>}
        {(error || stream.error) && <Typography.Text type="danger">{error ?? stream.error}</Typography.Text>}
        <Spin spinning={!live && loading}>
          <StreamOutput text={text} running={mode === 'stream' && stream.running} maxHeight={480} placeholder="暂无输出" />
        </Spin>
      </Space>}
    </Modal>
  )
}
