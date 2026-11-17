import { afterEach, describe, expect, it, vi } from 'vitest'
import { penNamesApi } from './penNames'
import { projectApi } from './projects'

afterEach(() => vi.unstubAllGlobals())

describe('笔名接口', () => {
  it('使用本人私库、修订号和 owner-only 项目绑定接口', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    await penNamesApi.create('山客')
    await penNamesApi.rename(7, '云客', 2)
    await penNamesApi.remove(7, 3)
    await penNamesApi.setProject(9, null)
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      '/api/v1/pen-names', '/api/v1/pen-names/7', '/api/v1/pen-names/7?expected_revision=3', '/api/v1/projects/9/pen-name',
    ])
    expect(JSON.parse(String(fetcher.mock.calls[1][1]?.body))).toEqual({ name: '云客', expected_revision: 2 })
    expect(JSON.parse(String(fetcher.mock.calls[3][1]?.body))).toEqual({ pen_name_id: null })
  })

  it('新建作品直接提交选择的笔名，不追加第二次绑定', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('{"id":9}', { status: 201 }))
    vi.stubGlobal('fetch', fetcher)
    await projectApi.create({ title: '新书', pen_name_id: 7 })
    expect(fetcher).toHaveBeenCalledOnce()
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({ title: '新书', pen_name_id: 7 })
  })
})
