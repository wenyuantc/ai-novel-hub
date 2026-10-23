import { Alert, Select, Space, Typography } from 'antd'
import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { PROVIDER_LABELS } from '../api/aiConfigs'
import { useShallow } from 'zustand/react/shallow'
import { selectTextConfigs, useAIConfigStore } from '../stores/aiConfigs'

interface Props {
  value?: number | null
  onChange?: (value: number | null) => void
  disabled?: boolean
}

/** 选择生成所用的文本模型配置；留空表示使用默认配置。无配置时引导去配置页。 */
export default function AIConfigSelect({ value, onChange, disabled }: Props) {
  const { loaded, loading, load } = useAIConfigStore()
  const configs = useAIConfigStore(useShallow(selectTextConfigs))

  useEffect(() => {
    void load()
  }, [load])

  if (loaded && configs.length === 0) {
    return (
      <Alert
        type="warning"
        showIcon
        message="尚未配置文本模型"
        description={
          <span>
            请先前往 <Link to="/settings/ai">模型配置</Link> 添加一个文本模型后再生成。
          </span>
        }
      />
    )
  }

  const defaultConfig = configs.find((c) => c.is_default)
  return (
    <Select<number>
      style={{ width: '100%' }}
      allowClear
      loading={loading}
      disabled={disabled}
      value={value ?? undefined}
      onChange={(v) => onChange?.(v ?? null)}
      placeholder={defaultConfig ? `默认：${defaultConfig.name}（${defaultConfig.model}）` : '使用默认配置'}
      options={configs.map((c) => ({
        value: c.id,
        label: (
          <Space size={4}>
            <span>{c.name}</span>
            <Typography.Text type="secondary">
              {PROVIDER_LABELS[c.provider]} · {c.model}
              {c.is_default ? ' · 默认' : ''}
            </Typography.Text>
          </Space>
        ),
      }))}
    />
  )
}

