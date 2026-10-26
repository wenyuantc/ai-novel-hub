import { Alert, Modal, Spin } from 'antd'
import { Suspense, useEffect, useState } from 'react'
import type { Task } from '../../api/types'
import { useTaskStream } from '../../hooks/useTaskStream'
import { TASK_RESULT_VIEWERS } from './taskResultViewers'

interface Props {
  task: Task
  onClose: () => void
}

/**
 * 任务面板「查看结果」：订阅已结束 / 待审核任务（快照 + done / paused 的 result），按 kind 渲染注册表中的结果组件（可编辑预览 + 保存）。
 * 关闭页面后再从任务面板打开，得到的是同一份预览。
 */
export default function TaskResultModal({ task, onClose }: Props) {
  const stream = useTaskStream()
  const { follow } = stream
  const [result, setResult] = useState<unknown>(undefined)
  const [detail, setDetail] = useState<string | null>(null)
  const Viewer = TASK_RESULT_VIEWERS[task.kind]

  useEffect(() => {
    let alive = true
    void follow(task).then((outcome) => {
      if (!alive) return
      if (outcome.status === 'succeeded' || outcome.status === 'paused') setResult(outcome.result)
      else if (outcome.status === 'lost') setDetail(outcome.message)
      else if (outcome.status !== 'detached') setDetail(outcome.detail || '任务未成功完成')
    })
    return () => {
      alive = false
    }
    // 只按 id 订阅一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id, follow])

  return (
    <Modal open title={task.title} footer={null} width={900} maskClosable={false} onCancel={onClose} destroyOnHidden>
      {detail && <Alert type="error" showIcon message={detail} />}
      {result === undefined && !detail && (
        <div style={{ textAlign: 'center', padding: 32 }}>
          <Spin />
        </div>
      )}
      {result !== undefined && Viewer && task.project_id !== null && (
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
