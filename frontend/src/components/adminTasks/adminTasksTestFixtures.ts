import type { AdminTaskBrief, AdminTaskDetail, AdminTaskList } from '../../api/types'

export const TASK_KEY = '00000000-0000-4000-8000-000000000001'
export const OTHER_TASK_KEY = '00000000-0000-4000-8000-000000000002'
export const AS_OF = '2026-10-31T10:00:00Z'
export function task(overrides: Partial<AdminTaskBrief> = {}): AdminTaskBrief {
  return {
    id: 42, task_key: TASK_KEY, kind: 'agent.turn', title: '项目智能体 #42', status: 'running', progress: 25,
    owner_user_id: 2, owner_username: '作者甲', project_id: 8, project_title: '测试作品', project_lifecycle: 'active', chapter_id: null,
    stage: '调用工具', error_code: null, error_message: null, created_at: '2026-10-31T09:00:00Z', queued_at: '2026-10-31T09:50:00Z', started_at: '2026-10-31T09:51:00Z', finished_at: null,
    queued_seconds: null, running_seconds: 540, cancellation_pending: false, cancel_requested_at: null, can_cancel: true, ...overrides,
  }
}
export const detail = (overrides: Partial<AdminTaskDetail> = {}): AdminTaskDetail => ({ ...task(), as_of: AS_OF, ...overrides })
export function list(items = [task()], total = items.length): AdminTaskList {
  const counts: AdminTaskList['counts'] = { queued: 0, running: 0, paused: 0, succeeded: 0, failed: 0, cancelled: 0, interrupted: 0, unknown: 0 }
  for (const item of items) counts[item.status] += 1
  return { items, total, counts, as_of: AS_OF }
}
