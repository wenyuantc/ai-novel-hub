import { afterEach, describe, expect, it } from 'vitest'
import { invalidateRequestGeneration } from '../api/client'
import type { User } from '../api/types'
import { useAuthStore } from '../stores/auth'
import { capturePenNameActor } from './usePenNames'

const user = (id: number, authorization_revision = 1): User => ({
  id, username: `作者${id}`, is_admin: false, is_super_admin: false,
  effective_permissions: [], authorization_revision, roles: [], created_at: '',
})

afterEach(() => useAuthStore.setState({ status: 'anonymous', user: null }))

describe('笔名请求身份守卫', () => {
  it('切账号、修订变化和请求代次变化后拒绝旧回调', () => {
    useAuthStore.setState({ status: 'authenticated', user: user(1) })
    const first = capturePenNameActor()
    expect(first()).toBe(true)
    useAuthStore.setState({ user: user(2) })
    expect(first()).toBe(false)
    useAuthStore.setState({ user: user(1) })
    const revision = capturePenNameActor()
    useAuthStore.setState({ user: user(1, 2) })
    expect(revision()).toBe(false)
    const generation = capturePenNameActor()
    invalidateRequestGeneration()
    expect(generation()).toBe(false)
  })
})
