import { CopyOutlined, EyeInvisibleOutlined, EyeOutlined, LoadingOutlined } from '@ant-design/icons'
import { App, Button, Input, Modal, Space, Tooltip, Typography } from 'antd'
import { useEffect, useMemo, useState } from 'react'
import { apiKeyApi } from '../../api/apiKeys'
import { errorMessage } from '../../api/client'
import type { ApiKey } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { createApiKeySecretSession } from '../../utils/apiKeySecrets'

interface Props {
  item: ApiKey
  disabled: boolean
}

/** 密钥仅在本行局部状态显露；离开、隐藏或撤销会废弃未完成的读取。 */
export default function ApiKeySecret({ item, disabled }: Props) {
  const { message } = App.useApp()
  const width = useModalWidth(520)
  const [secret, setSecret] = useState<string | null>(null)
  const [manualKey, setManualKey] = useState<string | null>(null)
  const [pending, setPending] = useState<'show' | 'copy' | null>(null)
  const session = useMemo(
    () => createApiKeySecretSession((signal) => apiKeyApi.reveal(item.id, signal).then((result) => result.key)),
    [item.id],
  )
  useEffect(() => () => session.cancel(), [session])

  const hide = () => {
    session.cancel()
    setSecret(null)
    setManualKey(null)
    setPending(null)
  }

  const perform = async (action: 'show' | 'copy', knownKey?: string) => {
    if (disabled) return
    setPending(action)
    try {
      const result = await session.run(action, knownKey)
      if (!result || !session.isCurrent(result)) return
      setPending(null)
      if (result.kind === 'visible') setSecret(result.key)
      else if (result.kind === 'manual') setManualKey(result.key)
      else {
        setManualKey(null)
        message.success('密钥已复制')
      }
    } catch (err) {
      setPending(null)
      message.error(errorMessage(err))
    }
  }

  const canHide = secret !== null || pending === 'show'
  const showLabel = `${canHide ? '隐藏' : '显示'}密钥「${item.name}」`

  return (
    <>
      <div className="api-key-secret">
        <code className="api-key-value">{disabled ? item.key_masked : secret ?? item.key_masked}</code>
        <Space size={2} className="api-key-secret-actions">
          <Tooltip title={showLabel}>
            <Button
              size="small"
              type="text"
              aria-label={showLabel}
              disabled={disabled}
              icon={pending === 'show' ? <LoadingOutlined spin /> : canHide ? <EyeInvisibleOutlined /> : <EyeOutlined />}
              onClick={() => canHide ? hide() : void perform('show')}
            />
          </Tooltip>
          <Tooltip title={`复制密钥「${item.name}」`}>
            <Button
              size="small"
              type="text"
              aria-label={`复制密钥「${item.name}」`}
              disabled={disabled || pending === 'show'}
              loading={pending === 'copy'}
              icon={<CopyOutlined />}
              onClick={() => void perform('copy', secret ?? undefined)}
            />
          </Tooltip>
        </Space>
      </div>
      {manualKey !== null && (
        <Modal
          open
          title="复制 API 密钥"
          width={width}
          onCancel={hide}
          destroyOnHidden
          footer={[
            <Button key="close" onClick={hide}>关闭并隐藏</Button>,
            <Button key="copy" type="primary" icon={<CopyOutlined />} loading={pending === 'copy'} onClick={() => void perform('copy', manualKey)}>
              复制密钥
            </Button>,
          ]}
        >
          <Typography.Paragraph>
            浏览器暂未允许自动复制。可选中下方完整密钥手动复制，或再次点击“复制密钥”。
          </Typography.Paragraph>
          <Input.TextArea
            readOnly
            aria-label="完整 API 密钥，可选中后手动复制"
            value={manualKey}
            autoSize={{ minRows: 2, maxRows: 5 }}
            onFocus={(event) => event.target.select()}
            onClick={(event) => event.currentTarget.select()}
            spellCheck={false}
          />
        </Modal>
      )}
    </>
  )
}
