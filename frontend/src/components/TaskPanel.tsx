import { ClearOutlined, ReloadOutlined, UnorderedListOutlined } from '@ant-design/icons'
import { App, Button, Drawer, Empty, FloatButton, List, Modal, Popconfirm, Progress, Space, Spin, Tabs, Tag, Typography } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { errorMessage } from '../api/client'
import { canRetryTask, isActiveTask, queueLabel, TASK_STATUS, taskApi, taskLink } from '../api/tasks'
import type { Task } from '../api/types'
import { useLoader } from '../hooks/useLoader'
import { useTaskStream } from '../hooks/useTaskStream'
import { useTasksStore } from '../stores/tasks'
import { formatTime } from '../utils/time'
import StreamOutput from './StreamOutput'

/** 右下角后台任务面板：活动任务数徽标 + 抽屉（进行中 / 最近），支持取消、重试、查看输出、跳转、删除、清理。 */
export default function TaskPanel() {
  const { message } = App.useApp()
  const active = useTasksStore((s) => s.active)
  const version = useTasksStore((s) => s.version)
  const startPolling = useTasksStore((s) => s.startPolling)
  const track = useTasksStore((s) => s.track)
  const bump = useTasksStore((s) => s.bump)
  const refresh = useTasksStore((s) => s.refresh)
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState('active')
  const [viewing, setViewing] = useState<Task | null>(null)
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
      <FloatButton
        icon={<UnorderedListOutlined />}
        tooltip="后台任务"
        badge={{ count: active.length, color: 'blue' }}
        onClick={() => setOpen(true)}
      />
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
                <TaskList tasks={active} onView={setViewing} onTrack={track} onRemoved={bump} onNavigate={() => setOpen(false)} />
              ),
            },
            {
              key: 'recent',
              label: '最近',
              children: (
                <Spin spinning={recentLoading}>
                  <TaskList tasks={recent} onView={setViewing} onTrack={track} onRemoved={bump} onNavigate={() => setOpen(false)} />
                </Spin>
              ),
            },
          ]}
        />
      </Drawer>
      {viewing && <TaskOutputModal task={viewing} onClose={() => setViewing(null)} />}
    </>
  )
}

interface ListProps {
  tasks: Task[]
  onView: (t: Task) => void
  onTrack: (t: Task) => void
  onRemoved: () => void
  onNavigate: () => void
}

function TaskList({ tasks, onView, onTrack, onRemoved, onNavigate }: ListProps) {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [pending, setPending] = useState<number | null>(null)

  const act = async (id: number, fn: () => Promise<void>) => {
    setPending(id)
    try {
      await fn()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setPending(null)
    }
  }

  if (tasks.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无任务" />

  return (
    <List
      dataSource={tasks}
      renderItem={(t) => {
        const st = TASK_STATUS[t.status]
        const activeTask = isActiveTask(t.status)
        const link = taskLink(t)
        const actions = [
          <Button key="view" type="link" size="small" onClick={() => onView(t)}>
            查看输出
          </Button>,
        ]
        if (link) {
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
              跳转
            </Button>,
          )
        }
        if (activeTask) {
          actions.push(
            <Popconfirm
              key="cancel"
              title="取消该任务？"
              description="已输出的内容不会保存，章节保持原样。"
              okText="取消任务"
              cancelText="不取消"
              onConfirm={() =>
                act(t.id, async () => {
                  onTrack(await taskApi.cancel(t.id))
                  message.success('已取消')
                })
              }
            >
              <Button type="link" size="small" danger loading={pending === t.id}>
                取消
              </Button>
            </Popconfirm>,
          )
        } else {
          if (canRetryTask(t.status)) {
            actions.push(
              <Button
                key="retry"
                type="link"
                size="small"
                loading={pending === t.id}
                onClick={() =>
                  void act(t.id, async () => {
                    onTrack(await taskApi.retry(t.id))
                    message.success('已重新提交')
                  })
                }
              >
                重试
              </Button>,
            )
          }
          actions.push(
            <Popconfirm
              key="del"
              title="删除该任务记录？"
              okText="删除"
              cancelText="取消"
              onConfirm={() =>
                act(t.id, async () => {
                  await taskApi.remove(t.id)
                  onRemoved()
                })
              }
            >
              <Button type="link" size="small" danger>
                删除
              </Button>
            </Popconfirm>,
          )
        }
        return (
          <List.Item key={t.id} actions={actions} style={{ flexWrap: 'wrap' }}>
            <div style={{ width: '100%' }}>
              <Space wrap size={4}>
                <Typography.Text strong>{t.title}</Typography.Text>
                <Tag color={st.color}>{st.label}</Tag>
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
                {t.status === 'queued' ? queueLabel(t) : t.stage}
                {t.stage || t.status === 'queued' ? ' · ' : ''}
                {activeTask ? `创建于 ${formatTime(t.created_at)}` : `结束于 ${formatTime(t.finished_at ?? t.created_at)}`}
              </Typography.Text>
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

/** 查看任务输出：进行中实时订阅，已结束读取 /output。 */
function TaskOutputModal({ task, onClose }: { task: Task; onClose: () => void }) {
  const live = isActiveTask(task.status)
  const stream = useTaskStream()
  const { follow } = stream
  const loader = useCallback(
    () => (live ? Promise.resolve('') : taskApi.output(task.id).then((r) => r.text)),
    [live, task.id],
  )
  const { data: finishedText = '', loading, error } = useLoader(loader)

  useEffect(() => {
    if (live) void follow(task)
    // task 对象每次轮询都会变化，只按 id 订阅一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, task.id, follow])

  const text = live ? stream.text : finishedText
  const current = live ? (stream.task ?? task) : task
  return (
    <Modal open title={task.title} footer={null} width={760} onCancel={onClose} destroyOnHidden>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Space wrap>
          <Tag color={TASK_STATUS[current.status].color}>{TASK_STATUS[current.status].label}</Tag>
          {current.status === 'queued' && <Typography.Text type="secondary">{queueLabel(current)}</Typography.Text>}
          {current.stage && current.status !== 'queued' && <Typography.Text type="secondary">{current.stage}</Typography.Text>}
          {stream.reconnecting && <Typography.Text type="warning">连接中断，正在重连…</Typography.Text>}
        </Space>
        {(error || stream.error) && <Typography.Text type="danger">{error ?? stream.error}</Typography.Text>}
        <Spin spinning={!live && loading}>
          <StreamOutput text={text} running={live && stream.running} maxHeight={480} placeholder="暂无输出" />
        </Spin>
      </Space>
    </Modal>
  )
}
