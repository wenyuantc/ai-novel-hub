import { afterEach, describe, expect, it, vi } from 'vitest'
import { annotationEvidence } from './annotations'

describe('批注选区校验', () => {
  afterEach(() => vi.unstubAllGlobals())
  it('HTTP 缺少摘要 API 时提交包含原始换行和表情的完整选区', async () => {
    vi.stubGlobal('crypto', { getRandomValues: vi.fn() })
    expect(await annotationEvidence('🙂\r\n原句')).toEqual({ selected_text: '🙂\r\n原句' })
  })
  it('支持 Web Crypto 时继续只提交 SHA-256', async () => {
    expect(await annotationEvidence('abc')).toEqual({
      selection_hash: 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    })
  })
})
