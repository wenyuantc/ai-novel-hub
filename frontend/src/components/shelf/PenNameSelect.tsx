import { Button, Select, Space, Typography } from 'antd'
import { usePenNames } from '../../hooks/usePenNames'

interface Props {
  value?: number | null
  onChange?: (value: number | null) => void
  disabled?: boolean
  currentName?: string
}

/** 创建者的私人笔名选择器；其他人不应挂载此组件。 */
export default function PenNameSelect({ value, onChange, disabled, currentName }: Props) {
  const { items, loading, error, reload } = usePenNames()
  const options = items.map((item) => ({ value: item.id, label: item.name }))
  if (value != null && currentName && !items.some((item) => item.id === value)) {
    options.push({ value, label: currentName })
  }
  return (
    <Space direction="vertical" style={{ width: '100%' }} size={2}>
      <Select<number>
        aria-label="作品笔名"
        allowClear
        showSearch
        optionFilterProp="label"
        value={value ?? undefined}
        onChange={(v) => onChange?.(v ?? null)}
        disabled={disabled || loading || !!error}
        loading={loading}
        placeholder="不设置，使用账号名"
        options={options}
        notFoundContent="暂无笔名，可在我的作品中管理"
        style={{ width: '100%' }}
      />
      {error && <Typography.Text type="danger">笔名加载失败：{error} <Button type="link" size="small" onClick={reload}>重试</Button></Typography.Text>}
    </Space>
  )
}
