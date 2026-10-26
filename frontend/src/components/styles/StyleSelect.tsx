import { Select } from 'antd'
import type { CSSProperties } from 'react'
import { useMemo } from 'react'
import { useStyles } from '../../stores/styles'
import { styleAssignOptions } from '../../utils/styles'

interface Props {
  value?: number | null
  onChange?: (value: number | null) => void
  disabled?: boolean
  /** 未选择时的文案（项目默认：不用风格；章节：继承项目默认…） */
  placeholder?: string
  style?: CSSProperties
}

/** 设定项的风格下拉（项目默认 / 本章风格）：只列具体风格，清空 = null。 */
export default function StyleSelect({ value, onChange, disabled, placeholder = '不用风格', style }: Props) {
  const { styles, loading } = useStyles()
  const options = useMemo(() => styleAssignOptions(styles), [styles])
  // 风格已不可见（被删除后外键置空前的瞬间等）时仍显示 id，避免下拉显示原始数字
  const known = value == null || styles.some((s) => s.id === value)
  return (
    <Select<number>
      allowClear
      showSearch
      optionFilterProp="label"
      value={value ?? undefined}
      onChange={(v) => onChange?.(v ?? null)}
      disabled={disabled}
      loading={loading}
      placeholder={placeholder}
      options={known || value == null ? options : [...options, { label: '其他', options: [{ value, label: `风格 #${value}` }] }]}
      style={{ width: '100%', ...style }}
    />
  )
}
