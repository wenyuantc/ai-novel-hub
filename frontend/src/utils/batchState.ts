// 批量生成横幅状态 / 续跑位置 / 排队标签（章节页在入口 chunk 中使用，保持精简；其余纯函数在 batch.ts）。

import type { BatchCheckpoint, Task, TaskStatus } from '../api/types'

/** Task.checkpoint 收窄为批量断点（含 items 的才是；拆书反推的断点摘要等其他类型为 null） */
export function batchCheckpoint(checkpoint: Task['checkpoint'] | undefined): BatchCheckpoint | null {
  return checkpoint && 'items' in checkpoint && Array.isArray(checkpoint.items) ? checkpoint : null
}

export type BatchBannerState = 'running' | 'paused' | 'resumable' | 'none'

/** 章节页横幅：排队 / 运行中 → running；待审核 → paused；失败 / 中断 → resumable（可从断点续跑）；其余不显示 */
export function batchBannerState(task: Pick<Task, 'kind' | 'status'> | null | undefined): BatchBannerState {
  if (!task || task.kind !== 'chapter.batch') return 'none'
  const map: Partial<Record<TaskStatus, BatchBannerState>> = {
    queued: 'running',
    running: 'running',
    paused: 'paused',
    failed: 'resumable',
    interrupted: 'resumable',
  }
  return map[task.status] ?? 'none'
}

/** 断点所指的下一章下标（后端 step_cursor）；没有断点或已全部处理完时为 null */
export function batchResumeIndex(task: Pick<Task, 'checkpoint' | 'step_cursor'>): number | null {
  const total = batchCheckpoint(task.checkpoint)?.items.length ?? 0
  if (total === 0) return null
  return task.step_cursor < total ? task.step_cursor : null
}

/** 未结束的批量里断点之后待生成（pending / 断点处失败）的章节 id（章节列表「排队批量」标签）；首次运行前没有断点，返回空集 */
export function queuedBatchChapterIds(task: Pick<Task, 'kind' | 'status' | 'checkpoint' | 'step_cursor'> | null | undefined): Set<number> {
  const ids = new Set<number>()
  if (!task || task.kind !== 'chapter.batch') return ids
  if (task.status !== 'queued' && task.status !== 'running' && task.status !== 'paused') return ids
  const from = batchResumeIndex(task)
  if (from === null) return ids
  for (const it of batchCheckpoint(task.checkpoint)?.items.slice(from) ?? []) {
    // 断点处失败停下的章节在重试后会重新生成，也算排队
    if (it.state === 'pending' || it.state === 'failed') ids.add(it.chapter_id)
  }
  return ids
}
