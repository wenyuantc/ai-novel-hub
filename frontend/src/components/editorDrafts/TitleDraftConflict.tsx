import { Alert, Button, Space, Typography } from 'antd'
import DraftConflictChoices from './DraftConflictChoices'

interface Props {
  local: string
  remote: string
  stamp: string
  disabled?: boolean
  onKeepLocal?: () => Promise<boolean>
  onUseRemote?: () => Promise<boolean>
  onExport?: () => void
}

export default function TitleDraftConflict({ local, remote, stamp, disabled, onKeepLocal, onUseRemote, onExport }: Props) {
  return (
    <Alert type="warning" showIcon message="章节标题需要核对" description={
      <Space direction="vertical" style={{ width: '100%', overflowWrap: 'anywhere' }}>
        <Typography.Text>服务端标题：{remote || '（空标题）'}</Typography.Text>
        <Typography.Text>本地标题：{local || '（空标题）'}</Typography.Text>
        {onUseRemote && <Typography.Text type="secondary">采用服务端版本后，本地标题会保留在草稿列表。</Typography.Text>}
        <DraftConflictChoices stamp={stamp} disabled={disabled} onKeepLocal={onKeepLocal} onUseRemote={onUseRemote} />
        {onExport && <Button size="small" onClick={onExport}>下载本地标题草稿</Button>}
      </Space>
    } />
  )
}
