import { beforeEach, describe, expect, it, vi } from 'vitest'
import { dramaWrite } from './write'
import { moveShot, shotApi, splitPromptReferences } from './shots'

vi.mock('./write', () => ({ dramaWrite: vi.fn() }))
beforeEach(() => vi.clearAllMocks())

describe('分镜顺序与版本', () => {
  it('移到前面或后面时保留每一条分镜', () => {
    expect(moveShot([2, 6, 8, 1], 6, 1)).toEqual([2, 8, 1, 6])
    expect(moveShot([2, 6, 8, 1], 8, 2)).toEqual([8, 2, 6, 1])
    expect(moveShot([2], 9, 2)).toEqual([2])
  })
  it('排序使用集修订号，修改和删除使用分镜修订号', async () => {
    await shotApi.order(7, 4, [20, 18])
    await shotApi.update(20, 3, { description: '离开。', scene_id: null, look_ids: [] })
    await shotApi.remove(18, 8)
    expect(vi.mocked(dramaWrite).mock.calls).toEqual([
      ['/plugins/drama/episodes/7/shots/order', { method: 'PUT', body: { revision: 4, shot_ids: [20, 18] } }],
      [
        '/plugins/drama/shots/20',
        { method: 'PATCH', body: { revision: 3, description: '离开。', scene_id: null, look_ids: [] } },
      ],
      ['/plugins/drama/shots/18', { method: 'DELETE', query: { revision: 8 } }],
    ])
  })
  it('只着色已引用的名称，优先匹配长名称，保留原文', () => {
    expect(splitPromptReferences('人物@林(夜)与@林；@陌生人', ['林', '林(夜)'])).toEqual([
      { value: '人物', linked: false },
      { value: '@林(夜)', linked: true },
      { value: '与', linked: false },
      { value: '@林', linked: true },
      { value: '；@陌生人', linked: false },
    ])
  })
})
