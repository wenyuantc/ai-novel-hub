import { Alert, Button, Descriptions, Skeleton, Space, Tag, Typography } from 'antd'
import type { BackupOverview as Overview } from '../../api/types'
import { formatTime } from '../../utils/time'
import type { BackupResource } from './adminBackupsController'
import { formatBackupSchedule } from './backupDisplay'

interface Props { resource: BackupResource<Overview>; onDetail: (id: number) => void }
export default function BackupOverview({ resource, onDetail }: Props) {
  const data = resource.data
  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    {resource.error && <Alert showIcon type="error" message={resource.error} description="概览未能刷新，以下内容可能已过期。" />}
    {!data ? resource.loading ? <Skeleton active paragraph={{ rows: 4 }} /> : <Typography.Text type="secondary">尚未取得备份状态，请刷新重试。</Typography.Text> : <>
      {!data.ready && <Alert showIcon type="warning" message="备份尚未就绪" description={data.readiness_errors.join('；') || '请检查存储、工具和加密配置。'} />}
      {!data.notification_ready && <Alert showIcon type="warning" message="失败邮件未就绪" description="手动备份仍可执行；开启定时前需配置好 SMTP 和接收人。后台始终保留真实执行结果。" />}
      {data.last_failure && <Alert showIcon type="error" message={`最近异常：备份 #${data.last_failure.id}`} description={data.last_failure.error_message ?? '备份、存储或清理发生异常，请查看明细。'} action={<Button size="small" onClick={() => onDetail(data.last_failure!.id)}>查看异常</Button>} />}
      <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
        <Descriptions.Item label="定时计划"><Tag>{data.schedule_enabled ? '已开启' : '已关闭'}</Tag>每天北京时间 {data.schedule_time}</Descriptions.Item>
        <Descriptions.Item label="下次计划">{data.next_scheduled_at ? formatBackupSchedule(data.next_scheduled_at) : '未安排'}</Descriptions.Item>
        <Descriptions.Item label="新备份加密"><Tag color={data.encryption_enabled ? 'success' : 'warning'}>{data.encryption_enabled ? '已开启' : '未加密'}</Tag></Descriptions.Item>
        <Descriptions.Item label="本地存储">{data.local_enabled ? `开启 · 保留 ${data.local_keep} 份` : '关闭'}</Descriptions.Item>
        <Descriptions.Item label="S3 存储">{data.s3_enabled ? `开启 · 保留 ${data.s3_keep} 份` : '关闭'}</Descriptions.Item>
        <Descriptions.Item label="配置版本">#{data.revision}</Descriptions.Item>
        <Descriptions.Item label="当前执行">{data.active_run ? <Button type="link" size="small" onClick={() => onDetail(data.active_run!.id)}>#{data.active_run.id} · {data.active_run.status === 'queued' ? '已受理' : '执行中'}</Button> : '空闲'}</Descriptions.Item>
        <Descriptions.Item label="最近完整成功">{data.last_success ? <Button type="link" size="small" onClick={() => onDetail(data.last_success!.id)}>#{data.last_success.id} · {formatTime(data.last_success.finished_at)}</Button> : '暂无'}</Descriptions.Item>
        <Descriptions.Item label="最近可用副本">{data.last_available ? <Button type="link" size="small" onClick={() => onDetail(data.last_available!.id)}>#{data.last_available.id} · 查看副本</Button> : '暂无'}</Descriptions.Item>
      </Descriptions>
    </>}
  </Space>
}
