import { Alert, Button, Modal, Select, Space, Switch, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { seriesApi, type SeriesImpactQuery, type SeriesMutation } from '../../api/series'
import type { SettingAiMode } from '../../api/settingAiVisibility'
import { useLoader } from '../../hooks/useLoader'
import { SETTING_AI_MODE_OPTIONS } from '../settingAi/settingAiOptions'
import SeriesBlockers from './SeriesBlockers'
import { seriesNeedsReview } from './seriesState'
import { useSeriesAction } from './useSeriesAction'

const ACTIONS = {
  promote: { title: '提升为系列共享', detail: '将本书的基础档案纳入系列共享库。原书仍可使用此条目，已保存的出场、关联和剧情变化不变。' },
  leave: { title: '退出系列', detail: '将此作品从系列中移出。仅在没有需要保留的共享引用或书内资料时才能退出。' },
  unuse: { title: '取消在本书使用', detail: '不改变共享库，仅在没有引用或书内资料时移除此书的采用记录。' },
  move: { title: '收回指定作品', detail: '共享档案将改为目标书的专属设定。目标书已有的引用和剧情变化保留；其他书如有依赖则阻止操作。' },
  delete: { title: '删除共享设定', detail: '从系列库移除此条目。任何已采用的作品如仍有引用或书内资料，则阻止删除。' },
}

interface Props {
  seriesId: number
  request: SeriesImpactQuery
  label: string
  onClose: () => void
  onDone: (affectedBookIds: number[], result: SeriesMutation) => void
}

export default function SeriesTransitionDialog({ seriesId, request, label, onClose, onDone }: Props) {
  const action = useSeriesAction()
  const [mode, setMode] = useState<SettingAiMode>('detected')
  const [tracking, setTracking] = useState(true)
  const load = useCallback(() => seriesApi.impact(seriesId, request), [seriesId, request])
  const preview = useLoader(load)
  const impact = preview.data
  const meta = ACTIONS[request.action]
  const requiresReview = action.error !== null && seriesNeedsReview(action.error)
  const previewInvalid = !impact || preview.loading || !!preview.error || !!impact.blockers.length
  const refresh = () => {
    action.clearError()
    preview.reload()
  }
  const apply = () => {
    if (!impact || previewInvalid || requiresReview) return
    const revision = impact.revision
    void action.run(async (): Promise<SeriesMutation> => {
      switch (request.action) {
        case 'promote':
          if (!impact.source_stamp) throw new Error('没有取得来源版本，请重新预检')
          return seriesApi.promote(seriesId, {
            project_id: request.project_id,
            ref: { kind: request.kind, id: request.entry_id },
            source_stamp: impact.source_stamp,
            expected_revision: revision,
            ...(impact.requires_ai_settings ? { ai_settings: { ai_mode: mode, mention_enabled: tracking } } : {}),
          })
        case 'leave':
          return seriesApi.leave(seriesId, request.project_id, revision)
        case 'unuse':
          return seriesApi.unuse(seriesId, request.entry_id, request.project_id, revision)
        case 'move':
          return seriesApi.move(seriesId, request.entry_id, request.project_id, revision)
        case 'delete':
          return seriesApi.removeEntry(seriesId, request.entry_id, revision)
      }
    }, (result) => onDone([...new Set([
      ...impact.affected_books.map((book) => book.id),
      ...('project_id' in request ? [request.project_id] : []),
    ])], result))
  }

  const blockedDuringWrite = action.error instanceof ApiError && action.error.body?.code === 'series_setting_in_use'
  return <Modal
    title={`${meta.title} · ${label}`}
    open
    onCancel={onClose}
    onOk={apply}
    okText={meta.title}
    cancelText="取消"
    confirmLoading={action.busy}
    okButtonProps={{ disabled: previewInvalid || requiresReview || blockedDuringWrite, danger: request.action === 'delete' }}
    closable={!action.busy}
    maskClosable={!action.busy}
    keyboard={!action.busy}
    cancelButtonProps={{ disabled: action.busy }}
  >
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text>{meta.detail}</Typography.Text>
      {preview.loading && <Typography.Text type="secondary" role="status">正在检查影响…</Typography.Text>}
      {preview.error && <Alert type="error" showIcon message={preview.error} action={<Button size="small" onClick={refresh}>重新检查</Button>} />}
      {impact && !preview.loading && !preview.error && <>
        <Typography.Text type="secondary">受影响作品：{impact.affected_books.length ? impact.affected_books.map((book) =>
          `${book.title}${book.recycled ? '（回收站）' : ''}`).join('、') : '无已采用作品'}</Typography.Text>
        <SeriesBlockers blockers={impact.blockers} />
        {request.action === 'promote' && impact.requires_ai_settings && <>
          <Alert type="info" showIcon message="原条目使用旧版 AI 规则，请为系列选择明确的策略。" />
          <Space wrap>
            <Select<SettingAiMode> aria-label="共享条目的 AI 策略" value={mode} onChange={setMode}
              disabled={action.busy} options={SETTING_AI_MODE_OPTIONS} style={{ minWidth: 172 }} />
            <Switch aria-label="启用提及追踪" checked={tracking} onChange={setTracking} disabled={action.busy} />
            <Typography.Text>提及追踪</Typography.Text>
          </Space>
        </>}
      </>}
      {!!action.error && <Alert type="error" showIcon message={errorMessage(action.error)}
        action={(requiresReview || blockedDuringWrite) ? <Button size="small" onClick={refresh}>重新检查</Button> : undefined} />}
    </Space>
  </Modal>
}
