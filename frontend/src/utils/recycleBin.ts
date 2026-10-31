import type { RecycleProject } from '../api/types'
import { canConfirmProjectDelete } from './projectAccess'
import { parseServerTime } from './time'

export const PROJECT_DELETE_NOTICE = '作品将移入回收站并保留 30 天，期间仅管理员可以恢复；到期自动彻底清理。删除前会自动取消排队、运行和待审核任务并等待收尾；失败或超时则作品不会删除，已取消的任务不会自动恢复。'

/** 前端只提前禁用明显超期操作，最终期限和权限始终由服务器裁决。 */
export function canRestoreProject(project: Pick<RecycleProject, 'restorable' | 'expires_at'>, now = Date.now()): boolean {
  const expiresAt = parseServerTime(project.expires_at)
  return project.restorable && expiresAt.isValid() && now < expiresAt.valueOf()
}

export function canPurgeProject(input: string, title: string): boolean {
  return canConfirmProjectDelete(input, title)
}

export function recycleValidPage(page: number, total: number, pageSize: number): number {
  return Math.max(1, Math.min(page, Math.ceil(total / pageSize)))
}

/** 弹窗关闭、目标变化、登出后使旧响应失效；代次不替代后端 deleted_at 校验。 */
export function createRecycleRequestGate() {
  let generation = 0
  return {
    begin: () => ++generation,
    invalidate: () => { generation += 1 },
    isCurrent: (ticket: number) => generation === ticket,
  }
}
