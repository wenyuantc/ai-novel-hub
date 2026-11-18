import { Button, Space, Tag, Typography } from 'antd'

type LocalStatus = 'saving' | 'saved' | 'error'
type SyncStatus = 'pending' | 'saving' | 'saved' | 'offline' | 'error' | 'review' | 'restricted' | 'recovered'

interface Props {
  local?: LocalStatus
  sync: SyncStatus
  notice?: string
  onRetry?: () => void
  retryText?: string
  disabled?: boolean
}

const SYNC_STATUS: Record<SyncStatus, { text: string; color?: string }> = {
  pending: { text: '待保存' },
  saving: { text: '保存中', color: 'processing' },
  saved: { text: '已保存', color: 'success' },
  offline: { text: '离线待同步', color: 'warning' },
  error: { text: '保存失败', color: 'error' },
  review: { text: '待核对', color: 'warning' },
  restricted: { text: '权限受限' },
  recovered: { text: '恢复稿待保存', color: 'warning' },
}

const LOCAL_STATUS: Record<LocalStatus, { text: string; color?: string }> = {
  saving: { text: '正在保留草稿' },
  saved: { text: '本地已保留' },
  error: { text: '本地草稿保留失败', color: 'error' },
}

export default function EditorSaveStatus({ local, sync, notice, onRetry, retryText = '重试保存', disabled }: Props) {
  const server = SYNC_STATUS[sync]
  const device = local ? LOCAL_STATUS[local] : undefined
  return (
    <Space size={[4, 4]} wrap>
      <span role="status">
        <Tag color={server.color}>{server.text}</Tag>
        {device && <Tag color={device.color}>{device.text}</Tag>}
        {notice && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>{notice}</Typography.Text>}
      </span>
      {onRetry && <Button size="small" type="link" disabled={disabled || sync === 'saving'} onClick={onRetry}>{retryText}</Button>}
    </Space>
  )
}
