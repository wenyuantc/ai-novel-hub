import { Alert, App, Select, Space, Typography } from 'antd'
import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { PROVIDER_LABELS } from '../api/aiConfigs'
import { errorMessage } from '../api/client'
import type { ModelType } from '../api/types'
import { useShallow } from 'zustand/react/shallow'
import { useAIModelSource } from '../hooks/useAIModelSource'
import { useAIConfigStore } from '../stores/aiConfigs'

interface Props {
  id?: string
  value?: number | null
  onChange?: (value: number | null) => void
  disabled?: boolean
  modelType?: ModelType
  /** 系列等独立作用域绝不借用当前作品的作者模型。 */
  ownOnly?: boolean
}

/**
 * 按模型类型选择配置；留空表示使用该类型默认配置。无配置时引导去配置页。
 * 作品内无本人该类型配置、但已获作者模型授权的编辑者：不显示警告，说明将回退作者默认配置（value 保持 null）。
 */
export default function AIConfigSelect({ id, value, onChange, disabled, modelType = 'text', ownOnly = false }: Props) {
  const { message } = App.useApp()
  const { loaded, loading, load } = useAIConfigStore()
  const configs = useAIConfigStore(useShallow((s) => s.configs.filter((c) => c.model_type === modelType)))
  const typeLabel = modelType === 'image' ? '图片' : modelType === 'embedding' ? '向量' : '文本'
  const scopedSource = useAIModelSource(modelType)
  const source = ownOnly ? 'actor' : scopedSource

  useEffect(() => {
    void load().catch((err: unknown) => message.error(errorMessage(err)))
  }, [load, message])

  // 回退作者配置时请求必须留空：本人配置被删除后残留的旧选择不能随请求发出。
  useEffect(() => {
    if (source === 'owner' && value != null) onChange?.(null)
  }, [source, value, onChange])

  if (source === 'owner') {
    return (
      <Alert
        type="info"
        showIcon
        message={`未配置本人${typeLabel}模型，将使用作者授权的默认${typeLabel}模型`}
        description="本次可能使用作者密钥并产生作者费用，实际来源以生成结果为准。"
      />
    )
  }

  if (loaded && configs.length === 0) {
    return (
      <Alert
        type="warning"
        showIcon
        message={`尚未配置${typeLabel}模型`}
        description={
          <span>
            请先前往 <Link to="/settings/ai">模型配置</Link> 添加一个{typeLabel}模型后再生成。
          </span>
        }
      />
    )
  }

  const defaultConfig = configs.find((c) => c.is_default)
  return (
    <Select<number>
      id={id}
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

