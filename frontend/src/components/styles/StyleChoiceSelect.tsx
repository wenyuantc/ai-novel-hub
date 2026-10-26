import { Select } from 'antd'
import type { CSSProperties } from 'react'
import { useMemo } from 'react'
import type { StyleChoice } from '../../api/types'
import { useStyles } from '../../stores/styles'
import type { StyleContext } from '../../utils/styles'
import { styleChoiceOptions } from '../../utils/styles'

interface Props {
  value?: StyleChoice
  onChange?: (value: StyleChoice) => void
  /** 用于显示「继承」实际会用哪个风格 */
  context: Omit<StyleContext, 'styles'>
  /** 替换继承项文案（批量：按章节或项目默认） */
  inheritLabel?: string
  disabled?: boolean
  style?: CSSProperties
}

/** 生成时的临时风格选择：继承（显示解析结果）/ 不用风格 / 指定风格。 */
export default function StyleChoiceSelect({ value = 'inherit', onChange, context, inheritLabel, disabled, style }: Props) {
  const { styles, loading } = useStyles()
  const options = useMemo(() => styleChoiceOptions(styles, context, inheritLabel), [styles, context, inheritLabel])
  return (
    <Select<StyleChoice>
      showSearch
      optionFilterProp="label"
      value={value}
      onChange={(v) => onChange?.(v)}
      disabled={disabled}
      loading={loading}
      options={options}
      style={{ width: '100%', ...style }}
    />
  )
}
