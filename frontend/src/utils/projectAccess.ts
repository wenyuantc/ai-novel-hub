import type { Project, User } from '../api/types'

/** 只控制界面能力；真实内容权限仍由服务器按每次请求校验。 */
export function projectAccess(project: Pick<Project, 'id' | 'owner'> | null | undefined, user: Pick<User, 'id' | 'is_admin'> | null, projectId: number) {
  const ready = Boolean(project && project.id === projectId && user)
  const otherOwner = ready && project?.owner != null && project.owner.id !== user?.id
  const managingOther = Boolean(otherOwner && user?.is_admin)
  return { ready, managingOther, canUseAI: ready && !otherOwner }
}

/** 危险操作必须输入完整名称，空输入和首尾多余空白均不算确认。 */
export function canConfirmProjectDelete(input: string, title: string) {
  return input.length > 0 && input === title
}
