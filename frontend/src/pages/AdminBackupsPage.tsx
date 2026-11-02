import { PlayCircleOutlined, ReloadOutlined } from '@ant-design/icons'
import { Alert, Button, Modal, Result, Space, Switch, Tabs, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link, useBeforeUnload, useBlocker } from 'react-router-dom'
import { StudioPage, StudioPanel } from '../components/StudioUI'
import BackupOverview from '../components/adminBackups/BackupOverview'
import BackupRestoreGuide from '../components/adminBackups/BackupRestoreGuide'
import BackupRunDetails from '../components/adminBackups/BackupRunDetails'
import BackupRuns from '../components/adminBackups/BackupRuns'
import BackupSettingsForm from '../components/adminBackups/BackupSettingsForm'
import { useAdminBackups } from '../components/adminBackups/useAdminBackups'
import { useAuthStore } from '../stores/auth'
import { authorizationKey, can } from '../utils/permissions'
import './AdminBackupsPage.css'

function Backups({ actorId, scope }: { actorId: number; scope: string }) {
  const actor = useAuthStore((state) => state.user)
  const { state, controller } = useAdminBackups(actorId, scope)
  const [reloadOpen, setReloadOpen] = useState(false)
  const dirty = Object.keys(state.changes).length > 0 || state.invalidNumbers.length > 0
  const blocker = useBlocker(dirty && !state.denied)
  useBeforeUnload(useCallback((event) => { if (dirty) { event.preventDefault(); event.returnValue = '' } }, [dirty]))
  const overview = state.overview.data
  const configure = can(actor, 'backups.configure') && !state.denied
  const download = can(actor, 'backups.download') && !state.denied
  const reload = () => {
    if (dirty) setReloadOpen(true)
    else void controller.reloadSettings()
  }
  return <div className="admin-backups"><StudioPage title="整库备份" hint="保存整个平台的账号、配置与全部作品；按已保存配置执行，手动受理和下载开始均不代表操作已完成。" action={<Space wrap><Switch checked={state.autoRefresh} disabled={state.denied} aria-label="自动刷新备份状态" checkedChildren="自动刷新" unCheckedChildren="暂停刷新" onChange={(value) => controller.setAutoRefresh(value)} /><Button icon={<ReloadOutlined />} disabled={state.denied} onClick={controller.refreshAll}>刷新状态</Button></Space>}>
    {state.denied ? <Alert showIcon type="warning" message="备份访问已停止" description="当前会话或权限已失效，页面数据与秘密草稿已清空。" action={<Link to="/projects">返回个人作品</Link>} /> : <Space direction="vertical" size={24} style={{ width: '100%', minWidth: 0 }}>
      {state.error && <Alert type="error" showIcon message={state.error} />}
      {state.notice && <Alert type="info" showIcon message={state.notice} />}
      <StudioPanel><BackupOverview resource={state.overview} onDetail={(id) => controller.openDetail(id)} />
        {can(actor, 'backups.run') && <Space wrap style={{ marginTop: 16 }}><Button type="primary" icon={<PlayCircleOutlined />} loading={state.starting} disabled={!overview || (!state.manualUncertain && (!overview.ready || !!overview.active_run))} onClick={() => void controller.startBackup()}>{state.manualUncertain ? '核实原受理请求' : '立即备份'}</Button><Typography.Text type="secondary">定时关闭时仍可手动；一次只执行一份整库快照。</Typography.Text></Space>}
      </StudioPanel>
      <StudioPanel><Tabs items={[
        { key: 'runs', label: '执行记录', children: <BackupRuns state={state} onQuery={(query) => controller.setQuery(query)} onDetail={(id) => controller.openDetail(id)} /> },
        ...(configure ? [{ key: 'settings', label: '备份配置', children: <BackupSettingsForm state={state} controller={controller} onReload={reload} /> }] : []),
        { key: 'restore', label: '恢复说明', children: <BackupRestoreGuide /> },
      ]} /></StudioPanel>
    </Space>}
    <BackupRunDetails id={state.detailId} resource={state.detail} canDownload={download} downloading={state.downloading} onClose={controller.closeDetail} onRefresh={() => void controller.refreshDetail()} onDownload={(id, destination) => void controller.download(id, destination)} />
    <Modal title="放弃未保存的备份配置？" open={reloadOpen && configure} onCancel={() => setReloadOpen(false)} okText="放弃修改并重新载入" cancelText="保留草稿" onOk={() => { setReloadOpen(false); void controller.reloadSettings() }}>未保存的连接参数和密码输入将被清空，重新读取当前配置。服务器现有设置不受影响。</Modal>
    <Modal title="离开并放弃未保存修改？" open={blocker.state === 'blocked' && !state.denied} okText="放弃修改并离开" cancelText="继续编辑" onOk={() => { if (blocker.state === 'blocked') blocker.proceed() }} onCancel={() => { if (blocker.state === 'blocked') blocker.reset() }}>未保存的备份配置和秘密输入会被清空。已经受理的备份将继续执行。</Modal>
  </StudioPage></div>
}
export default function AdminBackupsPage() {
  const user = useAuthStore((state) => state.user)
  const scope = authorizationKey(user)
  return user && can(user, 'backups.read') ? <Backups key={`${user.id}:${scope}`} actorId={user.id} scope={scope} /> : <Result status="403" title="没有查看备份的权限" />
}
