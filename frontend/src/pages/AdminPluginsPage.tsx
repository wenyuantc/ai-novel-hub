import { ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Empty, List, Skeleton, Switch, Typography } from 'antd'
import { useRef, useState, useSyncExternalStore } from 'react'
import { adminPluginsApi, type AdminPlugin } from '../api/adminPlugins'
import { ApiError, currentRequestGeneration, errorMessage, watchRequestGeneration } from '../api/client'
import { StudioPage, StudioPanel } from '../components/StudioUI'
import { useSettingsConfirmation } from '../components/systemSettings/useSettingsConfirmation'
import { useAuthorizationScope } from '../hooks/useAuthorizationScope'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { authorizationKey, can } from '../utils/permissions'
import { formatTime } from '../utils/time'

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

function PluginList({ writable }: { writable: boolean }) {
  const { modal } = App.useApp()
  const confirm = useSettingsConfirmation(modal, !writable)
  const { data, loading, error, reload, setData } = useLoader(adminPluginsApi.list)
  const [notice, setNotice] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const [saving, setSaving] = useState<string | null>(null)
  const pending = useRef(false)
  const access = useAuthorizationScope('plugins.update')
  function refresh() {
    setUncertain(false)
    setNotice('')
    reload()
  }
  async function save(item: AdminPlugin) {
    if (!data || !access.allowed() || pending.current || uncertain || loading || error) return
    pending.current = true
    const generation = currentRequestGeneration()
    const ticket = access.begin()
    const isCurrent = () => generation === currentRequestGeneration() && access.current(ticket)
    setSaving(item.id)
    setNotice('')
    try {
      const updated = await adminPluginsApi.setEnabled(item, !item.enabled, { isCurrent })
      if (!isCurrent()) return
      setData(() => ({ items: data.items.map((row) => row.id === updated.id ? updated : row) }), { invalidatePending: true })
      const auth = useAuthStore.getState()
      const enabled = new Set(auth.enabledPlugins)
      if (updated.enabled) enabled.add(updated.id)
      else enabled.delete(updated.id)
      auth.applyConfig({ registration_enabled: auth.registrationEnabled, mcp_enabled: auth.mcpEnabled, enabled_plugins: [...enabled] })
      void auth.refreshConfig()
    } catch (err) {
      if (!isCurrent()) return
      if (err instanceof ApiError && err.status === 409) {
        setNotice('状态已被其他管理员修改，请核对最新状态。')
        reload()
      } else {
        setUncertain(true)
        setNotice(errorMessage(err) + '。请刷新列表核实状态后再操作。')
      }
    } finally {
      pending.current = false
      if (isCurrent()) setSaving(null)
    }
  }
  function toggle(item: AdminPlugin) {
    if (!item.enabled) { void save(item); return }
    confirm({
      title: '停用「' + item.name + '」？',
      content: '入口将对所有作者关闭，运行中和排队的插件任务会被取消，已有数据保留。',
      okText: '停用插件', cancelText: '保持启用', okButtonProps: { danger: true }, autoFocusButton: 'cancel',
      onOk: () => save(item),
    })
  }
  return <StudioPage title="功能插件" action={<Button icon={<ReloadOutlined />} loading={loading} disabled={!!saving} onClick={refresh}>刷新列表</Button>}>
    {notice && <Alert showIcon type="warning" message={notice} />}
    {error && <Alert showIcon type="error" message={error} />}
    {loading && !data ? <Skeleton active /> : data && !error && <StudioPanel>
      <List dataSource={data.items} locale={{ emptyText: <Empty description="暂无已登记的功能插件" /> }}
        renderItem={(item) => <List.Item style={{ flexWrap: 'wrap', gap: 16 }}>
          <div style={{ flex: '1 1 220px', minWidth: 0, overflowWrap: 'anywhere' }}>
            <Typography.Title level={4}>{item.name}</Typography.Title>
            <Typography.Paragraph>{item.description}</Typography.Paragraph>
            <Typography.Text type="secondary">版本 {item.version} · {item.updated_at ? '最近变更 ' + formatTime(item.updated_at) : '尚无变更记录'}</Typography.Text>
          </div>
          <Switch aria-label={item.name + '启用状态'} checked={item.enabled} checkedChildren="已启用" unCheckedChildren="已停用"
            loading={saving === item.id} disabled={!writable || !!saving || loading || !!error || uncertain}
            onChange={() => toggle(item)} />
        </List.Item>} />
    </StudioPanel>}
  </StudioPage>
}

export default function AdminPluginsPage() {
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  const actor = useAuthStore((state) => state.user)
  if (!actor || !can(actor, 'plugins.read')) return null
  return <PluginList key={[generation, actor.id, actor.created_at, authorizationKey(actor)].join('/')} writable={can(actor, 'plugins.update')} />
}
