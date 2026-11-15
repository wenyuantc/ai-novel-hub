import { ApiError } from '../../api/client'
import type { ProjectAccess } from '../../api/types'

/** 卡片遵循作者或已接受作品协作者的入口；纯管理员沿用原有作者限制。 */
export function cardAccess(role: ProjectAccess['role'], isCollaborator: boolean, canRead: boolean, canEdit: boolean) {
  const canPreview = canRead && (role === 'owner' || isCollaborator)
  return { canPreview, canImport: canPreview && canEdit }
}

export function cardNeedsAccessRefresh(error: unknown, current: boolean): boolean {
  return current && error instanceof ApiError && (error.status === 403 || error.status === 404)
}
