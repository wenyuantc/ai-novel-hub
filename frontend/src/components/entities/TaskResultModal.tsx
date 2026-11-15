import { Alert, Modal, Spin } from 'antd'
import { Suspense, useEffect, useState, useSyncExternalStore } from 'react'
import { currentRequestGeneration, watchRequestGeneration } from '../../api/client'
import type { Task } from '../../api/types'
import { useTaskStream } from '../../hooks/useTaskStream'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../../stores/project'
import { TASK_RESULT_VIEWERS } from './taskResultViewers'

interface Props {
  task: Task
  onClose: () => void
}
const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

/**
 * 任务面板「查看结果」：订阅已结束 / 待审核任务（快照 + done / paused 的 result），按 kind 渲染注册表中的结果组件（可编辑预览 + 保存）。
 * 关闭页面后再从任务面板打开，得到的是同一份预览。
 */
export default function TaskResultModal({ task, onClose }: Props) {
  const [boundSession] = useState(currentRequestGeneration)
  const [scope] = useState(() => task.project_id === null ? null : captureProjectScope(task.project_id))
  const scopeVersion = useProjectStore((state) => state.scopeVersion)
  const session = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const allowed = session === boundSession && (!scope || (scopeVersion === scope.version && isProjectScopeCurrent(scope, 'canUseAI')))
  const stream = useTaskStream()
  const { follow } = stream
  const [result, setResult] = useState<unknown>(undefined)
  const [detail, setDetail] = useState<string | null>(null)
  // 收据受限收尾（结果未发布）仍是成功终态：说明按提示而非错误展示。
  const [detailTone, setDetailTone] = useState<'info' | 'error'>('error')
  const Viewer = TASK_RESULT_VIEWERS[task.kind]

  useEffect(() => {
    if (!allowed) return
    let alive = true
    void follow(task).then((outcome) => {
      if (!alive) return
      setDetailTone(outcome.status === 'succeeded' && outcome.unpublished ? 'info' : 'error')
      if (outcome.status === 'succeeded' && outcome.unpublished) setDetail(outcome.detail)
      else if (outcome.status === 'succeeded' || outcome.status === 'paused') setResult(outcome.result)
      else if (outcome.status === 'lost') setDetail(outcome.message)
      else if (outcome.status !== 'detached') setDetail(outcome.detail || '任务未成功完成')
    })
    return () => {
      alive = false
    }
    // 原始任务身份变化时重新订阅，不把复用 ID 当作同一任务。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allowed, task.id, task.task_key, follow])

  return (
    <Modal open title={task.title} footer={null} width={900} maskClosable={false} onCancel={onClose} destroyOnHidden>
      {!allowed && <Alert type="warning" showIcon message="会话或作品授权已变化，请重新打开任务。" />}
      {allowed && detail && <Alert type={detailTone} showIcon message={detail} />}
      {allowed && result === undefined && !detail && (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin />
        </div>
      )}
      {allowed && result !== undefined && Viewer && task.project_id !== null && (
        <Suspense
          fallback={
            <div style={{ textAlign: 'center', padding: 32 }}>
              <Spin />
            </div>
          }
        >
          <Viewer projectId={task.project_id} result={result} task={stream.task ?? task} />
        </Suspense>
      )}
    </Modal>
  )
}
