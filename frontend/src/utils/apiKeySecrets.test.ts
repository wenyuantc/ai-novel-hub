import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApiKeySecretSession, writeKeyToClipboard } from './apiKeySecrets'

function deferred<T>() {
  let resolve: (value: T) => void = () => undefined
  let reject: (error: unknown) => void = () => undefined
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

afterEach(() => vi.unstubAllGlobals())

describe('密钥显隐与复制会话', () => {
  it('隐藏、离开或撤销后，迟到的 reveal 不再显示或写入剪贴板', async () => {
    const pending = deferred<string>()
    const copy = vi.fn(async () => true)
    const session = createApiKeySecretSession(() => pending.promise, copy)
    const result = session.run('copy')
    session.cancel()
    pending.resolve('anv_test-secret')
    await expect(result).resolves.toBeNull()
    expect(copy).not.toHaveBeenCalled()
  })

  it('第二次操作使前一次请求失效，且取消后仍可重新显示', async () => {
    const old = deferred<string>()
    const reveal = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValueOnce('anv_new')
    const session = createApiKeySecretSession(reveal)
    const first = session.run('show')
    const second = await session.run('show')
    old.resolve('anv_old')
    await expect(first).resolves.toBeNull()
    expect(second).toMatchObject({ kind: 'visible', key: 'anv_new' })
    expect(second && session.isCurrent(second)).toBe(true)
    session.cancel()
    expect(second && session.isCurrent(second)).toBe(false)
  })

  it('隐藏状态可直接取完整 Key 复制，成功结果不保留明文', async () => {
    const copy = vi.fn(async () => true)
    const session = createApiKeySecretSession(async () => 'anv_full-secret', copy)
    const result = await session.run('copy')
    expect(copy).toHaveBeenCalledWith('anv_full-secret')
    expect(result).toMatchObject({ kind: 'copied' })
    expect(result).not.toHaveProperty('key')
  })

  it('浏览器不允许自动复制时返回手动复制需要的完整 Key', async () => {
    const session = createApiKeySecretSession(async () => 'anv_full-secret', async () => false)
    await expect(session.run('copy')).resolves.toMatchObject({ kind: 'manual', key: 'anv_full-secret' })
  })

  it('已显示或手动复制弹窗已有完整 Key 时不重复请求 reveal', async () => {
    const reveal = vi.fn(async () => 'unused')
    const session = createApiKeySecretSession(reveal, async () => true)
    await expect(session.run('copy', 'anv_known')).resolves.toMatchObject({ kind: 'copied' })
    expect(reveal).not.toHaveBeenCalled()
  })

  it('取消导致的网络中止不会冒充用户操作错误，真实错误仍会透传', async () => {
    const pending = deferred<string>()
    const session = createApiKeySecretSession(() => pending.promise)
    const result = session.run('show')
    session.cancel()
    pending.reject(new DOMException('cancelled', 'AbortError'))
    await expect(result).resolves.toBeNull()
    await expect(createApiKeySecretSession(async () => { throw new Error('密钥已撤销') }).run('show')).rejects.toThrow('密钥已撤销')
  })
})

describe('受限剪贴板', () => {
  it('不存在 Clipboard API 时保留手动复制路径', async () => {
    vi.stubGlobal('navigator', {})
    await expect(writeKeyToClipboard('anv_secret')).resolves.toBe(false)
  })

  it('NotAllowedError 不会丢失手动复制路径', async () => {
    vi.stubGlobal('navigator', { clipboard: { writeText: async () => { throw new DOMException('denied', 'NotAllowedError') } } })
    await expect(writeKeyToClipboard('anv_secret')).resolves.toBe(false)
  })
})
