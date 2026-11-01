import { Tag } from 'antd'
import type { AdminUser } from '../../api/types'
import { isSuperAdmin } from '../../utils/permissions'

export default function RoleTags({ user }: { user: Pick<AdminUser, 'roles' | 'is_admin' | 'is_super_admin'> }) {
  return <>{user.roles?.length ? user.roles.map((role) => <Tag key={role.id} color={role.system_key === 'super_admin' ? 'gold' : undefined}>{role.name}</Tag>) : <Tag color={isSuperAdmin(user) ? 'gold' : undefined}>{isSuperAdmin(user) ? '超级管理员' : '普通用户'}</Tag>}</>
}
