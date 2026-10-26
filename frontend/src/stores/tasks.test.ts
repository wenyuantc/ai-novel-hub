import { describe, expect, it } from 'vitest'
import type { Task } from '../api/types'
import { countFinished, countRunning, POLL_ACTIVE_MS, POLL_IDLE_MS, pollInterval, upsertActive } from './tasks'

function task(id: number, status: Task['status'] = 'running'): Task {
  return {
    id,
    kind: 'wizard.world',
    status,
    title: '世界观向导',
    project_id: 1,
    chapter_id: null,
    progress: 0,
    stage: '',
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
  }
}

describe('tasks store 纯函数', () => {
  it('轮询节奏：有活动任务 3 秒，否则 15 秒', () => {
    expect(POLL_ACTIVE_MS).toBe(3000)
    expect(POLL_IDLE_MS).toBe(15000)
    expect(pollInterval(0)).toBe(15000)
    expect(pollInterval(1)).toBe(3000)
    expect(pollInterval(5)).toBe(3000)
  })

  it('upsertActive：插入保持 id 倒序，覆盖同 id，已结束移除', () => {
    let list = upsertActive([], task(1))
    list = upsertActive(list, task(3, 'queued'))
    list = upsertActive(list, task(2))
    expect(list.map((t) => t.id)).toEqual([3, 2, 1])
    list = upsertActive(list, { ...task(3), progress: 50 })
    expect(list[0].progress).toBe(50)
    expect(upsertActive(list, task(2, 'cancelled')).map((t) => t.id)).toEqual([3, 1])
  })

  it('countFinished：上一轮存在、本轮消失的任务数', () => {
    expect(countFinished([task(1), task(2)], [task(2), task(3)])).toBe(1)
    expect(countFinished([], [task(1)])).toBe(0)
  })

  it('待审核：留在未结束列表，不计入快速轮询；从活动转为待审核算一次变化', () => {
    const list = upsertActive([task(1)], task(2, 'paused'))
    expect(list.map((t) => t.id)).toEqual([2, 1])
    expect(countRunning(list)).toBe(1)
    expect(countRunning([task(2, 'paused')])).toBe(0)
    expect(countFinished([task(1)], [task(1, 'paused')])).toBe(1)
    expect(countFinished([task(1, 'paused')], [task(1, 'paused')])).toBe(0)
    expect(countFinished([task(1, 'paused')], [task(1, 'queued')])).toBe(0)
  })
})
