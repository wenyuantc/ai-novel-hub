import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearCreationIntent, createIntentKey, readCreationIntent, sendCreationIntent, storeCreationIntent, type TemplateCreationIntent } from './novelTemplateCreation'

function memoryStorage(): Storage {
  const entries = new Map<string, string>()
  return { get length() { return entries.size }, clear: () => entries.clear(), getItem: (key) => entries.get(key) ?? null,
    key: (index) => [...entries.keys()][index] ?? null, removeItem: (key) => { entries.delete(key) },
    setItem: (key, value) => { entries.set(key, value) } }
}

const intent: TemplateCreationIntent = { version: 1, actor_id: 7, template_id: 12, template_name: '星河模板', request: {
  expected_revision: 3, intent_key: 'a1234567-1111-4111-8111-123456789abc', title: '新故事', pen_name_id: 5,
} }

afterEach(() => vi.unstubAllGlobals())

describe('小说模板创建意图', () => {
  it('保存完整原请求供断网与重新打开后重试，按账号隔离', () => {
    vi.stubGlobal('sessionStorage', memoryStorage())
    expect(storeCreationIntent(intent)).toBe(true)
    expect(readCreationIntent(7)).toEqual(intent)
    expect(readCreationIntent(8)).toBeNull()
    clearCreationIntent(7)
    expect(readCreationIntent(7)).toBeNull()
  })

  it('拒绝损坏或跨账号的意图，不生成新的重试请求', () => {
    const storage = memoryStorage()
    vi.stubGlobal('sessionStorage', storage)
    storage.setItem('ainovel.novel-template-intent.7', JSON.stringify({ ...intent, actor_id: 8 }))
    expect(readCreationIntent(7)).toBeNull()
    storage.setItem('ainovel.novel-template-intent.7', JSON.stringify({ ...intent, request: { ...intent.request, intent_key: 'bad' } }))
    expect(readCreationIntent(7)).toBeNull()
    storage.setItem('ainovel.novel-template-intent.7', JSON.stringify({ ...intent, request: { ...intent.request, unknown_field: true } }))
    expect(readCreationIntent(7)).toBeNull()
  })

  it('兼容旧版无模板名称的意图，名称按 Unicode 字符数校验', () => {
    const storage = memoryStorage()
    vi.stubGlobal('sessionStorage', storage)
    const legacy: TemplateCreationIntent = { version: 1, actor_id: intent.actor_id, template_id: intent.template_id, request: intent.request }
    expect(storeCreationIntent(legacy)).toBe(true)
    expect(readCreationIntent(7)).toEqual(legacy)
    const named = { ...legacy, template_name: '🌟'.repeat(100) }
    expect(storeCreationIntent(named)).toBe(true)
    expect(readCreationIntent(7)).toEqual(named)
    expect(storeCreationIntent({ ...legacy, template_name: '🌟'.repeat(101) })).toBe(true)
    expect(readCreationIntent(7)).toBeNull()
  })

  it('含扩展字符的有效书名在重开后仍能重放，超出 200 个字符才拒绝', () => {
    vi.stubGlobal('sessionStorage', memoryStorage())
    const longTitle = { ...intent, request: { ...intent.request, title: '𠮷'.repeat(200) } }
    expect(storeCreationIntent(longTitle)).toBe(true)
    expect(readCreationIntent(7)).toEqual(longTitle)
    expect(storeCreationIntent({ ...intent, request: { ...intent.request, title: '𠮷'.repeat(201) } })).toBe(true)
    expect(readCreationIntent(7)).toBeNull()
  })

  it('显式覆盖包含清空目标字数时原样保留，未知字段不被重放', () => {
    const storage = memoryStorage()
    vi.stubGlobal('sessionStorage', storage)
    const withOverrides: TemplateCreationIntent = { ...intent, request: { ...intent.request, overrides: { genre: '奇幻', target_words: null, tags: ['冒险'] } } }
    expect(storeCreationIntent(withOverrides)).toBe(true)
    expect(readCreationIntent(7)).toEqual(withOverrides)
    storage.setItem('ainovel.novel-template-intent.7', JSON.stringify({ ...withOverrides, request: { ...withOverrides.request, overrides: { extra: true } } }))
    expect(readCreationIntent(7)).toBeNull()
  })

  it('缺少 randomUUID 时使用安全随机源生成规范 v4', () => {
    vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array) => bytes.fill(0) })
    expect(createIntentKey()).toBe('00000000-0000-4000-8000-000000000000')
  })

  it('网络结果未知后原请求重试，不更换意图；成功后才清理', async () => {
    vi.stubGlobal('sessionStorage', memoryStorage())
    const send = vi.fn().mockRejectedValueOnce(new Error('断网')).mockResolvedValueOnce({ id: 20 })
    await expect(sendCreationIntent(intent, () => send(intent.request), () => true)).rejects.toThrow('断网')
    const retry = readCreationIntent(7)
    expect(retry).toEqual(intent)
    expect(await sendCreationIntent(retry!, () => send(retry!.request), () => true)).toEqual({ result: { id: 20 }, accepted: true })
    expect(send.mock.calls[0][0]).toEqual(send.mock.calls[1][0])
    expect(readCreationIntent(7)).toBeNull()
  })

  it('切换账号后迟到的成功不能清掉原意图', async () => {
    vi.stubGlobal('sessionStorage', memoryStorage())
    let release!: (value: { id: number }) => void
    let current = true
    const pending = sendCreationIntent(intent, () => new Promise((resolve) => { release = resolve }), () => current)
    current = false
    release({ id: 20 })
    expect((await pending).accepted).toBe(false)
    expect(readCreationIntent(7)).toEqual(intent)
  })

  it('旧请求迟到的成功不能清掉同账号新创建的意图', async () => {
    vi.stubGlobal('sessionStorage', memoryStorage())
    let release!: (value: { id: number }) => void
    const pending = sendCreationIntent(intent, () => new Promise((resolve) => { release = resolve }), () => true)
    const next: TemplateCreationIntent = { ...intent, request: { ...intent.request,
      intent_key: 'b1234567-1111-4111-8111-123456789abc', title: '另一本故事' } }
    expect(storeCreationIntent(next)).toBe(true)
    release({ id: 20 })
    expect((await pending).accepted).toBe(false)
    expect(readCreationIntent(7)).toEqual(next)
    expect(clearCreationIntent(7, intent.request.intent_key)).toBe(false)
    const send = vi.fn()
    await expect(sendCreationIntent(intent, send, () => true)).rejects.toThrow('创建请求已更新')
    expect(send).not.toHaveBeenCalled()
    expect(readCreationIntent(7)).toEqual(next)
  })
})
