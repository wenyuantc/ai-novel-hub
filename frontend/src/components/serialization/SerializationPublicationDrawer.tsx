import { Alert, Button, Drawer, Select, Space, Spin, Tabs, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { serializationApi } from '../../api/serialization'
import type { DocumentFormat } from '../../api/exports'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import TextDiff from '../TextDiff'
import { manuscriptTitle, beijingMinute } from './serializationForm'
import SerializationManuscript from './SerializationManuscript'
import { useSerializationAction } from './useSerializationAction'

interface Props { projectId: number; publicationId: number; canEdit: boolean; canExport: boolean; onClose: () => void; onChanged: () => void }

export default function SerializationPublicationDrawer({ projectId, publicationId, canExport, onClose }: Props) {
  const { canRead, canReadNow, canExportNow } = useProjectAccess(projectId)
  const [format, setFormat] = useState<DocumentFormat>('txt')
  const load = useCallback((signal: AbortSignal) => serializationApi.publication(projectId, publicationId, {
    signal, isCurrent: canReadNow,
  }), [projectId, publicationId, canReadNow])
  const query = useMentionQuery(projectId, `serialization-publication:${publicationId}`, load, canRead)
  const download = useSerializationAction(canExportNow)
  const details = query.data
  const draft = details?.draft
  const edition = details?.edition
  const record = details?.publication
  const [downloaded, setDownloaded] = useState('')
  const compare = draft && edition && <>
    {manuscriptTitle(edition) !== manuscriptTitle(draft) && <Typography.Paragraph>
      发布章名：{manuscriptTitle(edition)}<br />当前章名：{manuscriptTitle(draft)}
    </Typography.Paragraph>}
    {details.draft_changed && edition.content === draft.content && manuscriptTitle(edition) === manuscriptTitle(draft) &&
      <Alert type="info" showIcon message="正文文字一致，阅读排版有修改" style={{ marginBottom: 12 }} />}
    <TextDiff a={edition.content} b={draft.content} />
  </>
  return <Drawer open title="发布记录" width={760} onClose={onClose}>
    {query.loading && <Spin aria-label="正在读取发布记录" />}
    {query.error && <Alert type="error" message={query.error} showIcon action={<Button onClick={query.reload}>重试</Button>} />}
    {details && edition && record && <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text>{record.channel} · {beijingMinute(record.published_at).replace('T', ' ')}（北京时间） · 定稿 v{edition.seq}</Typography.Text>
      {record.chapter_id === null && <Alert type="info" showIcon message="源章节已删除，已保留发布原稿" />}
      {!draft && record.chapter_id !== null && <Typography.Text type="secondary">当前草稿暂不可对比。</Typography.Text>}
      <Tabs items={[
        { key: 'original', label: '发布原稿', children: <SerializationManuscript projectId={projectId} manuscript={edition} /> },
        ...(draft ? [{ key: 'compare', label: '与当前草稿对比', children: compare }] : []),
      ]} />
      {canExport && <Space wrap>
        <Select<DocumentFormat> aria-label="导出格式" value={format} disabled={download.busy} onChange={setFormat}
          options={[{ value: 'txt', label: 'TXT' }, { value: 'md', label: 'Markdown' }, { value: 'docx', label: 'Word（DOCX）' }]} />
        <Button loading={download.busy} onClick={() => {
          setDownloaded('')
          void download.run((opts) => serializationApi.download(projectId, publicationId, format, opts),
            (file) => { if (file) setDownloaded(file.filename) })
        }}>下载发布原稿</Button>
      </Space>}
      {download.error && <Alert type="error" showIcon message={download.error} />}
      {downloaded && <Typography.Text role="status" type="secondary">已下载 {downloaded}</Typography.Text>}
    </Space>}
  </Drawer>
}
