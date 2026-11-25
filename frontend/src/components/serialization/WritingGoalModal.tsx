import { Alert, InputNumber, Modal, Radio } from 'antd'
import { useState } from 'react'
import { ApiError } from '../../api/client'
import { writingStatsApi, type WritingGoal, type WritingPeriod } from '../../api/writingStats'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useSerializationAction } from './useSerializationAction'

export default function WritingGoalModal({ projectId, initial, onClose, onSaved }: {
  projectId: number; initial: WritingGoal; onClose: () => void; onSaved: () => void
}) {
  const { canEditNow } = useProjectAccess(projectId)
  const write = useSerializationAction(canEditNow)
  const [base, setBase] = useState(initial)
  const [period, setPeriod] = useState<WritingPeriod | null>(initial.period ?? 'day')
  const [words, setWords] = useState<number | null>(initial.target_words)
  const valid = period === null || (words !== null && Number.isInteger(words) && words > 0 && words <= 2_147_483_647)
  function save() {
    if (!valid) return
    void write.run(async (options) => {
      try {
        await writingStatsApi.setGoal(projectId, {
          period, target_words: period === null ? null : words, expected_revision: base.revision,
        }, options)
        return null
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 409 || !options.isCurrent()) throw err
        return (await writingStatsApi.overview(projectId, undefined, options)).goal
      }
    }, (fresh) => {
      if (fresh) setBase(fresh)
      else onSaved()
    })
  }
  return <Modal open title="设置我的码字目标" okText="保存码字目标" cancelText="取消"
    onCancel={onClose} onOk={save} confirmLoading={write.busy} okButtonProps={{ disabled: !valid }}>
    <Radio.Group aria-label="目标周期" disabled={write.busy}
      options={[{ label: '日目标', value: 'day' }, { label: '周目标', value: 'week' }, { label: '关闭目标', value: 'off' }]}
      optionType="button" onChange={({ target }) => setPeriod(target.value === 'off' ? null : target.value)}
      value={period ?? 'off'} />
    {period && <label className="serialization-goal-field" htmlFor="writing-goal-words">
      {period === 'day' ? '每天' : '每周'}计划手写字数
      <InputNumber id="writing-goal-words" value={words} min={1} max={2_147_483_647} step={1}
        disabled={write.busy} onChange={setWords} />
    </label>}
    {base.revision !== initial.revision && <Alert type="warning" showIcon
      message={`目标已更新，当前${base.period ? `为${base.period === 'day' ? '每日' : '每周'} ${base.target_words} 字` : '已关闭'}。请核对后再保存。`} />}
    {write.error && <Alert type="error" message={write.error} showIcon />}
  </Modal>
}
