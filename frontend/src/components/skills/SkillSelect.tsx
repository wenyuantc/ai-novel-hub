import { Select } from 'antd'
import type { CSSProperties } from 'react'
import { useMemo } from 'react'
import { useSkills } from '../../stores/skills'
import { skillSelectOptions } from '../../utils/skills'

interface Props {
  value?: number | null
  onChange?: (value: number | null) => void
  disabled?: boolean
  placeholder?: string
  style?: CSSProperties
  'aria-label'?: string
}

/** 可选的 Skill（我的 + 全局），可清空；清空 = 不使用 Skill。 */
export default function SkillSelect({ value, onChange, disabled, placeholder = '不使用 Skill', style, ...rest }: Props) {
  const { skills, loading } = useSkills()
  const options = useMemo(() => skillSelectOptions(skills), [skills])
  return (
    <Select<number>
      aria-label={rest['aria-label'] ?? '选择 Skill'}
      allowClear
      showSearch
      optionFilterProp="label"
      value={value ?? undefined}
      onChange={(v) => onChange?.(v ?? null)}
      disabled={disabled}
      loading={loading}
      placeholder={placeholder}
      options={options}
      notFoundContent={loading ? '加载中…' : '还没有可用的 Skill'}
      style={{ width: '100%', ...style }}
    />
  )
}
