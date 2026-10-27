// 拆书反推（book.import）断点的轻量读取与「跳过本批」判定（任务面板在入口 chunk 中使用，保持精简；完整收窄 bookImportSummary 在 bookImport.ts）。

import type { BookImportPauseReason, Task } from '../api/types'

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 断点摘要里的暂停原因（目前只有 AI 调用失败）；没有或结构不符时为 null */
export function bookImportPause(checkpoint: unknown): BookImportPauseReason | null {
  const r = isRecord(checkpoint) && isRecord(checkpoint.pause_reason) ? checkpoint.pause_reason : null
  if (!r || r.reason !== 'failed') return null
  return {
    reason: 'failed',
    phase: r.phase === 'synthesize' ? 'synthesize' : 'digest',
    batch_index: typeof r.batch_index === 'number' ? r.batch_index : null,
    error: typeof r.error === 'string' ? r.error : '',
  }
}

/** 通读批次进度（已处理 = 完成 + 跳过）；还没有批次时为 null */
export function bookImportBatches(checkpoint: unknown): { done: number; total: number } | null {
  if (!isRecord(checkpoint)) return null
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  const total = num(checkpoint.batches_total)
  return total > 0 ? { done: num(checkpoint.batches_done) + num(checkpoint.batches_skipped), total } : null
}

/** 暂停原因的一句话说明 */
export function bookImportPauseText(r: BookImportPauseReason): string {
  const where = r.phase === 'synthesize' ? '汇总' : `通读第 ${(r.batch_index ?? 0) + 1} 批`
  return `${where}失败（已自动重试 2 次）：${r.error || '未知错误'}`
}

/** 「跳过本批」只在拆书任务因 AI 调用失败而暂停时提供（与后端 skip 接口的 409 条件一致） */
export function canSkipBookImport(task: Pick<Task, 'kind' | 'status' | 'checkpoint'>): boolean {
  return task.kind === 'book.import' && task.status === 'paused' && bookImportPause(task.checkpoint) !== null
}
