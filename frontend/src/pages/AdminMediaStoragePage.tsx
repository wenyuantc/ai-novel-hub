import { ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, List, Skeleton, Tag, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { adminMediaStorageApi } from '../api/adminMediaStorage'
import { ApiError, currentRequestGeneration, errorMessage, watchRequestGeneration } from '../api/client'
import type { MediaConnectionTest, MediaStoragePatch } from '../api/types'
import { StudioPage, StudioPanel } from '../components/StudioUI'
import StorageSettingsForm from '../components/media/StorageSettingsForm'
import { useSettingsConfirmation } from '../components/systemSettings/useSettingsConfirmation'
import { useAuthorizationScope } from '../hooks/useAuthorizationScope'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { authorizationKey, can } from '../utils/permissions'
import { formatTime } from '../utils/time'
import { formatBytes } from '../utils/transfer'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose
const kindLabels = { local: '本地目录', s3: 'S3 对象存储' }
const stateLabels = { active: '当前使用', readonly: '只读', retired: '已退役' }
type Notice = { type: 'error' | 'warning' | 'success'; text: string }

function StoragePage({ writable }: { writable: boolean }) {
  const mounted = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const load = useCallback(() => adminMediaStorageApi.get({ isCurrent: () => mounted.current }), [])
  const { data, loading, error, reload, setData } = useLoader(load)
  const access = useAuthorizationScope('media_storage.configure')
  const { modal } = App.useApp()
  const confirm = useSettingsConfirmation(modal, !writable, data?.revision)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [checks, setChecks] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const active = data?.locations.find(row => row.state === 'active')
  const disabled = !writable || busy || loading || !!error

  async function execute(action: { patch: MediaStoragePatch } | { probe: MediaConnectionTest }) {
    if (disabled || pending.current || !access.allowed()) return
    pending.current = true
    setBusy(true)
    setNotice(null)
    const ticket = access.begin()
    const generation = currentRequestGeneration()
    const isCurrent = () => access.current(ticket) && generation === currentRequestGeneration()
    try {
      if ('probe' in action) {
        await adminMediaStorageApi.test(action.probe, { isCurrent })
        if (!isCurrent()) return
        if ('location_id' in action.probe) {
          const id = action.probe.location_id
          setChecks(previous => ({ ...previous, [id]: '连接正常' }))
        }
        setNotice({ type: 'success', text: '连接测试成功。' })
      } else {
        const saved = await adminMediaStorageApi.update(action.patch, { isCurrent })
        if (!isCurrent()) return
        setData(() => saved, { invalidatePending: true })
        setNotice({ type: 'success', text: '媒体存储设置已保存。' })
      }
    } catch (err) {
      if (!isCurrent()) return
      if ('probe' in action && 'location_id' in action.probe) {
        const id = action.probe.location_id
        setChecks(previous => ({ ...previous, [id]: '连接失败' }))
      }
      if ('patch' in action && err instanceof ApiError && err.status === 409) {
        setNotice({ type: 'warning', text: '其他管理员已更新配置，请核对最新设置后再修改。' })
        reload()
      } else {
        setNotice({ type: 'error', text: errorMessage(err) })
      }
    } finally {
      pending.current = false
      if (isCurrent()) setBusy(false)
    }
  }

  function save(patch: MediaStoragePatch) {
    if (disabled || !access.allowed()) return
    if (!patch.location) return execute({ patch })
    confirm({
      title: '切换媒体存储位置？', content: '新文件写入新位置，旧文件留在原位置并保持可读。',
      okText: '确认切换', cancelText: '取消', autoFocusButton: 'cancel', onOk: () => execute({ patch }),
    })
  }

  return <StudioPage title="媒体存储" action={<Button icon={<ReloadOutlined />} loading={loading} disabled={busy} onClick={() => { setNotice(null); reload() }}>刷新</Button>}>
    {notice && <Alert showIcon type={notice.type} message={notice.text} />}
    {error && <Alert showIcon type="error" message={error} />}
    {loading && !data && <Skeleton active />}
    {data && !error && <>
      <StudioPanel><h2>存储位置与占用</h2>
        <Typography.Paragraph>全站已占用 {formatBytes(data.total_bytes)}</Typography.Paragraph>
        <List dataSource={data.locations} rowKey="id" renderItem={row => <List.Item style={{ display: 'block', overflowWrap: 'anywhere' }}>
          <Typography.Text strong>{kindLabels[row.kind]} #{row.id}</Typography.Text> <Tag>{stateLabels[row.state]}</Tag>
          {row.kind === 's3' && <div>{[row.configuration.endpoint, row.configuration.bucket, row.configuration.prefix].filter(Boolean).join(' / ')}</div>}
          <div>{row.file_count} 个文件 · {formatBytes(row.size_bytes)}</div>
          {row.state !== 'retired' && writable && <Button type="link" size="small" disabled={disabled}
            aria-label={'检测位置 #' + row.id + ' 的连接'} onClick={() => void execute({ probe: { location_id: row.id } })}>检测此位置</Button>}
          {checks[row.id] && <Typography.Text role="status">{checks[row.id]}</Typography.Text>}
        </List.Item>} />
      </StudioPanel>
      {active && <StudioPanel><h2>存储设置</h2>
        <StorageSettingsForm key={data.revision} location={active} revision={data.revision} quotaBytes={data.default_quota_bytes}
          disabled={disabled} onSave={save} onTest={probe => execute({ probe })} />
      </StudioPanel>}
      <StudioPanel><h2>本地卷诊断</h2>
        {data.volume_mismatch && <Alert type="warning" showIcon message="各实例的媒体目录不是同一个共享卷" description="请检查各实例的媒体目录挂载。来自其他卷的媒体在当前实例不可用。" />}
        <List dataSource={data.volume_reports} rowKey="worker_key" locale={{ emptyText: '暂无实例上报卷信息' }}
          renderItem={report => <List.Item style={{ display: 'block', overflowWrap: 'anywhere' }}>
            <Typography.Text strong>{report.worker_key}</Typography.Text>
            <div>卷标识 {report.volume_id}</div><Typography.Text type="secondary">最近上报 {formatTime(report.seen_at)}</Typography.Text>
          </List.Item>} />
      </StudioPanel>
    </>}
  </StudioPage>
}

export default function AdminMediaStoragePage() {
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const actor = useAuthStore(state => state.user)
  if (!actor || !can(actor, 'media_storage.read')) return null
  return <StoragePage key={[generation, actor.id, actor.created_at, authorizationKey(actor)].join('/')} writable={can(actor, 'media_storage.configure')} />
}
