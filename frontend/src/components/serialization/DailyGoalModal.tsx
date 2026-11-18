import { Alert, InputNumber, Modal } from 'antd'
import { useState } from 'react'
import { ApiError } from '../../api/client'
import { serializationApi, type SerializationOverview } from '../../api/serialization'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useSerializationAction } from './useSerializationAction'

export default function DailyGoalModal({ projectId, initial, month, onClose, onSaved }: {
  projectId: number; initial: SerializationOverview; month: string; onClose: () => void; onSaved: () => void
}) {
  const { canEditNow } = useProjectAccess(projectId)
  const write = useSerializationAction(canEditNow)
  const [value, setValue] = useState<number | null>(initial.daily_goal)
  const [base, setBase] = useState(initial)
  const [conflicted, setConflicted] = useState(false)
  const valid = value === null || (Number.isInteger(value) && value > 0 && value <= 2_147_483_647)
  function save() {
    if (!valid) return
    void write.run(async (options) => {
      try {
        await serializationApi.setGoal(projectId, { daily_goal: value, expected_revision: base.revision }, options)
        return null
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 409 || !options.isCurrent()) throw err
        return serializationApi.overview(projectId, month, options)
      }
    }, (fresh) => {
      if (fresh === null) onSaved()
      else { setBase(fresh); setConflicted(true) }
    })
  }
  return <Modal open title="设置日更目标" okText="保存目标" cancelText="取消" confirmLoading={write.busy}
    okButtonProps={{ disabled: !valid }} onCancel={onClose} onOk={save}>
    <label className="serialization-goal-field" htmlFor="serialization-daily-goal">每天计划发布的章数
      <InputNumber id="serialization-daily-goal" value={value} min={1} max={2_147_483_647} step={1}
        disabled={write.busy} placeholder="未设置" onChange={setValue} />
    </label>
    <p className="serialization-muted">清空后仅统计存稿章数。</p>
    {conflicted && <Alert type="warning" showIcon
      message={`日更目标已被更新，当前为${base.daily_goal === null ? '未设置' : base.daily_goal + ' 章'}。请核对后再保存。`} />}
    {write.error && <Alert type="error" message={write.error} showIcon />}
  </Modal>
}
