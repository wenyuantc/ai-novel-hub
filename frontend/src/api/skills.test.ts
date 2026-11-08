import { afterEach, describe, expect, it, vi } from 'vitest'
import { skillApi } from './skills'

afterEach(() => vi.unstubAllGlobals())

describe('Skill 预览客户端', () => {
  it('POST /skills/{id}/preview，请求体为 {inputs}，返回填入后的正文', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response(JSON.stringify({ body: '基调：沉重', missing_required: ['who'], errors: [] }), { status: 200 })
    })
    const result = await skillApi.preview(5, { tone: '沉重' })
    expect(calls.map(({ url, init }) => [init.method, url])).toEqual([['POST', '/api/v1/skills/5/preview']])
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ inputs: { tone: '沉重' } })
    expect(result).toEqual({ body: '基调：沉重', missing_required: ['who'], errors: [] })
  })
})
