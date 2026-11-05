import { InputNumber, Radio, Select, Space, Typography } from 'antd'
import type { ProgressionPositionInput } from '../../api/types'
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
          if (kind === 'chapter' || kind === 'planned') {
            onChange({ anchor_kind: kind, chapter_id: null, chapter_no: null, boundary: value.boundary })
          }
        }}
        options={[{ value: 'chapter', label: '已有章节' }, { value: 'planned', label: '计划章节' }]}
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
      ) : (
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
      <Typography.Text type="secondary">{purpose === 'preview' ? '查看所选位置的有效设定。' : '设置变化在章节开始前生效，或在该章结束后生效。'}</Typography.Text>
      <Radio.Group
        aria-label={purpose === 'preview' ? '预览时点' : '生效时点'}
        disabled={disabled}
        value={value.boundary}
        onChange={(event) => {
          const boundary: unknown = event.target.value
          if (boundary === 'start' || boundary === 'end') onChange({ ...value, boundary })
        }}
        options={purpose === 'preview'
          ? [{ value: 'start', label: '章初' }, { value: 'end', label: '章末' }]
          : [{ value: 'start', label: '从本章起' }, { value: 'end', label: '本章结束后' }]}
      />
    </Space>
  )
}
