import { afterEach, describe, expect, it, vi } from 'vitest'
import { promptComponentApi } from './promptComponents'
import { classifySaveError } from '../utils/promptComponents'

afterEach(() => vi.unstubAllGlobals())

describe('提示词组件 API', () => {
  it('列表 / 搜索 / 详情 / 新建 / 带修订号更新 / 删除 / 导入导出的方法与路径', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response(init.method === 'DELETE' ? null : '{}', { status: init.method === 'DELETE' ? 204 : 200 })
    })
    await promptComponentApi.list()
    await promptComponentApi.list('  禁用词  ')
    await promptComponentApi.get(3)
    await promptComponentApi.create({ slug: 'banned', name: '禁用词', content: '不要写「然而」' })
    await promptComponentApi.update(3, { slug: 'banned', name: '禁用词', description: '', content: '新', revision: 4 })
    await promptComponentApi.remove(3)
    await promptComponentApi.export()
    await promptComponentApi.import({ format: 'ainovel.prompt-components', version: 1, items: [] })
    expect(calls.map(({ url, init }) => [init.method ?? 'GET', url])).toEqual([
      ['GET', '/api/v1/prompt-components'],
      ['GET', '/api/v1/prompt-components?q=%E7%A6%81%E7%94%A8%E8%AF%8D'],
      ['GET', '/api/v1/prompt-components/3'],
      ['POST', '/api/v1/prompt-components'],
      ['PUT', '/api/v1/prompt-components/3'],
      ['DELETE', '/api/v1/prompt-components/3'],
      ['GET', '/api/v1/prompt-components/export'],
      ['POST', '/api/v1/prompt-components/import'],
    ])
    expect(JSON.parse(calls[4].init.body as string)).toEqual({ slug: 'banned', name: '禁用词', description: '', content: '新', revision: 4 })
    expect(JSON.parse(calls[7].init.body as string)).toEqual({ data: { format: 'ainovel.prompt-components', version: 1, items: [] } })
  })

  it('把后端结构化错误识别为修订冲突 / 被引用 / 影响覆盖，其他错误不识别，且不自动重试', async () => {
    const respond = (status: number, body: unknown) => vi.fn(async () => new Response(JSON.stringify(body), { status }))
    const save = () => promptComponentApi.update(1, { slug: 'a', name: 'A', content: '', revision: 1 }).catch((e: unknown) => e)

    let fetch = respond(409, { detail: '组件已被修改', code: 'stale_revision' })
    vi.stubGlobal('fetch', fetch)
    expect(classifySaveError(await save())).toEqual({ kind: 'stale' })
    expect(fetch).toHaveBeenCalledTimes(1)

    fetch = respond(409, {
      detail: '组件 a 正被 Skill「续写」引用，不能修改标识',
      code: 'component_in_use',
      usages: [
        { kind: 'skill', id: 5, key: null, name: '续写', enabled: null },
        { kind: 'override', id: null, key: 'chapter.generate', name: '写章节', enabled: false },
      ],
    })
    vi.stubGlobal('fetch', fetch)
    expect(classifySaveError(await save())).toEqual({
      kind: 'in_use',
      detail: '组件 a 正被 Skill「续写」引用，不能修改标识',
      usages: [
        { kind: 'skill', id: 5, key: null, name: '续写', enabled: null },
        { kind: 'override', id: null, key: 'chapter.generate', name: '写章节', enabled: false },
      ],
    })

    vi.stubGlobal('fetch', respond(400, {
      detail: '修改组件 a 会让引用它的模板覆盖校验失败',
      code: 'component_breaks_usages',
      failures: [{ key: 'chapter.generate', name: '写章节', errors: [{ part: 'user', line: 2, message: '组件 a：未知变量：x' }] }],
    }))
    expect(classifySaveError(await save())).toEqual({
      kind: 'breaks',
      failures: [{ key: 'chapter.generate', name: '写章节', errors: [{ part: 'user', line: 2, message: '组件 a：未知变量：x' }] }],
    })

    vi.stubGlobal('fetch', respond(409, { detail: '标识已被使用，请换一个' }))
    expect(classifySaveError(await save())).toBeNull()
    vi.stubGlobal('fetch', respond(400, { detail: '组件内不能引用其他组件' }))
    expect(classifySaveError(await save())).toBeNull()
  })
})
