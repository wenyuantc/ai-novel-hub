import { afterEach, describe, expect, it, vi } from 'vitest'
import { collaborationApi } from './collaboration'

afterEach(() => vi.unstubAllGlobals())

describe('协作 API', () => {
  it('按范围、邀请轮次和版本发送，不为系列发送作品权限字段', async () => {
    const calls: { url: string; init: RequestInit }[] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response('{}')
    })
    const signal = new AbortController().signal
    await collaborationApi.invitations(signal)
    await collaborationApi.respond('project', 'uuid-1', 'accept', 3, signal)
    await collaborationApi.respond('series', 'uuid-2', 'decline', 4, signal)
    await collaborationApi.members('project', 8, signal)
    await collaborationApi.inviteProject(8, { username: 'alice', role: 'editor', allow_owner_ai: false }, signal)
    await collaborationApi.inviteSeries(9, 'bob', signal)
    await collaborationApi.updateProject(8, 15, { expected_revision: 5, allow_owner_ai: true }, signal)
    await collaborationApi.revoke('series', 9, 16, 7, signal)
    expect(calls.map(({ url, init }) => [url, init.method])).toEqual([
      ['/api/v1/collaboration/invitations', 'GET'],
      ['/api/v1/collaboration/invitations/project/uuid-1/accept', 'POST'],
      ['/api/v1/collaboration/invitations/series/uuid-2/decline', 'POST'],
      ['/api/v1/projects/8/collaborators', 'GET'],
      ['/api/v1/projects/8/collaborators', 'POST'],
      ['/api/v1/series/9/collaborators', 'POST'],
      ['/api/v1/projects/8/collaborators/15', 'PATCH'],
      ['/api/v1/series/9/collaborators/16?expected_revision=7', 'DELETE'],
    ])
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ expected_revision: 3 })
    expect(JSON.parse(String(calls[5].init.body))).toEqual({ username: 'bob' })
    expect(JSON.parse(String(calls[6].init.body))).toEqual({ expected_revision: 5, allow_owner_ai: true })
    expect(calls.every(({ init }) => init.signal === signal && init.credentials === 'include')).toBe(true)
  })
})
