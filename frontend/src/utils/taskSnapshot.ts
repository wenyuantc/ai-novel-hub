import type { Task } from '../api/types'

/** 复制任务和审核身份，使打开确认弹窗之后的轮询不能替换已展示的目标。 */
export function snapshotTaskAction(task: Task): Task {
  return { ...task, rich_review: task.rich_review ? { ...task.rich_review } : task.rich_review }
}
