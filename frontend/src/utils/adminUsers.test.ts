import { describe, expect, it } from 'vitest'
import type { AdminUser, User } from '../api/types'
import { adminGuard, canConfirmDelete, rowActions } from './adminUsers'

const admin: User = { id: 1, username: 'root', is_admin: true, created_at: '2026-10-23T00:00:00' }
const normal: User = { ...admin, id: 2, username: 'alice', is_admin: false }

function row(overrides: Partial<AdminUser> = {}): AdminUser {
  return {
    id: 2,
    username: 'alice',
    is_admin: false,
    is_active: true,
    created_at: '2026-10-23T00:00:00',
    project_count: 0,
    ...overrides,
  }
}

describe('adminGuard', () => {
  it('登录状态未知时等待', () => {
    expect(adminGuard('unknown', null)).toBe('loading')
  })
  it('未登录跳转登录页', () => {
    expect(adminGuard('anonymous', null)).toBe('login')
  })
  it('普通用户回首页，管理员放行', () => {
    expect(adminGuard('authenticated', normal)).toBe('home')
    expect(adminGuard('authenticated', admin)).toBe('allow')
  })
})

describe('rowActions', () => {
  it('自己的行禁用全部危险操作', () => {
    expect(rowActions(row({ id: 1, is_admin: true }), 1)).toEqual({
      isSelf: true,
      canResetPassword: false,
      canToggleActive: false,
      canToggleAdmin: false,
      canDelete: false,
    })
  })
  it('他人的行全部可操作', () => {
    const a = rowActions(row({ is_active: false }), 1)
    expect(a.isSelf).toBe(false)
    expect([a.canResetPassword, a.canToggleActive, a.canToggleAdmin, a.canDelete]).toEqual([true, true, true, true])
  })
})

describe('canConfirmDelete', () => {
  it('必须与用户名完全一致', () => {
    expect(canConfirmDelete('alice', 'alice')).toBe(true)
    expect(canConfirmDelete('', 'alice')).toBe(false)
    expect(canConfirmDelete('alice ', 'alice')).toBe(false)
    expect(canConfirmDelete('Alice', 'alice')).toBe(false)
  })
})
