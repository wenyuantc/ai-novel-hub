import type { UsageCurrency } from './aiUsage'
import { ApiError, request, type EditorActorIdentity, type RequestOptions } from './client'

export type NotificationKind = 'invitation' | 'task' | 'budget' | 'consistency'
export type NotificationPreferences = Record<NotificationKind, boolean>
export interface InboxItem {
  id: string
  kind: NotificationKind
  title: string
  created_at: string
  read_at: string | null
}
export interface InboxPage { items: InboxItem[]; next_before: string | null }
export type NotificationTarget =
  | { kind: 'invitation'; scope: 'project' | 'series'; invitation_id: string }
  | { kind: 'task'; task_id: number; task_key: string }
  | { kind: 'budget'; project_id: number | null; currency: UsageCurrency; budget_key: string }
  | { kind: 'consistency'; project_id: number; run_id: number }

export function notificationsForActor(editorActor: EditorActorIdentity, actorChanged: () => void) {
  async function send<T>(path: string, options: RequestOptions): Promise<T> {
    try {
      return await request<T>(path, { ...options, editorActor })
    } catch (error) {
      if (!options.signal?.aborted && error instanceof ApiError && error.body?.code === 'editor_actor_changed') actorChanged()
      throw error
    }
  }
  return {
    list: (before: string | null, signal: AbortSignal) =>
      send<InboxPage>('/notifications', { query: { before, limit: 20 }, signal }),
    unread: (signal: AbortSignal) => send<{ count: number }>('/notifications/unread-count', { signal }),
    read: (id: string, signal: AbortSignal) => send<void>(`/notifications/${encodeURIComponent(id)}/read`, { method: 'POST', signal }),
    open: (id: string, signal: AbortSignal) => send<NotificationTarget>(`/notifications/${encodeURIComponent(id)}/open`, { method: 'POST', signal }),
    preferences: (signal: AbortSignal) => send<NotificationPreferences>('/notifications/preferences', { signal }),
    setPreference: (kind: NotificationKind, enabled: boolean, signal: AbortSignal) =>
      send<NotificationPreferences>(`/notifications/preferences/${kind}`, { method: 'PUT', body: { enabled }, signal }),
  }
}

/** 业务页确认邀请已处理或目标已失效时，使仍在途的收件查询一并失效。 */
let revision = 0
const listeners = new Set<() => void>()
export const notificationRevision = () => revision
export function subscribeNotifications(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}
export function invalidateNotifications() {
  revision += 1
  for (const listener of listeners) listener()
}

export function notificationPath(target: NotificationTarget, notificationId: string): string {
  switch (target.kind) {
    case 'invitation': return `/projects?invitation_id=${encodeURIComponent(target.invitation_id)}&invitation_scope=${target.scope}`
    case 'task': return `/projects?task_id=${target.task_id}&task_key=${encodeURIComponent(target.task_key)}`
    case 'budget': return `${target.project_id === null ? '/settings' : `/projects/${target.project_id}`}/ai-usage?budget_currency=${encodeURIComponent(target.currency)}&budget_key=${encodeURIComponent(target.budget_key)}`
    case 'consistency': return `/projects/${target.project_id}/consistency?run_id=${target.run_id}&notification_id=${encodeURIComponent(notificationId)}`
  }
}
