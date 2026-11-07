import { afterEach, describe, expect, it, vi } from 'vitest'
import { seriesApi } from './series'

afterEach(() => vi.unstubAllGlobals())

describe('系列 API', () => {
  it('按归属和预检传送不同身份；所有版本变更显式传送期望版本', async () => {
    const calls: { url: string; init: RequestInit }[] = []
    vi.stubGlobal('fetch', async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response('{}')
    })
    const signal = new AbortController().signal
    await seriesApi.entries(4, { page: 2, page_size: 20, q: '青 云', kind: 'character' }, signal)
    await seriesApi.impact(4, { action: 'promote', project_id: 12, kind: 'character', entry_id: 19 }, signal)
    await seriesApi.promote(4, {
      project_id: 12, ref: { kind: 'character', id: 19 }, source_stamp: 'a'.repeat(64), expected_revision: 3,
      ai_settings: { ai_mode: 'detected', mention_enabled: true },
    })
    await seriesApi.use(4, 35, 12, 4)
    await seriesApi.unuse(4, 35, 12, 5)
    await seriesApi.move(4, 35, 6, 6)
    await seriesApi.removeEntry(4, 35, 7)

    expect(calls.map(({ url, init }) => [url, init.method])).toEqual([
      ['/api/v1/series/4/entries?page=2&page_size=20&q=%E9%9D%92+%E4%BA%91&kind=character', 'GET'],
      ['/api/v1/series/4/impact?action=promote&project_id=12&kind=character&entry_id=19', 'GET'],
      ['/api/v1/series/4/entries/promote', 'POST'],
      ['/api/v1/series/4/entries/35/uses', 'POST'],
      ['/api/v1/series/4/entries/35/uses/12?expected_revision=5', 'DELETE'],
      ['/api/v1/series/4/entries/35/move-to-book', 'POST'],
      ['/api/v1/series/4/entries/35?expected_revision=7', 'DELETE'],
    ])
    expect(calls[0].init.signal).toBe(signal)
    expect(calls[1].init.signal).toBe(signal)
    expect(JSON.parse(String(calls[2].init.body))).toMatchObject({
      project_id: 12, ref: { kind: 'character', id: 19 }, expected_revision: 3,
    })
    expect(JSON.parse(String(calls[3].init.body))).toEqual({ project_id: 12, expected_revision: 4 })
    expect(JSON.parse(String(calls[5].init.body))).toEqual({ project_id: 6, expected_revision: 6 })
    expect(calls.every(({ init }) => init.credentials === 'include')).toBe(true)
  })
})
