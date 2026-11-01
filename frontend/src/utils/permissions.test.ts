import { describe, expect, it } from 'vitest'
import type { PermissionCode, PermissionDefinition, User } from '../api/types'
import { adminDestinations, authorizationKey, can, isSuperAdmin } from './permissions'
import { permissionSelection } from './roles'
import { quotaConfig, quotaDescription } from './quotas'
import { projectAccess } from './projectAccess'

const user = (permissions: PermissionCode[] = []): User => ({ id: 1, username: 'member', is_admin: false, is_super_admin: false, effective_permissions: permissions, authorization_revision: 1, roles: [], created_at: '' })

describe('明确操作权限，不由布尔管理员或跨域读取推导授权', () => {
  it('普通账号默认无管理能力，旧is_admin不升级权限，自定义全权也不等于超管', () => {
    const legacy = { ...user(), is_admin: true }
    expect(can(legacy, 'users.read')).toBe(false)
    expect(isSuperAdmin(user(['users.read', 'users.delete']))).toBe(false)
    expect(adminDestinations(user())).toEqual([])
    expect(adminDestinations(user(['tasks.read'])).map((item) => item.path)).toEqual(['/admin/tasks'])
    expect(adminDestinations({ ...user(), is_super_admin: true }).some((item) => item.path === '/admin/roles')).toBe(true)
  })
  it('权限并集由服务端提供；授写不暗含read，不把他人AI或阅读进度扩权', () => {
    const actor = user(['projects.edit', 'users.read'])
    const access = projectAccess({ id: 3, owner: { id: 2, username: 'other' } }, actor, 3)
    expect(access).toMatchObject({ managingOther: true, canRead: false, canEdit: true, canRecycle: false, canUseAI: false })
    expect(can(actor, 'tasks.read')).toBe(false)
    expect(authorizationKey(actor)).not.toBe(authorizationKey({ ...actor, authorization_revision: 2 }))
    expect(authorizationKey(actor)).toBe(authorizationKey({ ...actor, effective_permissions: [...actor.effective_permissions].reverse() }))
  })
  it('角色编辑显式补齐配额用户读取依赖，但不添加别的跨域read', () => {
    const definitions: PermissionDefinition[] = [
      { code: 'users.read', label: '查看用户', group: '用户', dangerous: false, requires: [] },
      { code: 'quotas.read', label: '查看配额', group: '配额', dangerous: false, requires: ['users.read'] },
      { code: 'quotas.update', label: '改配额', group: '配额', dangerous: true, requires: ['quotas.read'] },
      { code: 'projects.edit', label: '编辑作品', group: '作品', dangerous: true, requires: [] },
    ]
    expect(permissionSelection(['quotas.update', 'projects.edit'], definitions)).toEqual(['users.read', 'quotas.read', 'quotas.update', 'projects.edit'])
  })
})

describe('配额三态严格值语义', () => {
  it('0不变成无限或默认；继承和无限不携带旧value', () => {
    expect(quotaConfig({ mode: 'limited', value: 0 })).toEqual({ mode: 'limited', value: 0 })
    expect(quotaConfig({ mode: 'inherit', value: 8 })).toEqual({ mode: 'inherit' })
    expect(quotaConfig({ mode: 'unlimited', value: 8 })).toEqual({ mode: 'unlimited' })
    for (const value of [null, -1, 1.5, NaN, Infinity, 2_147_483_648]) expect(quotaConfig({ mode: 'limited', value })).toBeNull()
  })
  it('0、继承与无限有不同展示来源', () => {
    expect(quotaDescription({ config: { mode: 'inherit' }, effective_limit: 0, used: 2, source: 'system' })).toBe('继承默认：0')
    expect(quotaDescription({ config: { mode: 'limited', value: 0 }, effective_limit: 0, used: 2, source: 'user' })).toBe('用户指定：0')
    expect(quotaDescription({ config: { mode: 'unlimited' }, effective_limit: null, used: 2, source: 'user' })).toBe('用户指定：不限额')
  })
})
