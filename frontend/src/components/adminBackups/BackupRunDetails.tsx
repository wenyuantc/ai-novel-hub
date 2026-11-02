import { DownloadOutlined } from '@ant-design/icons'
import { Alert, Button, Card, Descriptions, Drawer, Empty, Skeleton, Space, Tag, Typography } from 'antd'
import type { BackupDestination, BackupRunOut } from '../../api/types'
import { formatTime } from '../../utils/time'
import type { BackupResource } from './adminBackupsController'
import { BACKUP_ARTIFACT_STATUS, BACKUP_CLEANUP_STATUS, BACKUP_RUN_STATUS, backupMailStatus, backupSize } from './backupDisplay'

interface Props {
  id: number | null; resource: BackupResource<BackupRunOut>; canDownload: boolean; downloading: string | null
  onClose: () => void; onRefresh: () => void; onDownload: (id: number, destination: BackupDestination) => void
}
export default function BackupRunDetails({ id, resource, canDownload, downloading, onClose, onRefresh, onDownload }: Props) {
  const run = resource.data
  return <Drawer title={`备份明细${id ? ` #${id}` : ''}`} open={id !== null} onClose={onClose} width={720} destroyOnHidden extra={<Button onClick={onRefresh} loading={resource.loading}>刷新明细</Button>}>
    {resource.error && <Alert showIcon type="error" message={resource.error} />}
    {!run ? resource.loading ? <Skeleton active /> : <Empty description="暂无明细" /> : <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
      <Descriptions bordered size="small" column={1}>
        <Descriptions.Item label="执行结果"><Tag color={BACKUP_RUN_STATUS[run.status].color}>{BACKUP_RUN_STATUS[run.status].label}</Tag>{run.error_message}</Descriptions.Item>
        <Descriptions.Item label="来源">{run.source === 'manual' ? `手动 · ${run.actor_username ?? '历史账号'}` : '定时计划'}</Descriptions.Item>
        <Descriptions.Item label="数据库 / 格式">{run.database_engine} · {run.file_format} · {run.encrypted ? '已加密' : '未加密'}</Descriptions.Item>
        <Descriptions.Item label="配置 / 数据库版本">#{run.settings_revision} / {run.schema_revision ?? '未记录'}</Descriptions.Item>
        <Descriptions.Item label="受理 / 开始 / 结束">{formatTime(run.created_at)} / {formatTime(run.started_at)} / {formatTime(run.finished_at)}</Descriptions.Item>
        <Descriptions.Item label="执行标识"><Typography.Text style={{ overflowWrap: 'anywhere' }}>{run.execution_key}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="保留清理">{BACKUP_CLEANUP_STATUS[run.cleanup_status]}{run.cleanup_error && ` · ${run.cleanup_error}`}</Descriptions.Item>
        <Descriptions.Item label="失败邮件">{backupMailStatus(run)}{run.notification_error && ` · ${run.notification_error}`}</Descriptions.Item>
      </Descriptions>
      <Typography.Text type="secondary">邮件“已受理”仅表示 SMTP 服务接收，不保证收件人已阅读；投递未知时不会自动重复发送。</Typography.Text>
      {run.artifacts.map((artifact) => <Card key={artifact.id} size="small" title={`${artifact.destination === 'local' ? '本地' : 'S3'}副本`}>
        <Space direction="vertical" style={{ width: '100%', minWidth: 0 }}>
          <Typography.Text>{BACKUP_ARTIFACT_STATUS[artifact.status]} · {backupSize(artifact.size_bytes)}</Typography.Text>
          {artifact.filename && <Typography.Text style={{ overflowWrap: 'anywhere' }}>{artifact.filename}</Typography.Text>}
          {artifact.checksum_sha256 && <Typography.Paragraph style={{ overflowWrap: 'anywhere', marginBottom: 0 }}>SHA-256：{artifact.checksum_sha256}</Typography.Paragraph>}
          {artifact.error_message && <Alert type="warning" showIcon message={artifact.error_message} />}
          {artifact.deleted_at && <Typography.Text type="secondary">清理时间：{formatTime(artifact.deleted_at)}</Typography.Text>}
          {canDownload && artifact.downloadable && <Button icon={<DownloadOutlined />} loading={downloading === `${run.id}:${artifact.destination}`} disabled={downloading !== null} onClick={() => onDownload(run.id, artifact.destination)}>下载{artifact.destination === 'local' ? '本地' : 'S3'}副本</Button>}
        </Space>
      </Card>)}
      <Alert showIcon type={run.encrypted ? 'info' : 'warning'} message={run.encrypted ? '恢复需要生成此文件时的备份密码' : '此文件未加密，请妥善保管'} description="整库文件包含所有账号、配置和作品。原 ENCRYPTION_KEY 必须单独保管；改变当前密码不会改变旧文件。" />
    </Space>}
  </Drawer>
}
