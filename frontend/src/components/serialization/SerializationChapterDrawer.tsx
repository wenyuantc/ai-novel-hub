import { Alert, App, Button, Drawer, Form, Input, Popconfirm, Select, Space, Spin, Tabs, Tag, Typography } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { serializationApi, type SerializationChapter, type SerializationEntryUpdate, type SerializationPublishRequest } from '../../api/serialization'
import { errorMessage } from '../../api/client'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import SerializationManuscript from './SerializationManuscript'
import { manuscriptTitle, beijingMinute, publicationRequest } from './serializationForm'
import { useSerializationAction } from './useSerializationAction'
import { EDITORIAL_LABEL } from './calendarModel'

interface Props {
  projectId: number; chapterId: number; canEdit: boolean; canExport: boolean
  onChanged: () => void; onClose: () => void; onPublication?: (id: number) => void
}

function ScheduleForm({ chapter, sceneId, busy, submit }: {
  chapter: SerializationChapter; sceneId: number; busy: boolean; submit: (body: SerializationEntryUpdate) => void
}) {
  const [stage, setStage] = useState(chapter.stage)
  const [date, setDate] = useState(chapter.planned_date ?? '')
  const changed = stage !== chapter.stage || (date || null) !== chapter.planned_date
  const save = () => {
    const body: SerializationEntryUpdate = { expected_revision: chapter.revision, expected_scene_id: sceneId }
    if (stage !== chapter.stage && stage !== 'final') body.stage = stage
    if ((date || null) !== chapter.planned_date) body.planned_date = date || null
    if (changed) submit(body)
  }
  const button = <Button type="primary" loading={busy} disabled={!changed} onClick={chapter.stage === 'final' && stage !== 'final' ? undefined : save}>保存编辑状态与排期</Button>
  return <Form layout="vertical">
    <Form.Item label="编辑状态" htmlFor="serial-stage">
      <Select id="serial-stage" value={stage} disabled={busy} onChange={setStage} options={[
        { value: 'revision', label: '待修订' }, { value: 'proofreading', label: '待校对' },
        ...(chapter.stage === 'final' ? [{ value: 'final', label: '定稿' }] : []),
      ]} />
    </Form.Item>
    <Form.Item label="计划发布日期（北京时间）" htmlFor="serial-plan">
      <Space.Compact style={{ maxWidth: '100%' }}>
        <Input type="date" id="serial-plan" value={date} onChange={(event) => setDate(event.target.value)} disabled={busy} />
        <Button disabled={busy || !date} onClick={() => setDate('')}>清除</Button>
      </Space.Compact>
    </Form.Item>
    {chapter.stage === 'final' && stage !== 'final'
      ? <Popconfirm title="撤下这章的待发定稿？" description="已发布的记录仍保留。重新定稿后可再次作为待发稿。"
        okText="撤下并保存" cancelText="取消" onConfirm={save}>{button}</Popconfirm> : button}
  </Form>
}

export default function SerializationChapterDrawer({ projectId, chapterId, canEdit, onClose, onChanged, onPublication }: Props) {
  const { message } = App.useApp()
  const { canRead, canReadNow, canEditNow } = useProjectAccess(projectId)
  const [tab, setTab] = useState('schedule')
  const [editionId, setEditionId] = useState<number>()
  const [channel, setChannel] = useState('')
  const [publishTime, setPublishTime] = useState(() => beijingMinute())
  const [lastPublication, setLastPublication] = useState<number>()
  const intent = useRef<SerializationPublishRequest | null>(null)
  const [formError, setFormError] = useState('')
  const load = useCallback((signal: AbortSignal) => serializationApi.chapter(projectId, chapterId, editionId,
    { signal, isCurrent: canReadNow }), [projectId, chapterId, editionId, canReadNow])
  const query = useMentionQuery(projectId, `serialization-chapter:${chapterId}:${editionId ?? 'latest'}`, load, canRead)
  const write = useSerializationAction(canEditNow)
  const data = query.data
  const chapter = data?.chapter
  const draft = data?.draft
  const edition = data?.edition
  const refresh = () => { setEditionId(undefined); query.reload() }
  const changed = () => { refresh(); onChanged() }
  const finalize = () => {
    if (!data || !chapter || !draft) return
    void write.run((opts) => serializationApi.finalize(projectId, chapterId, {
      expected_revision: chapter.revision, expected_scene_id: data.scene_id,
      expected_content_revision: draft.content_revision, expected_fingerprint: draft.fingerprint,
    }, opts), (result) => {
      message.success(`已确认定稿 v${result.seq}`)
      setTab('publish')
      changed()
    }, refresh)
  }
  const publish = () => {
    if (!chapter || !edition) return
    let request: SerializationPublishRequest
    try {
      request = publicationRequest(intent.current, chapter.revision, edition.id, channel, publishTime)
    } catch (err) {
      setFormError(errorMessage(err))
      return
    }
    intent.current = request
    setFormError('')
    void write.run((opts) => serializationApi.publish(projectId, chapterId, request, opts), (result) => {
      intent.current = null
      setChannel('')
      setLastPublication(result.id)
      message.success('已登记发布')
      changed()
    }, refresh)
  }
  const schedule = data && chapter && (canEdit
    ? <ScheduleForm key={`${data.scene_id}:${chapter.revision}`} chapter={chapter} sceneId={data.scene_id} busy={write.busy} submit={(body) => {
      void write.run((opts) => serializationApi.updateEntry(projectId, chapterId, body, opts), () => {
        message.success('编辑状态与排期已保存'); changed()
      }, refresh)
    }} /> : <Typography.Text>计划发布日期：{chapter.planned_date || '未排期'}</Typography.Text>)

  const finalizePanel = <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    {draft ? <SerializationManuscript projectId={projectId} manuscript={draft} />
      : <Alert type="info" showIcon message="当前正文仍在生成，完成后可以预览并定稿。" />}
    {canEdit && draft && <Button type="primary" loading={write.busy} disabled={!draft.content.trim()} onClick={finalize}>
      {chapter?.stage === 'final' ? '将当前草稿重新定稿' : '确认当前草稿为定稿'}
    </Button>}
  </Space>

  const publishPanel = data && <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    {data.editions.length > 0 && <Form.Item label="发布稿次" htmlFor="serial-edition" layout="vertical" style={{ marginBottom: 0 }}>
      <Select id="serial-edition" disabled={write.busy} value={edition?.id} placeholder="选择已确认的稿次"
        options={data.editions.map((item) => ({ value: item.id, label: `定稿 v${item.seq} · ${manuscriptTitle(item)}` }))}
        onChange={(id) => { setEditionId(id); setFormError(''); write.clearError() }} />
    </Form.Item>}
    {edition ? <>
      {data.draft_changed && <Alert type="warning" showIcon message={edition.id === chapter?.latest_edition_id
        ? '当前草稿有修改，本次登记使用下方定稿原文。' : '所选稿次与当前草稿有差异，本次登记使用下方原文。'} />}
      <SerializationManuscript projectId={projectId} manuscript={edition} />
      {canEdit && <Form layout="vertical" onFinish={publish} disabled={write.busy}>
        <Form.Item label="发布平台" htmlFor="serial-channel">
          <Input id="serial-channel" value={channel} placeholder="例如：起点中文网" onChange={(e) => setChannel(e.target.value)} />
        </Form.Item>
        <Form.Item label="实际发布时间（北京时间）" htmlFor="serial-time">
          <Input id="serial-time" type="datetime-local" value={publishTime} onChange={(e) => setPublishTime(e.target.value)} />
        </Form.Item>
        {formError && <Alert type="error" showIcon message={formError} style={{ marginBottom: 12 }} />}
        <Button type="primary" htmlType="submit" loading={write.busy} disabled={!channel.trim() || !publishTime}>
          登记发布 v{edition.seq}
        </Button>
      </Form>}
    </> : <Alert type="info" showIcon message={canEdit ? '请先在“确认定稿”中核对正文并定稿。' : '暂无可查看的定稿。'} />}
    {lastPublication && onPublication && <Button onClick={() => onPublication(lastPublication)}>查看刚登记的发布原稿</Button>}
  </Space>
  return <Drawer open title={chapter ? manuscriptTitle(chapter) : '章节连载详情'} onClose={onClose} width={740}>
    {write.error && <Alert type="error" showIcon message={write.error} style={{ marginBottom: 12 }} />}
    {query.loading && <Spin aria-label="正在读取章节" />}
    {query.error && <Alert type="error" showIcon message={query.error} action={<Button onClick={refresh}>重新读取</Button>} />}
    {chapter && <>
      <Space wrap><Tag>{EDITORIAL_LABEL[chapter.stage]}</Tag>{chapter.is_published && <Tag color="green">已发布</Tag>}</Space>
      <Tabs activeKey={tab} onChange={(key) => { if (!write.busy) setTab(key) }} items={[
        { key: 'schedule', label: '编辑与排期', children: schedule },
        { key: 'finalize', label: canEdit ? '确认定稿' : '当前草稿', children: finalizePanel },
        { key: 'publish', label: canEdit ? '登记发布' : '已确认稿件', children: publishPanel },
      ]} />
    </>}
  </Drawer>
}
