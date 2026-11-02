import { Alert, Button, Card, Empty, Pagination, Select, Space, Table, Tag, Typography } from 'antd'
import type { BackupRunFilters, BackupRunOut, BackupRunStatus, BackupSource } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { formatTime } from '../../utils/time'
import type { BackupsState } from './adminBackupsController'
import { BACKUP_ARTIFACT_STATUS, BACKUP_CLEANUP_STATUS, BACKUP_RUN_STATUS, backupMailStatus, backupSize } from './backupDisplay'

interface Props { state: BackupsState; onQuery: (query: BackupRunFilters) => void; onDetail: (id: number) => void }
function Status({ run }: { run: BackupRunOut }) { return <Tag color={BACKUP_RUN_STATUS[run.status].color}>{BACKUP_RUN_STATUS[run.status].label}</Tag> }
function Copies({ run }: { run: BackupRunOut }) {
  return <Space direction="vertical" size={0}>{run.artifacts.map((item) => <span key={item.id}>{item.destination === 'local' ? '本地' : 'S3'}：{BACKUP_ARTIFACT_STATUS[item.status]} · {backupSize(item.size_bytes)}</span>)}</Space>
}
export default function BackupRuns({ state, onQuery, onDetail }: Props) {
  const mobile = useIsMobile()
  const { list, query } = state
  const page = query.page ?? 1
  const pageSize = query.page_size ?? 20
  const pagination = <Pagination current={page} pageSize={pageSize} total={list.data?.total ?? 0} showSizeChanger pageSizeOptions={[10, 20, 50, 100]} showTotal={(total) => `共 ${total} 次执行`} onChange={(next, size) => onQuery({ ...query, page: size === pageSize ? next : 1, page_size: size })} />
  return <Space direction="vertical" size={16} style={{ width: '100%', minWidth: 0 }}>
    <Space wrap>
      <Select aria-label="筛选备份状态" placeholder="全部状态" allowClear value={query.status} style={{ width: 150 }} options={Object.entries(BACKUP_RUN_STATUS).map(([value, { label }]) => ({ value, label }))} onChange={(status: BackupRunStatus | undefined) => onQuery({ ...query, status, page: 1 })} />
      <Select aria-label="筛选备份来源" placeholder="全部来源" allowClear value={query.source} style={{ width: 150 }} options={[{ value: 'manual', label: '手动' }, { value: 'scheduled', label: '定时' }]} onChange={(source: BackupSource | undefined) => onQuery({ ...query, source, page: 1 })} />
    </Space>
    {list.error && <Alert type="error" showIcon message={list.error} description="列表可能已过期，请刷新核实。" />}
    {mobile ? <>
      {list.loading && <Typography.Text role="status">正在刷新执行记录…</Typography.Text>}
      {!list.data?.items.length && !list.loading && <Empty description="暂无执行记录" />}
      {list.data?.items.map((run) => <Card key={run.id} size="small" title={<Space>#{run.id}<Status run={run} /></Space>} extra={<Button size="small" onClick={() => onDetail(run.id)}>明细</Button>}>
        <Space direction="vertical" size={8}>
          <Typography.Text>{run.source === 'manual' ? '手动' : '定时'} · {formatTime(run.created_at)}</Typography.Text>
          <Typography.Text>{run.database_engine} · {run.encrypted ? '已加密' : '未加密'}</Typography.Text>
          <Copies run={run} /><Typography.Text type="secondary">{BACKUP_CLEANUP_STATUS[run.cleanup_status]} · {backupMailStatus(run)}</Typography.Text>
        </Space>
      </Card>)}
    </> : <Table<BackupRunOut> rowKey="id" loading={list.loading} dataSource={list.data?.items ?? []} pagination={false} scroll={{ x: 1000 }} locale={{ emptyText: '暂无执行记录' }} columns={[
      { title: '执行', dataIndex: 'id', width: 100, render: (id: number) => <Button type="link" onClick={() => onDetail(id)}>#{id}</Button> },
      { title: '结果', key: 'status', width: 120, render: (_, run) => <Status run={run} /> },
      { title: '来源 / 受理时间', key: 'created', width: 185, render: (_, run) => <>{run.source === 'manual' ? '手动' : '定时'}<br />{formatTime(run.created_at)}</> },
      { title: '实际格式', key: 'format', width: 150, render: (_, run) => <>{run.database_engine}<br />{run.file_format} · {run.encrypted ? '已加密' : '未加密'}</> },
      { title: '存储副本', key: 'copies', width: 240, render: (_, run) => <Copies run={run} /> },
      { title: '清理 / 邮件', key: 'notification', width: 180, render: (_, run) => <>{BACKUP_CLEANUP_STATUS[run.cleanup_status]}<br />{backupMailStatus(run)}</> },
    ]} />}
    <div className="backup-pagination">{pagination}</div>
  </Space>
}
