import type { TaskResultViewerProps } from '../entities/types'
import { parseBatchSummary } from '../../utils/batch'
import { batchCheckpoint } from '../../utils/batchState'
import BatchProgressView from './BatchProgressView'

/** chapter.batch 的任务结果查看组件（已在 taskResultViewers 注册）：汇总 + 章节清单（取自 task.checkpoint）。只读。 */
export default function BatchResultView({ result, task }: TaskResultViewerProps) {
  return <BatchProgressView items={batchCheckpoint(task?.checkpoint)?.items ?? []} summary={parseBatchSummary(result)} />
}
