import { Alert, App, Button, Drawer, Form, Input, Popconfirm, Select, Space, Spin, Tabs, Tag, Typography } from 'antd'
import { useCallback, useRef, useState } from 'react'
import { serializationApi, type SerializationChapter, type SerializationEntryUpdate, type SerializationPublishRequest, type SerializationFinalizeRequest, type SerializationRequestOptions, type PreflightPage } from '../../api/serialization'
import { errorMessage } from '../../api/client'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import SerializationManuscript, { type Manuscript } from './SerializationManuscript'
import SerializationPreflightReview from './SerializationPreflightReview'
import { manuscriptTitle, beijingMinute, publicationRequest } from './serializationForm'
import { useSerializationAction } from './useSerializationAction'
import { EDITORIAL_LABEL } from './calendarModel'

type Submission = { kind: 'finalize'; body: SerializationFinalizeRequest } | { kind: 'publish'; body: SerializationPublishRequest }
interface Review { submission: Submission; page: PreflightPage; stale: boolean }

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

export default function SerializationChapterDrawer({ projectId, chapterId, canEdit, canExport, onClose, onChanged, onPublication }: Props) {
  const { message } = App.useApp()
  const { canRead, canReadNow, canEditNow, canExportNow } = useProjectAccess(projectId)
  const [tab, setTab] = useState('schedule')
  const [editionId, setEditionId] = useState<number>()
  const [channel, setChannel] = useState('')
  const [publishTime, setPublishTime] = useState(() => beijingMinute())
  const [lastPublication, setLastPublication] = useState<number>()
  const intent = useRef<SerializationPublishRequest | null>(null)
  const [review, setReview] = useState<Review>()
  const [formError, setFormError] = useState('')
  const load = useCallback((signal: AbortSignal) => serializationApi.chapter(projectId, chapterId, editionId,
    { signal, isCurrent: canReadNow }), [projectId, chapterId, editionId, canReadNow])
  const query = useMentionQuery(projectId, `serialization-chapter:${chapterId}:${editionId ?? 'latest'}`, load, canRead)
  const write = useSerializationAction(canEditNow)
  const download = useSerializationAction(canExportNow)
  const busy = write.busy || download.busy
  const data = query.data
  const chapter = data?.chapter
  const draft = data?.draft
  const edition = data?.edition
  const refresh = () => { setReview(undefined); query.reload() }
  const changed = () => { setEditionId(undefined); refresh(); onChanged() }
  const submit = (submission: Submission, token?: string, offset?: number) => {
    const send = (opts: SerializationRequestOptions, pageOffset?: number) => {
      const confirmation = token ? { confirmation_token: token } : {}
      if (submission.kind === 'finalize') {
        return serializationApi.finalize(projectId, chapterId, { ...submission.body, ...confirmation }, opts, pageOffset)
      }
      return serializationApi.publish(projectId, chapterId, { ...submission.body, ...confirmation }, opts, pageOffset)
    }
    void write.run(async (opts) => {
      let result = await send(opts, offset)
      const replaced = result.kind === 'review' && !!review && review.page.confirmation_token !== result.page.confirmation_token
      if (replaced && offset && opts.isCurrent()) result = await send(opts, 0)
      return { result, replaced }
    }, ({ result, replaced }) => {
      if (result.kind === 'review') {
        setReview({ submission, page: result.page, stale: result.stale || replaced })
        return
      }
      if ('seq' in result.result) {
        message.success(`已确认定稿 v${result.result.seq}`)
        setTab('publish')
      } else {
        intent.current = null
        setChannel('')
        setLastPublication(result.result.id)
        message.success('已登记发布')
      }
      changed()
    }, refresh)
  }
  const finalize = () => {
    if (!data || !chapter || !draft) return
    submit({ kind: 'finalize', body: {
      expected_revision: chapter.revision, expected_scene_id: data.scene_id,
      expected_content_revision: draft.content_revision, expected_fingerprint: draft.fingerprint,
    } })
  }
  const publish = () => {
    if (!chapter || !edition || review || busy) return
    let request: SerializationPublishRequest
    try {
      request = publicationRequest(intent.current, chapter.revision, edition.id, channel, publishTime)
    } catch (err) {
      setFormError(errorMessage(err))
      return
    }
    intent.current = request
    setFormError('')
    submit({ kind: 'publish', body: request })
  }
  const manuscriptPreview = (kind: Submission['kind'], manuscript: Manuscript) => canEdit && review?.submission.kind === kind
    ? <SerializationPreflightReview key={`${review.page.confirmation_token}:${review.page.offset}`}
      projectId={projectId} chapterId={chapterId} manuscript={manuscript} page={review.page} stale={review.stale}
      busy={busy} actionLabel={kind === 'finalize' ? '定稿' : '登记发布'}
      target={review.submission.kind === 'publish' ? `定稿 v${edition?.seq} · ${review.submission.body.channel}` : '当前已保存草稿'}
      onPage={(offset) => submit(review.submission, undefined, offset)}
      onConfirm={() => submit(review.submission, review.page.confirmation_token)} onCancel={() => setReview(undefined)} />
    : <SerializationManuscript projectId={projectId} manuscript={manuscript} />
  const schedule = data && chapter && (canEdit
    ? <ScheduleForm key={`${data.scene_id}:${chapter.revision}`} chapter={chapter} sceneId={data.scene_id} busy={write.busy} submit={(body) => {
      void write.run((opts) => serializationApi.updateEntry(projectId, chapterId, body, opts), () => {
        message.success('编辑状态与排期已保存'); changed()
      }, refresh)
    }} /> : <Typography.Text>计划发布日期：{chapter.planned_date || '未排期'}</Typography.Text>)

  const finalizePanel = <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    {draft ? manuscriptPreview('finalize', draft)
      : <Alert type="info" showIcon message="当前正文仍在生成，完成后可以预览并定稿。" />}
    {canEdit && draft && !review && <Button type="primary" loading={write.busy} disabled={!draft.content.trim()} onClick={finalize}>
      {chapter?.stage === 'final' ? '将当前草稿重新定稿' : '确认当前草稿为定稿'}
    </Button>}
  </Space>

  const publishPanel = data && <Space direction="vertical" size="middle" style={{ width: '100%' }}>
    {data.editions.length > 0 && <Form.Item label="发布稿次" htmlFor="serial-edition" layout="vertical" style={{ marginBottom: 0 }}>
      <Select id="serial-edition" disabled={busy} value={edition?.id} placeholder="选择已确认的稿次"
        options={data.editions.map((item) => ({ value: item.id, label: `定稿 v${item.seq} · ${manuscriptTitle(item)}` }))}
        onChange={(id) => { setEditionId(id); setReview(undefined); setFormError(''); write.clearError(); download.clearError() }} />
    </Form.Item>}
    {edition ? <>
      {data.draft_changed && <Alert type="warning" showIcon message={edition.id === chapter?.latest_edition_id
        ? '当前草稿有修改，本次登记使用下方定稿原文。' : '所选稿次与当前草稿有差异，本次登记使用下方原文。'} />}
      {manuscriptPreview('publish', edition)}
      {canExport && !review && <Space direction="vertical">
        <Button loading={download.busy} disabled={write.busy} onClick={() => {
          void download.run((opts) => serializationApi.downloadEdition(projectId, chapterId, edition.id, opts),
            (file) => { if (file) message.success(`已下载 ${file.filename}`) })
        }}>导出定稿 v{edition.seq}（TXT）</Button>
        <Typography.Text type="secondary">在平台作者后台提交稿件，实际发布后再登记。</Typography.Text>
        {download.error && <Alert type="error" showIcon message={download.error} />}
      </Space>}
      {canEdit && <Form layout="vertical" onFinish={publish} disabled={busy}>
        <Form.Item label="发布平台" htmlFor="serial-channel">
          <Input id="serial-channel" value={channel} placeholder="例如：起点中文网" onChange={(e) => { setChannel(e.target.value); setReview(undefined) }} />
        </Form.Item>
        <Form.Item label="实际发布时间（北京时间）" htmlFor="serial-time">
          <Input id="serial-time" type="datetime-local" value={publishTime} onChange={(e) => { setPublishTime(e.target.value); setReview(undefined) }} />
        </Form.Item>
        {formError && <Alert type="error" showIcon message={formError} style={{ marginBottom: 12 }} />}
        {!review && <Button type="primary" htmlType="submit" loading={write.busy} disabled={!channel.trim() || !publishTime}>
          登记发布 v{edition.seq}
        </Button>}
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
      <Tabs activeKey={tab} onChange={(key) => { if (!busy) { setTab(key); setReview(undefined); write.clearError() } }} items={[
        { key: 'schedule', label: '编辑与排期', children: schedule },
        { key: 'finalize', label: canEdit ? '确认定稿' : '当前草稿', children: finalizePanel },
        { key: 'publish', label: canEdit ? '登记发布' : '已确认稿件', children: publishPanel },
      ]} />
    </>}
  </Drawer>
}
