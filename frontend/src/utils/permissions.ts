import type { PermissionCode } from '../api/types'

export interface AuthorizationSubject {
  is_super_admin: boolean
  effective_permissions: readonly PermissionCode[]
  authorization_revision?: number
}

/** 超管由服务端明确标记，旧 is_admin 字段不参与前端授权。 */
export function isSuperAdmin(user: Pick<AuthorizationSubject, 'is_super_admin'> | null | undefined): boolean {
  return user?.is_super_admin === true
}

export function can(user: AuthorizationSubject | null | undefined, permission: PermissionCode): boolean {
  return !!user && (isSuperAdmin(user) || user.effective_permissions?.includes(permission) === true)
}

export function hasManagementAccess(user: AuthorizationSubject | null | undefined): boolean {
  return isSuperAdmin(user) || (user?.effective_permissions?.length ?? 0) > 0
}

/** 用于请求/弹窗的身份代次；相同actor撤权再恢复也不能接纳旧操作。 */
export function authorizationKey(user: AuthorizationSubject | null | undefined): string {
  return `${isSuperAdmin(user)}:${user?.authorization_revision ?? 0}:${[...(user?.effective_permissions ?? [])].sort().join(',')}`
}

export const ADMIN_DESTINATIONS: ReadonlyArray<{ key: string; path: string; label: string; permission?: PermissionCode; superOnly?: boolean }> = [
  { key: 'admin-dashboard', path: '/admin/dashboard', label: '管理员首页', permission: 'dashboard.read' },
  { key: 'admin-tasks', path: '/admin/tasks', label: '全站任务监控', permission: 'tasks.read' },
  { key: 'admin', path: '/admin/users', label: '用户管理', permission: 'users.read' },
  { key: 'admin-roles', path: '/admin/roles', label: '角色与权限', superOnly: true },
  { key: 'admin-projects', path: '/admin/projects', label: '作品管理', permission: 'projects.read' },
  { key: 'admin-recycle-bin', path: '/admin/recycle-bin', label: '作品回收站', permission: 'recycle_bin.read' },
  { key: 'admin-audit-logs', path: '/admin/audit-logs', label: '操作日志', permission: 'audit.read' },
  { key: 'admin-backups', path: '/admin/backups', label: '整库备份', permission: 'backups.read' },
  { key: 'admin-system-settings', path: '/admin/system-settings', label: '系统设置', permission: 'settings.read' },
]

export function adminDestinations(user: AuthorizationSubject | null | undefined) {
  return ADMIN_DESTINATIONS.filter((item) => item.superOnly ? isSuperAdmin(user) : !!item.permission && can(user, item.permission))
}

export function firstAdminPath(user: AuthorizationSubject | null | undefined): string {
  return adminDestinations(user)[0]?.path ?? '/projects'
}
