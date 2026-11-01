import { describe, expect, it } from 'vitest'
import { projectAccess, canConfirmProjectDelete } from './projectAccess'

const admin = { id: 1, is_admin: true, is_super_admin: true, effective_permissions: [], authorization_revision: 1, roles: [] }
const owner = { id: 2, is_admin: false, is_super_admin: false, effective_permissions: [], authorization_revision: 1, roles: [] }
const project = { id: 8, owner: { id: 2, username: 'alice' } }

describe('项目管理能力', () => {
  it('管理员管理他人项目只启用人工管理，自有项目继续正常写作', () => {
    expect(projectAccess(project, admin, 8)).toMatchObject({ ready: true, managingOther: true, canUseAI: false })
    expect(projectAccess({ ...project, owner: { id: 1, username: 'root' } }, admin, 8)).toMatchObject({ ready: true, managingOther: false, canUseAI: true })
    expect(projectAccess(project, owner, 8)).toMatchObject({ ready: true, managingOther: false, canUseAI: true })
  })

  it('加载、退出或切换项目时不复用旧项目的能力；普通用户不能获得跨用户能力', () => {
    expect(projectAccess(null, admin, 8).canUseAI).toBe(false)
    expect(projectAccess(project, null, 8).canUseAI).toBe(false)
    expect(projectAccess(project, admin, 9)).toMatchObject({ ready: false, managingOther: false, canUseAI: false })
    expect(projectAccess(project, { id: 3, is_super_admin: false, effective_permissions: [] }, 8)).toMatchObject({ ready: true, managingOther: true, canRead: false, canEdit: false, canRecycle: false, canUseAI: false })
  })

  it('兼容原有自有项目响应，删除名称必须准确输入', () => {
    expect(projectAccess({ id: 8 }, owner, 8).canUseAI).toBe(true)
    expect(canConfirmProjectDelete('星河', '星河')).toBe(true)
    expect(canConfirmProjectDelete('', '星河')).toBe(false)
    expect(canConfirmProjectDelete('星河 ', '星河')).toBe(false)
  })
})
