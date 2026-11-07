import { Alert, Button, InputNumber, Pagination, Radio, Select, Space, Typography } from 'antd'
import { useCallback, useState } from 'react'
import type { ProgressionPositionInput } from '../../api/types'
import { sceneApi } from '../../api/scenes'
import { useProjectId } from '../../hooks/useProjectId'
import { useMentionQuery } from '../../hooks/useMentionQuery'
import { chapterOptions, type ChapterReference } from './position'

interface Props {
  chapters: readonly ChapterReference[]
  value: ProgressionPositionInput
  onChange: (position: ProgressionPositionInput) => void
  disabled?: boolean
  purpose?: 'event' | 'preview'
}

/** 现有章节以 ID 绑定；计划章节仅保存章号。 */
export default function ChapterPositionInput({ chapters, value, onChange, disabled = false, purpose = 'event' }: Props) {
  return (
    <Space direction="vertical" size="small" style={{ width: '100%' }}>
      <Radio.Group
        aria-label="章节位置类型"
        disabled={disabled}
        value={value.anchor_kind}
        onChange={(event) => {
          const kind: unknown = event.target.value
          if (kind === 'chapter' || kind === 'planned' || kind === 'scene') {
            onChange({ anchor_kind: kind, chapter_id: null, chapter_no: null, ...(kind === 'scene' ? { scene_id: null } : {}), boundary: value.boundary })
          }
        }}
        options={[{ value: 'chapter', label: '已有章节' }, { value: 'scene', label: '实际场景' }, { value: 'planned', label: '计划章节' }]}
      />
      {value.anchor_kind === 'chapter' ? (
        <Select
          aria-label="选择关联章节"
          placeholder={chapters.length ? '选择章节' : '暂无章节，可选择计划章节'}
          disabled={disabled}
          showSearch
          optionFilterProp="label"
          options={chapterOptions(chapters)}
          value={value.chapter_id ?? undefined}
          onChange={(chapterId: number) => onChange({ ...value, chapter_id: chapterId, chapter_no: null })}
          style={{ width: '100%' }}
        />
      ) : value.anchor_kind === 'scene' ? <SceneAnchorSelect value={value.scene_id ?? null} disabled={disabled} onChange={(scene_id) => onChange({ anchor_kind: 'scene', scene_id, chapter_id: null, chapter_no: null, boundary: value.boundary })} /> : (
        <InputNumber
          aria-label="计划章节号"
          placeholder="输入计划章节号"
          min={1}
          max={2_147_483_647}
          precision={0}
          disabled={disabled}
          value={value.chapter_no}
          onChange={(chapterNo) => onChange({ ...value, chapter_id: null, chapter_no: chapterNo })}
          style={{ width: '100%' }}
        />
      )}
      <Typography.Text type="secondary">{purpose === 'preview' ? '查看所选位置的有效设定。' : value.anchor_kind === 'scene' ? '绑定真实场景身份，开始前或结束后生效；重排须通过完整历史校验，不按场景序号复活。' : '设置变化在章节开始前生效，或在该章结束后生效。'}</Typography.Text>
      <Radio.Group
        aria-label={purpose === 'preview' ? '预览时点' : '生效时点'}
        disabled={disabled}
        value={value.boundary}
        onChange={(event) => {
          const boundary: unknown = event.target.value
          if (boundary === 'start' || boundary === 'end') onChange({ ...value, boundary })
        }}
        options={purpose === 'preview'
          ? [{ value: 'start', label: value.anchor_kind === 'scene' ? '场景开始前' : '章初' }, { value: 'end', label: value.anchor_kind === 'scene' ? '场景结束后' : '章末' }]
          : [{ value: 'start', label: value.anchor_kind === 'scene' ? '从本场景起' : '从本章起' }, { value: 'end', label: value.anchor_kind === 'scene' ? '本场景结束后' : '本章结束后' }]}
      />
    </Space>
  )
}

function SceneAnchorSelect({ value, onChange, disabled }: { value: number | null; onChange: (id: number) => void; disabled: boolean }) {
  const projectId = useProjectId()
  const [page, setPage] = useState(1)
  const load = useCallback((signal: AbortSignal) => sceneApi.directory(projectId, { offset: (page - 1) * 50, limit: 50 }, signal), [projectId, page])
  const result = useMentionQuery(projectId, `scene-anchor:${page}`, load, Number.isSafeInteger(projectId) && projectId > 0)
  const options = result.data?.items.map((scene) => ({ value: scene.id, label: `第 ${scene.chapter_no} 章 · 场景 ${scene.sort + 1} · ${scene.title || '未命名'}` })) ?? []
  if (value && !options.some((item) => item.value === value)) options.push({ value, label: `已绑定场景 #${value}（可能不在当前页）` })
  return <Space direction="vertical" size={4} style={{ width: '100%' }}><Select aria-label="选择关联场景" style={{ width: '100%' }} value={value ?? undefined} placeholder="选择真实场景" showSearch optionFilterProp="label" options={options} disabled={disabled} loading={result.loading} onChange={onChange} /><Pagination simple current={page} pageSize={50} total={result.data?.total ?? 0} onChange={setPage} />{result.error && <Alert type="error" message={result.error} action={<Button onClick={result.reload}>重试</Button>} />}</Space>
}
