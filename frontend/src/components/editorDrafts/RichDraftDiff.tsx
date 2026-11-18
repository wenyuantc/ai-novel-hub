import { Collapse, Divider, List, Space, Typography } from 'antd'
import type { ReactNode } from 'react'
import { useMemo } from 'react'
import type { RichDocument, RichMarker, RichSection } from '../../api/richDocument'
import TextDiff from '../TextDiff'
import { draftFormatChanges, richDraftChanges, type DraftChange } from './richDraftChanges'

const DIFF_TEXT_LIMIT = 10_000
const MARKER_COLORS: Record<RichMarker['color'], string> = { yellow: '黄色', green: '绿色', blue: '蓝色', purple: '紫色', red: '红色' }
const TEXT_STYLE = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 240, overflowY: 'auto', marginBlock: 4 } as const

export function DraftTextReview({ remote, local }: { remote?: string; local?: string }) {
  if (remote !== undefined && local !== undefined && remote !== local && remote.length + local.length <= DIFF_TEXT_LIMIT) {
    return <TextDiff a={remote} b={local} maxHeight={240} />
  }
  if (remote !== undefined && remote === local) return <Typography.Text type="secondary">文字相同</Typography.Text>
  return <Space direction="vertical" style={{ width: '100%' }}>
    {remote !== undefined && <div><Typography.Text strong>服务端</Typography.Text><Typography.Paragraph style={TEXT_STYLE}>{remote || '（空）'}</Typography.Paragraph></div>}
    {local !== undefined && <div>{remote !== undefined && <Typography.Text strong>本地</Typography.Text>}<Typography.Paragraph style={TEXT_STYLE}>{local || '（空）'}</Typography.Paragraph></div>}
  </Space>
}

function ChangeList<T>({ data, render }: { data: DraftChange<T>[]; render: (change: DraftChange<T>) => ReactNode }) {
  return <List size="small" rowKey={(item) => item.key} dataSource={data} pagination={data.length > 8 ? { pageSize: 8, size: 'small', showSizeChanger: false } : false}
    renderItem={(change) => <List.Item style={{ display: 'block', overflowWrap: 'anywhere' }}>{render(change)}</List.Item>} />
}

function FormatList({ changes, preview }: { changes: DraftChange<string>[]; preview: boolean }) {
  return <ChangeList data={changes} render={({ remote, local }) => (
    <Typography.Text>{preview ? local : remote ? `服务端：${remote}` : `本地：${local}`}</Typography.Text>
  )} />
}

function sectionPlacement(section: RichSection) {
  return `正文位置 ${section.anchor + 1} · 同位置顺序 ${section.order + 1} · ${section.include_in_ai ? '可供 AI 参考' : '不供 AI 参考'}`
}

function SectionReview({ change, preview }: { change: DraftChange<RichSection>; preview: boolean }) {
  const { remote, local } = change
  const format = draftFormatChanges(remote?.formatting, local?.formatting)
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Typography.Text strong>{!preview && (!remote ? '新增 · ' : !local ? '移除 · ' : '')}{local?.title || remote?.title || '未命名区块'}</Typography.Text>
    {remote && <Typography.Text>{local ? '服务端：' : ''}{remote.title || '未命名'} · {sectionPlacement(remote)}</Typography.Text>}
    {local && <Typography.Text>{!preview ? '本地：' : ''}{local.title || '未命名'} · {sectionPlacement(local)}</Typography.Text>}
    <DraftTextReview remote={remote?.content} local={local?.content} />
    {format.length > 0 && <Collapse size="small" items={[{
      key: 'format', label: `区块格式（${format.length} 项）`, children: <FormatList changes={format} preview={preview} />,
    }]} />}
  </Space>
}

function MarkerLine({ label, marker, doc }: { label?: string; marker: RichMarker; doc?: RichDocument }) {
  const section = marker.section_key === null ? '正文' : doc?.sections.find((item) => item.key === marker.section_key)?.title || '作者区块'
  const lastStart = marker.draft_last_start ?? marker.last_start
  const location = marker.start !== null && marker.end !== null ? ` · 第 ${marker.start + 1}–${marker.end} 字符` : lastStart === null ? '' : ` · 原位置 ${lastStart + 1}`
  const excerpt = marker.draft_excerpt ?? marker.excerpt
  const truncated = marker.draft_excerpt === undefined || marker.draft_excerpt === null ? marker.excerpt_truncated : marker.draft_excerpt_truncated
  return <Typography.Paragraph style={{ marginBottom: 4 }}>
    {label}{section} · {MARKER_COLORS[marker.color]} · {marker.status === 'active' ? '有效' : '待重新关联'}{location}
    {excerpt && <> · 「{excerpt}{truncated ? '…' : ''}」</>}
  </Typography.Paragraph>
}

/** 同一份只读文档视图用于草稿预览与冲突比较。 */
export default function RichDraftDiff({ remote, local }: { remote?: RichDocument; local: RichDocument }) {
  const changes = useMemo(() => richDraftChanges(remote, local), [remote, local])
  const preview = remote === undefined
  const items = []
  if (changes.formatting.length) items.push({ key: 'format', label: `正文格式（${changes.formatting.length} 项）`, children: <FormatList changes={changes.formatting} preview={preview} /> })
  if (changes.sections.length) items.push({ key: 'sections', label: `作者区块（${changes.sections.length} 项）`, children: <ChangeList data={changes.sections} render={(change) => <SectionReview change={change} preview={preview} />} /> })
  if (changes.markers.length) items.push({ key: 'markers', label: `标记（${changes.markers.length} 项）`, children: <ChangeList data={changes.markers} render={({ remote: prior, local: next }) => <>
    {!preview && !prior && <Typography.Text strong>新增标记</Typography.Text>}
    {!preview && !next && <Typography.Text strong>移除标记</Typography.Text>}
    {prior && <MarkerLine label="服务端：" marker={prior} doc={remote} />}
    {next && <MarkerLine label={preview ? undefined : '本地：'} marker={next} doc={local} />}
  </>} /> })
  if (changes.retired.length) items.push({ key: 'retired', label: `已退场区块记录（${changes.retired.length} 项）`, children: <ChangeList data={changes.retired} render={({ remote: prior, local: next }) => <Typography.Text>{preview ? next : prior ? `服务端：${prior}` : `本地：${next}`}</Typography.Text>} /> })
  return (
    <Space direction="vertical" style={{ width: '100%', minWidth: 0 }}>
      <Typography.Text strong>正文</Typography.Text>
      {!preview && <Typography.Text type="secondary">差异中，删除线表示仅服务端有的文字，浅色底表示本地新增的文字。</Typography.Text>}
      <DraftTextReview remote={remote?.content} local={local.content} />
      {items.length > 0 && <><Divider style={{ marginBlock: 4 }} /><Collapse size="small" items={items} /></>}
    </Space>
  )
}
