import { BellOutlined } from '@ant-design/icons'
import { Alert, Badge, Button, Drawer, Space, Switch } from 'antd'
import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../../api/client'
import {
  invalidateNotifications, notificationPath, notificationRevision, notificationsForActor, subscribeNotifications,
  type InboxItem, type NotificationKind, type NotificationTarget,
} from '../../api/notifications'
import { useAuthStore } from '../../stores/auth'
import { formatTime } from '../../utils/time'
import { startForegroundPolling } from '../../utils/visiblePolling'
import { useCollaborationFacts } from '../collaboration/useCollaborationFacts'
import './notifications.css'

const labels: Record<NotificationKind, string> = {
  invitation: '协作邀请', task: '后台任务', budget: '预算阈值', consistency: '一致性检查结果',
}
const kinds = Object.keys(labels) as NotificationKind[]

type InboxApi = ReturnType<typeof notificationsForActor>

function NotificationSettings({ api }: { api: InboxApi }) {
  const prefs = useCollaborationFacts('notification-preferences', true, api.preferences)
  const settings = prefs.data
  return <>
    {prefs.loading && !prefs.data && <p role="status">正在加载通知偏好…</p>}
    {prefs.error && <Alert type="error" message={prefs.error} action={<Button onClick={prefs.refresh}>重试</Button>} />}
    {prefs.notice && <p role="status">{prefs.notice}</p>}
    {settings && <ul className="notification-list">{kinds.map(kind =>
      <li key={kind} className="notification-pref"><span>{labels[kind]}</span>
        <Switch aria-label={labels[kind]} checked={settings[kind]} disabled={prefs.stale || prefs.busy || prefs.loading}
          onChange={enabled => void prefs.mutate(signal => api.setPreference(kind, enabled, signal), '通知偏好已保存')} />
      </li>)}</ul>}
  </>
}

export default function NotificationInbox({ actorId, createdAt }: { actorId: number; createdAt: string }) {
  const [actorChanged, setActorChanged] = useState(false)
  const api = useMemo(() => notificationsForActor({ id: actorId, createdAt }, () => setActorChanged(true)), [actorId, createdAt])
  const navigate = useNavigate()
  const [view, setView] = useState<'list' | 'settings' | null>(null)
  const [cursors, setCursors] = useState<string[]>([])
  const [notice, setNotice] = useState('')
  const revision = useSyncExternalStore(subscribeNotifications, notificationRevision, notificationRevision)
  const count = useCollaborationFacts(`notification-count:${revision}`, !actorChanged, api.unread)
  const before = cursors.at(-1) ?? null
  const readPage = useCallback((signal: AbortSignal) => api.list(before, signal), [api, before])
  const list = useCollaborationFacts(`notifications:${before}:${revision}`, view === 'list' && !actorChanged, readPage, count.refresh)
  const refreshCount = count.refresh
  const refreshList = list.refresh
  useEffect(() => startForegroundPolling(() => {
    refreshCount()
    if (view === 'list') refreshList()
  }, document, window, 30_000), [refreshCount, refreshList, view])

  const activate = async (item: InboxItem, open: boolean) => {
    setNotice('')
    let target: NotificationTarget | undefined
    const success = await list.mutate(async signal => {
      try {
        if (open) target = await api.open(item.id, signal)
        else await api.read(item.id, signal)
      } catch (error) {
        if (!signal.aborted && list.isCurrent() && error instanceof ApiError && [403, 404].includes(error.status)) {
          setNotice('这条通知已失效。')
          invalidateNotifications()
        }
        throw error
      }
    }, open ? '' : '已标为已读')
    if (success && target && list.isCurrent() && revision === notificationRevision()) {
      setView(null)
      navigate(notificationPath(target, item.id))
    }
  }
  const openInbox = () => {
    setNotice('')
    setCursors([])
    setView('list')
    count.refresh()
  }
  const blocked = list.busy || list.loading || list.stale
  const unread = actorChanged || count.stale ? undefined : count.data?.count
  return <>
    <Badge count={unread} size="small">
      <Button type="text" aria-label={unread === undefined ? '站内通知' : `站内通知，${unread} 条未读`}
        title="站内通知" icon={<BellOutlined />} onClick={openInbox} />
    </Badge>
    <Drawer open={view !== null} onClose={() => setView(null)} width="min(440px, 100vw)"
      title={view === 'settings' ? '通知偏好' : '站内通知'} extra={!actorChanged && <Button type="link" onClick={() => {
        setNotice('')
        setView(view === 'settings' ? 'list' : 'settings')
      }}>{view === 'settings' ? '返回收件箱' : '通知偏好'}</Button>}>
      {actorChanged && <Alert type="warning" message="登录账号已变化，请刷新账号信息。"
        action={<Button onClick={() => void useAuthStore.getState().init()}>刷新账号</Button>} />}
      {!actorChanged && view === 'settings' && <NotificationSettings api={api} />}
      {!actorChanged && view === 'list' && <>
        {count.error && <Alert type="warning" message="未读数加载失败" action={<Button onClick={count.refresh}>重试</Button>} />}
        {list.error && <Alert type="error" message={list.error} action={<Button onClick={list.refresh}>重新加载</Button>} />}
        {(notice || list.notice) && <p role="status">{notice || list.notice}</p>}
        {list.loading && !list.data && <p role="status">正在加载通知…</p>}
        {list.data?.items.length === 0 && <p>{before === null ? '暂无通知' : '本页暂无通知'}</p>}
        {list.data && list.data.items.length > 0 && <ul className="notification-list">{list.data.items.map(item =>
          <li className="notification-row" key={item.id}>
            <div className="notification-meta"><span>{labels[item.kind]}</span>
              <time dateTime={item.created_at}>{formatTime(item.created_at)}</time></div>
            <button type="button" className="notification-title" disabled={blocked} onClick={() => void activate(item, true)}>{item.title}</button>
            <div className="notification-status">{item.read_at ? '已读' : <><span>未读</span>
              <Button type="link" size="small" disabled={blocked} aria-label={`将「${item.title}」标为已读`}
                onClick={() => void activate(item, false)}>标为已读</Button></>}</div>
          </li>)}</ul>}
        {(before !== null || list.data?.next_before != null) && <Space>
          <Button disabled={before === null || list.busy} onClick={() => { setNotice(''); setCursors(cursors.slice(0, -1)) }}>上一页</Button>
          <Button disabled={list.data?.next_before == null || blocked} onClick={() => {
            const next = list.data?.next_before
            if (next != null) { setNotice(''); setCursors([...cursors, next]) }
          }}>下一页</Button>
        </Space>}
      </>}
    </Drawer>
  </>
}
