import { Button, Space, Tooltip } from 'antd'
import type { ReactElement } from 'react'
import type { AdminUser } from '../../api/types'
import { rowActions, userDeleteBlockReason } from '../../utils/adminUsers'
import type { AdminUserAction } from './adminUserActionsController'
import { useAuthStore } from '../../stores/auth'

interface Props {
  user: AdminUser
  actorId: number
  disabled: boolean
  onAction: (user: AdminUser, action: AdminUserAction) => void
}

/** 两个管理入口共用原有自身/删号保护，服务器仍做最终权限校验。 */
export default function AdminUserActionButtons({ user, actorId, disabled, onAction }: Props) {
  const actor = useAuthStore((state) => state.user)
  const actions = rowActions(user, actorId, actor)
  const guarded = (allowed: boolean, button: ReactElement, reason = '不能对自己执行该操作') => allowed ? button : <Tooltip title={reason}>{button}</Tooltip>
  return <Space size={0} wrap>
    {guarded(actions.canResetPassword, <Button type="link" size="small" disabled={disabled || !actions.canResetPassword} onClick={() => onAction(user, 'reset')}>重置密码</Button>)}
    {guarded(actions.canToggleActive, <Button type="link" size="small" danger={user.is_active} disabled={disabled || !actions.canToggleActive} onClick={() => onAction(user, 'active')}>{user.is_active ? '禁用' : '启用'}</Button>)}
    {guarded(actions.canDelete, <Button type="link" size="small" danger disabled={disabled || !actions.canDelete} onClick={() => onAction(user, 'delete')}>删除</Button>, actions.isSelf ? '不能对自己执行该操作' : userDeleteBlockReason(user) ?? undefined)}
  </Space>
}
