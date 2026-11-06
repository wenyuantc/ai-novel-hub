import { Input, InputNumber, Radio, Select, Space, Typography } from 'antd'
import { useId } from 'react'
import { LIFE_STATUS } from '../../api/characters'
import ChapterPositionInput from './ChapterPositionInput'
import type { ProgressionDraft, ProgressionFieldChoice } from './fields'
import type { ChapterReference } from './position'
import SettingFieldValueControl from '../settingFields/SettingFieldValueControl'
import { fieldValueFromRaw } from '../settingFields/valueDraft'
import { changeProgressionField } from './fields'
import type { SettingEntry } from '../../api/types'

interface Props {
  projectId: number
  draft: ProgressionDraft
  fields: readonly ProgressionFieldChoice[]
  chapters: readonly ChapterReference[]
  disabled: boolean
  onChange: (draft: ProgressionDraft) => void
  reference?: SettingEntry | null
}

/** 不生成 HTML form，允许嵌入角色编辑表单内的弹窗。 */
export default function ProgressionEditorFields({ projectId, draft, fields, chapters, disabled, onChange, reference }: Props) {
  const id = useId()
  const current = fields.find((item) => item.id === draft.field)
  const typed = current?.slot ? fieldValueFromRaw(current.slot.field_type, draft.value) : null
  const set = (patch: Partial<ProgressionDraft>) => onChange({ ...draft, ...patch })
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div>
        <label htmlFor={`${id}-field`}>变化字段</label>
        <Select
          id={`${id}-field`}
          aria-label="变化字段"
          placeholder="选择变化字段"
          value={draft.field ?? undefined}
          options={fields.map((item) => ({ value: item.id, label: `${item.label}${item.slot ? '（共享字段）' : item.custom_field_id ? '（本条目原有字段）' : ''}`, disabled: item.slot?.archived && item.id !== draft.field }))}
          onChange={(key: string) => {
            const selected = fields.find((item) => item.id === key)
            if (!selected) return
            onChange(changeProgressionField(draft, selected))
          }}
          disabled={disabled}
          style={{ width: '100%' }}
        />
      </div>
      <div>
        <Typography.Text>生效位置</Typography.Text>
        <ChapterPositionInput chapters={chapters} value={draft.position} disabled={disabled} onChange={(position) => set({ position })} />
      </div>
      <div>
        <Typography.Text>变化方式</Typography.Text>
        <div>
          <Radio.Group
            aria-label="变化方式"
            value={draft.operation}
            disabled={disabled}
            onChange={(event) => {
              const operation: unknown = event.target.value
              if (operation === 'append' || operation === 'replace') set({ operation })
            }}
            options={[
              { value: 'append', label: '追加', disabled: !current?.supportsAppend },
              { value: 'replace', label: '替换' },
            ]}
          />
        </div>
      </div>
      <div>
        <label htmlFor={`${id}-value`}>变化内容</label>
        {current?.slot && typed ? <SettingFieldValueControl projectId={projectId} slot={current.slot} value={typed} disabled={disabled} reference={reference ?? current.slot.reference} onChange={(value) => set({ value: value.value })} id={`${id}-value`} label="变化内容" /> : current?.control === 'life_status' ? (
          <Select
            id={`${id}-value`}
            aria-label="变化内容"
            placeholder="选择存活状态"
            value={typeof draft.value === 'string' ? draft.value || undefined : undefined}
            options={Object.entries(LIFE_STATUS).map(([value, details]) => ({ value, label: details.label }))}
            onChange={(value: string) => set({ value })}
            disabled={disabled}
            style={{ width: '100%' }}
          />
        ) : current?.control === 'short_text' ? (
          <Input id={`${id}-value`} value={typeof draft.value === 'string' ? draft.value : ''} disabled={disabled} onChange={(event) => set({ value: event.target.value })} />
        ) : (
          <Input.TextArea
            id={`${id}-value`}
            value={typeof draft.value === 'string' ? draft.value : ''}
            disabled={disabled}
            onChange={(event) => set({ value: event.target.value })}
            autoSize={{ minRows: 3, maxRows: 10 }}
          />
        )}
        {draft.operation === 'replace' && current?.control !== 'life_status' && (
          <Typography.Text type="secondary">留空可清除此字段在该位置的内容。</Typography.Text>
        )}
      </div>
      <div>
        <label htmlFor={`${id}-order`}>同一位置的顺序（越小越先）</label>
        <br />
        <InputNumber
          id={`${id}-order`}
          min={0}
          max={2_147_483_647}
          precision={0}
          value={draft.sort_order}
          disabled={disabled}
          onChange={(sort_order) => set({ sort_order })}
        />
      </div>
      <div>
        <label htmlFor={`${id}-note`}>备注（可选，最多 200 字）</label>
        <Input.TextArea id={`${id}-note`} value={draft.note} disabled={disabled} onChange={(event) => set({ note: event.target.value })} autoSize={{ minRows: 2, maxRows: 4 }} />
      </div>
    </Space>
  )
}
