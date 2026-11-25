import { afterEach, describe, expect, it, vi } from 'vitest'
import { invalidateRequestGeneration } from './client'
import { FactsController, type FactsState } from '../components/collaboration/factsController'
import {
  invalidateNotifications, notificationPath, notificationRevision, notificationsForActor, subscribeNotifications, type InboxPage,
} from './notifications'

const actor = { id: 7, createdAt: '2026-11-25T10:00:00Z' }
const notificationId = '4213a560-3d18-4d81-a56b-7888e4987321'
const notificationsApi = notificationsForActor(actor, vi.fn())

afterEach(() => vi.unstubAllGlobals())

describe('站内通知', () => {
  it('分页、单条读写和偏好均访问当前账号，并传递取消信号', async () => {
    const fetch = vi.fn().mockImplementation(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetch)
    const signal = new AbortController().signal
    await notificationsApi.list(null, signal)
    await notificationsApi.list('cursor:30', signal)
    await notificationsApi.unread(signal)
    await notificationsApi.read(notificationId, signal)
    await notificationsApi.open(notificationId, signal)
    await notificationsApi.preferences(signal)
    await notificationsApi.setPreference('consistency', false, signal)
    expect(fetch.mock.calls.map(([url, init]) => [url, init.method])).toEqual([
      ['/api/v1/notifications?limit=20', 'GET'],
      ['/api/v1/notifications?before=cursor%3A30&limit=20', 'GET'],
      ['/api/v1/notifications/unread-count', 'GET'],
      ['/api/v1/notifications/4213a560-3d18-4d81-a56b-7888e4987321/read', 'POST'],
      ['/api/v1/notifications/4213a560-3d18-4d81-a56b-7888e4987321/open', 'POST'],
      ['/api/v1/notifications/preferences', 'GET'],
      ['/api/v1/notifications/preferences/consistency', 'PUT'],
    ])
    expect(fetch.mock.calls.at(-1)?.[1].body).toEqual(JSON.stringify({ enabled: false }))
    for (const [, init] of fetch.mock.calls) {
      expect(init).toMatchObject({ signal, credentials: 'include', headers: {
        'X-Editor-Actor-Id': '7', 'X-Editor-Actor-Created-At': actor.createdAt,
      } })
    }
  })

  it('另一标签页更换 Cookie 时冻结当前收件箱', async () => {
    vi.stubGlobal('fetch', () => Promise.resolve(Response.json({ detail: '账号已变化', code: 'editor_actor_changed' }, { status: 409 })))
    const actorChanged = vi.fn()
    const api = notificationsForActor(actor, actorChanged)
    await expect(api.unread(new AbortController().signal)).rejects.toMatchObject({ status: 409 })
    expect(actorChanged).toHaveBeenCalledOnce()
  })

  it('用固定业务路由定位邀请、任务、两种预算和特定检查', () => {
    expect(notificationPath({ kind: 'invitation', scope: 'series', invitation_id: 'one&two' }, notificationId)).toBe('/projects?invitation_id=one%26two&invitation_scope=series')
    expect(notificationPath({ kind: 'task', task_id: 15, task_key: 'old?key' }, notificationId)).toBe('/projects?task_id=15&task_key=old%3Fkey')
    expect(notificationPath({ kind: 'budget', project_id: null, currency: 'CNY', budget_key: 'account/1' }, notificationId)).toBe('/settings/ai-usage?budget_currency=CNY&budget_key=account%2F1')
    expect(notificationPath({ kind: 'budget', project_id: 8, currency: 'USD', budget_key: 'project/8' }, notificationId)).toBe('/projects/8/ai-usage?budget_currency=USD&budget_key=project%2F8')
    expect(notificationPath({ kind: 'consistency', project_id: 8, run_id: 4 }, notificationId)).toBe('/projects/8/consistency?run_id=4&notification_id=4213a560-3d18-4d81-a56b-7888e4987321')
  })

  it('会话已切换时丢弃前一账号在途的收件页', async () => {
    let respond!: (response: Response) => void
    vi.stubGlobal('fetch', () => new Promise<Response>(resolve => { respond = resolve }))
    const publish = vi.fn<(state: FactsState<InboxPage>) => void>()
    const inbox = new FactsController(signal => notificationsApi.list(null, signal), () => true, publish)
    const pending = inbox.refresh()
    invalidateRequestGeneration()
    respond(Response.json({ items: [{ title: '上一账号的邀请' }], next_before: null }))
    await pending
    expect(publish.mock.calls.every(([state]) => state.data === null)).toBe(true)
    inbox.dispose()
  })

  it('业务目标失效时收件查询代次刷新，离开后解除监听', () => {
    const before = notificationRevision()
    const updates = vi.fn()
    const stop = subscribeNotifications(updates)
    invalidateNotifications()
    expect(notificationRevision()).toBe(before + 1)
    expect(updates).toHaveBeenCalledOnce()
    stop()
    invalidateNotifications()
    expect(updates).toHaveBeenCalledOnce()
  })
})
