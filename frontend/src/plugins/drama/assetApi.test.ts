import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useAuthStore } from '../../stores/auth'
import { dramaAssetApi } from './assetApi'

const fetchMock = vi.fn(async () => new Response('{}'))
const actor = {
  id: 7,
  username: '作者',
  is_admin: false,
  is_super_admin: false,
  effective_permissions: [],
  authorization_revision: 1,
  roles: [],
  created_at: '2026-11-17T12:00:00.123456Z',
}
beforeEach(() => useAuthStore.setState({ status: 'authenticated', user: actor }))
afterEach(() => {
  useAuthStore.setState({ status: 'anonymous', user: null })
  vi.unstubAllGlobals()
  fetchMock.mockClear()
})

it('给已有角色新增造型，修改资产和移出本集分别使用资产、剧集的修订号', async () => {
  vi.stubGlobal('fetch', fetchMock)
  await dramaAssetApi.create('looks', 42, { name: '晚宴' })
  await dramaAssetApi.patch('looks', 10, { image_prompt: '立绘', revision: 3 })
  await dramaAssetApi.membership(4, 'looks', 10, 7, false)
  expect(fetchMock).toHaveBeenNthCalledWith(
    1,
    '/api/v1/plugins/drama/characters/42/looks',
    expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({ 'X-Editor-Actor-Id': '7', 'X-Editor-Actor-Created-At': actor.created_at }),
    }),
  )
  expect(fetchMock).toHaveBeenNthCalledWith(
    2,
    '/api/v1/plugins/drama/looks/10',
    expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ image_prompt: '立绘', revision: 3 }) }),
  )
  expect(fetchMock).toHaveBeenNthCalledWith(
    3,
    '/api/v1/plugins/drama/episodes/4/assets/looks/10?revision=7',
    expect.objectContaining({ method: 'DELETE' }),
  )
})

it('集内新建随同剧集修订号原子绑定，素材库新建不带剧集', async () => {
  vi.stubGlobal('fetch', fetchMock)
  await dramaAssetApi.create('scenes', 2, { location: '花园' }, { id: 4, revision: 7 })
  await dramaAssetApi.create('characters', 2, { name: '小林' })
  expect(fetchMock).toHaveBeenNthCalledWith(
    1,
    '/api/v1/plugins/drama/series/2/scenes?episode_id=4&episode_revision=7',
    expect.objectContaining({ method: 'POST' }),
  )
  expect(fetchMock).toHaveBeenNthCalledWith(
    2,
    '/api/v1/plugins/drama/series/2/characters',
    expect.objectContaining({ method: 'POST' }),
  )
})
