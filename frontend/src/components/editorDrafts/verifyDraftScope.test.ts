import { beforeEach, describe, expect, it, vi } from 'vitest'
import { authApi } from '../../api/auth'
import { ApiError } from '../../api/client'
import type { User } from '../../api/types'
import { verifyProjectScope } from '../../stores/project'
import { verifyDraftScope } from './verifyDraftScope'

const identity = vi.hoisted(() => ({
  clear: vi.fn(),
  applyAuthorization: vi.fn(),
  user: {
    id: 7, username: '作者', created_at: '2026-11-17T01:02:03.123456Z',
    authorization_revision: 1, effective_permissions: [], roles: [], is_admin: false, is_super_admin: false,
  } satisfies User,
}))

vi.mock('../../stores/auth', () => ({
  useAuthStore: { getState: () => identity },
}))
vi.mock('../../stores/project', () => ({
  isProjectScopeCurrent: () => true,
  verifyProjectScope: vi.fn(async () => 'confirmed'),
}))
vi.mock('../../api/auth', () => ({ authApi: { me: vi.fn() } }))

const scope = { actorId: 7, projectId: 3, version: 1, identity: '当前授权' }
const me = vi.mocked(authApi.me)
beforeEach(() => vi.clearAllMocks())

describe('本地草稿展示的最新账号校验', () => {
  it('同数字 ID 的不同账号代次也不能展示旧记录', async () => {
    me.mockResolvedValueOnce({ ...identity.user, created_at: '2026-11-17T01:02:03.123457Z' })
    expect(await verifyDraftScope(scope, () => true)).toBe(false)
    expect(identity.clear).toHaveBeenCalledOnce()
    expect(verifyProjectScope).not.toHaveBeenCalled()
  })

  it('页面离开或者回调失效后，旧账号差异不清除新会话', async () => {
    let current = true
    me.mockImplementationOnce(async () => {
      current = false
      return { ...identity.user, id: 8 }
    })
    expect(await verifyDraftScope(scope, () => current)).toBe(false)
    expect(identity.clear).not.toHaveBeenCalled()
  })

  it('临时网络错误不丢失登录，恢复网络后还需作品权威确认', async () => {
    me.mockRejectedValueOnce(new ApiError(0, '无法连接'))
    expect(await verifyDraftScope(scope, () => true)).toBe(false)
    expect(identity.clear).not.toHaveBeenCalled()
    me.mockResolvedValueOnce(identity.user)
    expect(await verifyDraftScope(scope, () => true)).toBe(true)
    expect(verifyProjectScope).toHaveBeenCalledExactlyOnceWith(scope)
  })
})
