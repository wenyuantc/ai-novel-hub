import { afterEach, describe, expect, it, vi } from 'vitest'
import { taskApi } from './tasks'
import { runWizard } from './wizard'
import type { Task, WizardRequest } from './types'

const key = '00000000-0000-4000-8000-000000000001'
const nextKey = '00000000-0000-4000-8000-000000000002'
const task = { id: 7, task_key: key } as Task
afterEach(() => vi.unstubAllGlobals())

describe('后台任务操作绑定原实例', () => {
  it('所有单任务入口都携带最初 task_key，拒绝缺身份', async () => {
    const fetch = vi.fn(async (url: string, init: RequestInit) => {
      if (init.method === 'DELETE') return new Response(null, { status: 204 })
      if (url.includes('/output')) return new Response('{"text":"原输出"}')
      if (url.includes('/retry')) return new Response(JSON.stringify({ ...task, id: 8, task_key: nextKey }))
      return new Response(JSON.stringify(task))
    })
    vi.stubGlobal('fetch', fetch)
    await taskApi.get(task); await taskApi.output(task); await taskApi.cancel(task)
    await taskApi.retry(task); await taskApi.resume(task); await taskApi.finish(task)
    await taskApi.remove(task); await taskApi.skipBookImport(task)
    expect(fetch).toHaveBeenCalledTimes(8)
    for (const [url] of fetch.mock.calls) expect(new URL(url, 'https://local.test').searchParams.get('task_key')).toBe(key)
    const missing = { id: 7, task_key: null }
    await expect(taskApi.get(missing)).rejects.toThrow('身份')
    await expect(taskApi.output(missing)).rejects.toThrow('身份')
    await expect(taskApi.cancel(missing)).rejects.toThrow('身份')
    await expect(taskApi.retry(missing)).rejects.toThrow('身份')
    await expect(taskApi.resume(missing)).rejects.toThrow('身份')
    await expect(taskApi.finish(missing)).rejects.toThrow('身份')
    await expect(taskApi.remove(missing)).rejects.toThrow('身份')
    await expect(taskApi.skipBookImport(missing)).rejects.toThrow('身份')
    expect(fetch).toHaveBeenCalledTimes(8)
  })

  it('旧 ID 指向新实例时拒绝响应；提案身份不匹配时本地拒发', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ ...task, task_key: nextKey })))
    vi.stubGlobal('fetch', fetch)
    await expect(taskApi.get(task)).rejects.toMatchObject({ status: 409 })
    await expect(taskApi.cancel(task)).rejects.toMatchObject({ status: 409 })
    await expect(taskApi.retry(task)).resolves.toMatchObject({ task_key: nextKey })
    const review = { task_key: nextKey, run_key: 'run', token: 'token' }
    await expect(taskApi.finish(task, review)).rejects.toThrow('身份')
    await expect(taskApi.remove(task, review)).rejects.toThrow('身份')
    expect(fetch).toHaveBeenCalledTimes(3)
  })

  it('后端 404 不重放；重试响应不能沿用原任务 key', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response('{"detail":"不存在"}', { status: 404 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(task)))
    vi.stubGlobal('fetch', fetch)
    await expect(taskApi.cancel(task)).rejects.toMatchObject({ status: 404 })
    await expect(taskApi.retry(task)).rejects.toMatchObject({ status: 409 })
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('向导续写必须成对发送用户已见原任务 ID 与 key，缺一个都拒绝', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    await expect(runWizard(2, 'world', { continue_from_task_id: 7 } as WizardRequest)).rejects.toThrow('先前任务身份')
    await expect(runWizard(2, 'world', { continue_from_task_key: key } as WizardRequest)).rejects.toThrow('先前任务身份')
    expect(fetch).not.toHaveBeenCalled()
    fetch.mockResolvedValueOnce(new Response(JSON.stringify({ ...task, id: 8, task_key: nextKey }), { status: 202 }))
    const result = await runWizard(2, 'world', { continue_from_task_id: 7, continue_from_task_key: key })
    expect(result).toMatchObject({ id: 8, task_key: nextKey })
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ continue_from_task_id: 7, continue_from_task_key: key })
    expect(fetch).toHaveBeenCalledOnce()
  })

  it('向导原任务已不可用时仅报告 400，不换用同 ID 新任务或重发', async () => {
    const fetch = vi.fn(async () => new Response('{"detail":"来源不可用"}', { status: 400 }))
    vi.stubGlobal('fetch', fetch)
    await expect(runWizard(2, 'world', { continue_from_task_id: 7, continue_from_task_key: key })).rejects.toMatchObject({ status: 400 })
    expect(fetch).toHaveBeenCalledOnce()
  })
})
