import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { isAbortError } from './sse'
import {
  canRetryTask,
  isActiveTask,
  isOpenTask,
  queueLabel,
  TASK_STATUS,
  reconnectDelay,
  subscribeTask,
  TASK_KIND_LABELS,
  taskApi,
  isChapterWriteKind,
  taskLink,
  wizardTaskKind,
  type TaskStreamHandlers,
} from './tasks'
import type { Task } from './types'

function task(over: Partial<Task> = {}): Task {
  return {
    id: 1,
    kind: 'chapter.generate',
    status: 'running',
    title: '第 1 章 · AI 生成',
    project_id: 2,
    chapter_id: 3,
    progress: 10,
    stage: '生成中',
    error: '',
    retry_of_id: null,
    character_id: null,
    outline_id: null,
    analysis_id: null,
    checkpoint: null,
    step_cursor: 0,
    queue_position: null,
    created_at: '2026-10-23T08:00:00',
    started_at: null,
    finished_at: null,
    ...over,
  }
}

function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } })
}

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

/** 记录所有回调，模拟 useTaskStream 的文本累积（snapshot 覆盖、delta 追加） */
function recorder() {
  const log: string[] = []
  let text = ''
  const handlers: TaskStreamHandlers = {
    onSnapshot: (t, s) => {
      text = s
      log.push(`snapshot:${t?.status ?? 'null'}`)
    },
    onDelta: (d) => {
      text += d
    },
    onContinue: (e) => log.push(`continue:${e.round}`),
    onProgress: (p) => log.push(`progress:${p.status}:${p.progress}:${p.queue_position ?? '-'}`),
    onReconnect: (n, ms) => log.push(`reconnect:${n}:${ms}`),
  }
  return { handlers, log, text: () => text }
}

const noSleep = async () => {}

describe('subscribeTask', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('GET 订阅：snapshot + delta + continue + progress，done 返回 result', async () => {
    const fetchMock = vi.fn(async () =>
      sseResponse([
        ': stream-start\n\n',
        frame('snapshot', { task: task({ status: 'queued', queue_position: 1 }), text: '' }),
        frame('progress', { status: 'queued', progress: 0, stage: '排队中', queue_position: 0 }),
        frame('progress', { status: 'running', progress: 5, stage: '生成中' }),
        frame('delta', { text: '首轮' }),
        frame('continue', { round: 1, words: 2, target: 3000 }),
        ': ping\n\n',
        frame('delta', { text: '\n\n续写' }),
        frame('done', { task: task({ status: 'succeeded', progress: 100 }), result: { chapter: { id: 3 } } }),
      ]),
    )
    vi.stubGlobal('fetch', fetchMock)
    const r = recorder()
    const end = await subscribeTask(1, r.handlers)
    expect(end).toEqual({
      status: 'succeeded',
      task: task({ status: 'succeeded', progress: 100 }),
      result: { chapter: { id: 3 } },
    })
    expect(r.text()).toBe('首轮\n\n续写')
    expect(r.log).toEqual(['snapshot:queued', 'progress:queued:0:0', 'progress:running:5:-', 'continue:1'])
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('/api/v1/tasks/1/stream')
    expect(init.method).toBe('GET')
    expect(init.credentials).toBe('include')
  })

  it('断线后退避重连，重连 snapshot 覆盖本地文本（不重复）', async () => {
    const responses = [
      () => sseResponse([frame('snapshot', { task: task(), text: '' }), frame('delta', { text: 'ab' })]),
      () => {
        throw new TypeError('network down')
      },
      () =>
        sseResponse([
          frame('snapshot', { task: task(), text: 'abcd' }),
          frame('delta', { text: 'e' }),
          frame('done', { task: task({ status: 'succeeded' }), result: {} }),
        ]),
    ]
    let i = 0
    vi.stubGlobal('fetch', async () => responses[i++]())
    const r = recorder()
    const sleeps: number[] = []
    const end = await subscribeTask(1, r.handlers, undefined, {
      sleep: async (ms) => {
        sleeps.push(ms)
      },
    })
    expect(end.status).toBe('succeeded')
    expect(r.text()).toBe('abcde')
    expect(i).toBe(3)
    // 第一次断开（流结束无终止事件）→ 1s；随后网络错误 → 2s
    expect(sleeps).toEqual([1000, 2000])
    expect(r.log.filter((l) => l.startsWith('reconnect'))).toEqual(['reconnect:1:1000', 'reconnect:2:2000'])
  })

  it('error 事件按 task.status 区分 failed / cancelled / interrupted；task 为 null 时容忍', async () => {
    const cases: Array<[Task | null, string]> = [
      [task({ status: 'failed' }), 'failed'],
      [task({ status: 'cancelled' }), 'cancelled'],
      [task({ status: 'interrupted' }), 'interrupted'],
      [null, 'failed'],
    ]
    for (const [t, expected] of cases) {
      vi.stubGlobal('fetch', async () =>
        sseResponse([frame('snapshot', { task: t, text: '部分' }), frame('error', { task: t, detail: '模型调用失败：额度不足' })]),
      )
      const end = await subscribeTask(1, {}, undefined, { sleep: noSleep })
      expect(end).toEqual({ status: expected, task: t, detail: '模型调用失败：额度不足' })
    }
  })

  it('批量事件：reset 清空文本、chapter_start / chapter_end / stage 回调，paused 作为终止事件返回 result', async () => {
    const item = {
      chapter_id: 11,
      chapter_no: 5,
      title: '风起',
      state: 'done',
      target_words: 3000,
      words_source: 'plan',
      words: 3100,
      analysis: 'ok',
      warning: '',
      error: '',
    }
    const summary = { total: 2, done: 1, skipped_written: 0, skipped_missing: 0, failed: [], analysis_failed: [], remaining: 1, warnings: [] }
    vi.stubGlobal('fetch', async () =>
      sseResponse([
        frame('snapshot', { task: task({ kind: 'chapter.batch', chapter_id: null }), text: '上一章残留' }),
        frame('reset', {}),
        frame('chapter_start', { index: 0, total: 2, chapter_id: 11, chapter_no: 5, title: '风起', target_words: 3000 }),
        frame('stage', { stage: '第 1/2 章 · 生成中' }),
        frame('delta', { text: '正文' }),
        frame('chapter_end', { index: 0, item }),
        frame('chapter_end', { index: 1 }),
        frame('paused', { task: task({ kind: 'chapter.batch', status: 'paused', chapter_id: null }), result: summary }),
        frame('delta', { text: '不应出现' }),
      ]),
    )
    const r = recorder()
    const log: string[] = []
    let resets = 0
    const end = await subscribeTask(1, {
      ...r.handlers,
      onReset: () => {
        resets += 1
        r.handlers.onSnapshot?.(null, '')
      },
      onStage: (st) => log.push(`stage:${st}`),
      onChapterStart: (e) => log.push(`start:${e.index}:${e.chapter_no}`),
      onChapterEnd: (e) => log.push(`end:${e.index}:${e.item.state}`),
    })
    expect(resets).toBe(1)
    expect(r.text()).toBe('正文')
    expect(log).toEqual(['start:0:5', 'stage:第 1/2 章 · 生成中', 'end:0:done'])
    expect(end).toEqual({
      status: 'paused',
      task: task({ kind: 'chapter.batch', status: 'paused', chapter_id: null }),
      result: summary,
    })
  })

  it('404 不重连，直接抛 ApiError', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ detail: '任务不存在' }), { status: 404 }))
    vi.stubGlobal('fetch', fetchMock)
    const err = await subscribeTask(9, {}, undefined, { sleep: noSleep }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).detail).toBe('任务不存在')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('连续失败超过上限后抛出最后一次错误', async () => {
    const fetchMock = vi.fn(async () => new Response('', { status: 502 }))
    vi.stubGlobal('fetch', fetchMock)
    const err = await subscribeTask(1, {}, undefined, { maxRetries: 2, sleep: noSleep }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(502)
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('「连上即断」反复循环：snapshot 不清零失败计数，继续退避并最终放弃', async () => {
    const fetchMock = vi.fn(async () => sseResponse([frame('snapshot', { task: task(), text: '' })]))
    vi.stubGlobal('fetch', fetchMock)
    const sleeps: number[] = []
    const err = await subscribeTask(1, {}, undefined, {
      maxRetries: 3,
      now: () => 0,
      sleep: async (ms) => {
        sleeps.push(ms)
      },
    }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(Error)
    expect((err as Error).message).toBe('连接意外中断')
    expect(fetchMock).toHaveBeenCalledTimes(4)
    expect(sleeps).toEqual([1000, 2000, 4000])
  })

  it('连接稳定存活满 stableMs 后断开：失败计数清零', async () => {
    let i = 0
    vi.stubGlobal('fetch', async () => {
      i += 1
      return i < 4
        ? sseResponse([frame('snapshot', { task: task(), text: '' })])
        : sseResponse([frame('snapshot', { task: task(), text: '' }), frame('done', { task: task(), result: {} })])
    })
    let clock = 0
    const sleeps: number[] = []
    const end = await subscribeTask(1, {}, undefined, {
      maxRetries: 1,
      stableMs: 5000,
      // 每次读取时间前进 6 秒：snapshot 与断开之间相隔 6 秒，视为稳定连接
      now: () => (clock += 6000),
      sleep: async (ms) => {
        sleeps.push(ms)
      },
    })
    expect(end.status).toBe('succeeded')
    expect(sleeps).toEqual([1000, 1000, 1000])
  })

  it('中止 signal 抛 AbortError（等待重连期间中止也一样）', async () => {
    vi.stubGlobal('fetch', async () => sseResponse([frame('snapshot', { task: task(), text: '' })]))
    const controller = new AbortController()
    const p = subscribeTask(1, { onReconnect: () => controller.abort() }, controller.signal, { baseDelayMs: 10_000 })
    const err = await p.catch((e: unknown) => e)
    expect(isAbortError(err)).toBe(true)
  })
})

describe('task helpers', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('reconnectDelay 指数退避并封顶', () => {
    expect([1, 2, 3, 4, 5, 6].map((n) => reconnectDelay(n))).toEqual([1000, 2000, 4000, 8000, 15000, 15000])
  })

  it('queueLabel', () => {
    expect(queueLabel({ status: 'queued', queue_position: 2 })).toBe('排队中，前面还有 2 个任务')
    expect(queueLabel({ status: 'queued', queue_position: 0 })).toBe('排队中，下一个执行')
    expect(queueLabel({ status: 'queued', queue_position: null })).toBe('排队中')
    expect(queueLabel({ status: 'running', queue_position: null })).toBe('')
  })

  it('taskLink：章节任务到编辑器，向导任务到向导页对应步骤', () => {
    expect(taskLink(task())).toBe('/projects/2/chapters/3')
    expect(taskLink(task({ chapter_id: null }))).toBeNull()
    expect(taskLink(task({ kind: 'wizard.characters', chapter_id: null }))).toBe('/projects/2/wizard?kind=characters')
    expect(taskLink(task({ project_id: null }))).toBeNull()
  })

  it('taskLink：建索引到知识库页', () => {
    expect(taskLink(task({ kind: 'kb.index', chapter_id: null }))).toBe('/projects/2/knowledge')
  })

  it('taskLink：大纲续写 / 展开到大纲页', () => {
    expect(taskLink(task({ kind: 'outline.continue', chapter_id: null }))).toBe('/projects/2/outlines')
    expect(taskLink(task({ kind: 'outline.expand', chapter_id: null, outline_id: 5 }))).toBe('/projects/2/outlines')
    expect(TASK_KIND_LABELS['outline.expand']).toBe('AI 展开细纲')
  })

  it('taskLink：设定类任务到角色 / 组织页', () => {
    expect(taskLink(task({ kind: 'character.generate', chapter_id: null }))).toBe('/projects/2/characters')
    expect(taskLink(task({ kind: 'relations.parse', chapter_id: null }))).toBe('/projects/2/characters')
    expect(taskLink(task({ kind: 'organization.generate', chapter_id: null }))).toBe('/projects/2/organizations')
  })

  it('taskLink / 标签：职业生成到职业页，职业体系向导到向导页对应步骤', () => {
    expect(taskLink(task({ kind: 'career.generate', chapter_id: null }))).toBe('/projects/2/careers')
    expect(taskLink(task({ kind: 'wizard.careers', chapter_id: null }))).toBe('/projects/2/wizard?kind=careers')
    expect(TASK_KIND_LABELS['career.generate']).toBe('AI 生成职业')
    expect(TASK_KIND_LABELS['wizard.careers']).toBe('职业体系向导')
    expect(wizardTaskKind('careers')).toBe('wizard.careers')
  })

  it('taskLink / 标签：章节分析与按建议重写到编辑器并打开分析面板', () => {
    expect(taskLink(task({ kind: 'chapter.analyze' }))).toBe('/projects/2/chapters/3?panel=analysis')
    expect(taskLink(task({ kind: 'chapter.revise' }))).toBe('/projects/2/chapters/3?panel=analysis')
    expect(taskLink(task({ kind: 'chapter.analyze', chapter_id: null }))).toBeNull()
    expect(TASK_KIND_LABELS['chapter.analyze']).toBe('章节分析')
    expect(TASK_KIND_LABELS['chapter.revise']).toBe('按建议重写')
    expect(isChapterWriteKind('chapter.revise')).toBe(true)
    expect(isChapterWriteKind('chapter.polish')).toBe(true)
    expect(isChapterWriteKind('chapter.analyze')).toBe(false)
  })

  it('待审核：未结束但不活动，不能重试；批量生成跳到章节页', () => {
    expect(isActiveTask('paused')).toBe(false)
    expect(isOpenTask('paused')).toBe(true)
    expect(isOpenTask('running')).toBe(true)
    expect(isOpenTask('succeeded')).toBe(false)
    expect(canRetryTask('paused')).toBe(false)
    expect(TASK_STATUS.paused.label).toBe('待审核')
    expect(TASK_KIND_LABELS['chapter.batch']).toBe('批量生成')
    expect(taskLink(task({ kind: 'chapter.batch', chapter_id: null }))).toBe('/projects/2/chapters')
  })

  it('resume / finish 接口路径', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(task()), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await taskApi.resume(4)
    await taskApi.finish(4)
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls.map((c) => [c[0], c[1].method])).toEqual([
      ['/api/v1/tasks/4/resume', 'POST'],
      ['/api/v1/tasks/4/finish', 'POST'],
    ])
  })

  it('list / clearFinished 的查询参数', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify([]), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await taskApi.list({ chapter_id: 3, active: true, limit: 5 })
    await taskApi.clearFinished(7)
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls[0][0]).toBe('/api/v1/tasks?chapter_id=3&active=true&limit=5')
    expect(calls[1][0]).toBe('/api/v1/tasks?finished=true&project_id=7')
    expect(calls[1][1].method).toBe('DELETE')
  })
})
