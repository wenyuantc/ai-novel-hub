import { Form, Input, Radio, Select, Space, Typography } from 'antd'
import { useId } from 'react'
import { SETTING_ENTRY_TYPE_OPTIONS } from '../../api/settingFields'
import type { SettingEntryType } from '../../api/types'
import ChapterPositionInput from '../progressions/ChapterPositionInput'
import type { ChapterReference } from '../progressions/position'
import type { CategoryFilters } from './categoryQuery'

interface Props { value: CategoryFilters; chapters: readonly ChapterReference[]; disabled?: boolean; onChange: (patch: Partial<CategoryFilters>) => void }

/** 当前值包含有效计划变化；章节筛选只影响下拉字段的求值位置。 */
export default function CategoryResultFilters({ value, chapters, disabled = false, onChange }: Props) {
  const prefix = useId()
  return <Form component="div" layout="vertical" style={{ minWidth: 0 }}>
    <Form.Item label="求值位置" htmlFor={`${prefix}-mode`} style={{ marginBottom: 12 }}>
      <Radio.Group id={`${prefix}-mode`} aria-label="分类求值位置" value={value.position || value.invalidPosition ? 'history' : 'current'} disabled={disabled}
        onChange={(event) => { const mode: unknown = event.target.value; if (mode === 'current') onChange({ position: null }); else if (mode === 'history') onChange({ position: { anchor_kind: 'chapter', chapter_id: null, chapter_no: null, boundary: 'start' } }) }}
        options={[{ value: 'current', label: '当前（含有效计划变化）' }, { value: 'history', label: '按章节查看' }]} />
    </Form.Item>
    {(value.position || value.invalidPosition) && <div style={{ marginBottom: 16 }}>
      <ChapterPositionInput chapters={chapters} value={value.position ?? { anchor_kind: 'chapter', chapter_id: null, chapter_no: null, boundary: 'start' }} purpose="preview" disabled={disabled} onChange={(position) => onChange({ position })} />
    </div>}
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <div><Typography.Text>结果类型</Typography.Text><Select<SettingEntryType[]> mode="multiple" aria-label="筛选分类结果类型" placeholder="全部适用类型" allowClear value={value.types} options={SETTING_ENTRY_TYPE_OPTIONS} disabled={disabled}
        onChange={(types) => onChange({ types })} style={{ width: '100%', marginTop: 6 }} /></div>
      <label htmlFor={`${prefix}-search`}><Typography.Text>条目名称</Typography.Text>
        <Input.Search id={`${prefix}-search`} key={value.q} aria-label="搜索分类结果名称" defaultValue={value.q} allowClear placeholder="输入名称后搜索" enterButton="搜索" disabled={disabled} onSearch={(q) => onChange({ q })} style={{ marginTop: 6 }} />
      </label>
    </Space>
  </Form>
}
