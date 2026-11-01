import { can } from '../utils/permissions'
import { ReloadOutlined, SaveOutlined, UndoOutlined } from '@ant-design/icons'
import { Alert, App, Button, InputNumber, Result, Skeleton, Space, Switch, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { SystemSettingChanges, SystemSettingItem } from '../api/types'
import { StudioPage } from '../components/StudioUI'
import { useSystemSettings } from '../components/systemSettings/useSystemSettings'
import { useSettingsConfirmation } from '../components/systemSettings/useSettingsConfirmation'
import { useAuthStore } from '../stores/auth'
import { isBooleanSetting, MAX_OPEN_API_RATE_LIMIT_PER_MINUTE, settingIntegerMaximum, settingValue, SYSTEM_SETTING_HINTS, SYSTEM_SETTING_LABELS, SYSTEM_SETTING_SOURCE } from '../utils/systemSettings'
import { formatTime } from '../utils/time'
import './AdminSystemSettingsPage.css'

interface SettingRowProps {
  item: SystemSettingItem
  changes: SystemSettingChanges
  invalid: boolean
  disabled: boolean
  onBoolean: (value: boolean) => void
  onNumber: (value: number | null) => void
  onRestore: () => void
}

/** 六项安全设置的统一行，不把完整部署配置渲染到DOM。 */
export function SystemSettingRow({ item, changes, invalid, disabled, onBoolean, onNumber, onRestore }: SettingRowProps) {
  const change = changes[item.key]
  const resetting = Object.hasOwn(changes, item.key) && change === null
  const value = resetting ? item.deployment_value : change ?? item.value
  return <section className="system-setting-row" aria-labelledby={`setting-${item.key}`}>
    <div className="system-setting-description">
      <h3 id={`setting-${item.key}`}>{SYSTEM_SETTING_LABELS[item.key]} <Tag>{SYSTEM_SETTING_SOURCE[item.source]}</Tag>{resetting && <Tag color="warning">待恢复，尚未保存</Tag>}{change !== undefined && !resetting && <Tag color="processing">待保存</Tag>}</h3>
      <Typography.Paragraph type="secondary">{SYSTEM_SETTING_HINTS[item.key]}</Typography.Paragraph>
      <p className="system-setting-baseline">当前有效：{settingValue(item.value)} · 部署值：{settingValue(item.deployment_value)}（{SYSTEM_SETTING_SOURCE[item.deployment_source]}） · 代码默认：{settingValue(item.default_value)}</p>
    </div>
    <div className="system-setting-editor">
      {isBooleanSetting(item) ? <Switch checked={value === true} disabled={disabled} aria-label={SYSTEM_SETTING_LABELS[item.key]} checkedChildren="开启" unCheckedChildren="关闭" onChange={onBoolean} /> : <InputNumber aria-label={SYSTEM_SETTING_LABELS[item.key]} min={1} max={settingIntegerMaximum(item.key)} precision={0} value={invalid ? null : typeof value === 'number' ? value : item.value} disabled={disabled} status={invalid ? 'error' : undefined} onChange={onNumber} />}
      {invalid && <Typography.Text type="danger" role="alert">{item.key === 'open_api_rate_limit_per_minute' ? `请输入 1 至 ${MAX_OPEN_API_RATE_LIMIT_PER_MINUTE} 的整数` : '请输入大于 0 的整数'}</Typography.Text>}
      <Button size="small" type="link" icon={<UndoOutlined />} disabled={disabled || resetting || (!item.overridden && change === undefined && !invalid)} onClick={onRestore}>恢复部署值</Button>
    </div>
  </section>
}

function Settings({ actorId }: { actorId: number }) {
  const actor = useAuthStore((state) => state.user)
  const writable = can(actor, 'settings.update')
  const { state, controller } = useSystemSettings(actorId)
  const { modal } = App.useApp()
  const confirm = useSettingsConfirmation(modal, state.denied || !writable)
  const dirty = Object.keys(state.changes).length > 0 || state.invalidNumbers.length > 0
  const blocked = !writable || state.saving || state.loading || state.denied || state.reviewRequired
  const resetDraft = () => {
    if (!dirty) { void controller.reload(); return }
    confirm({ title: '放弃未保存修改并重新载入？', content: '当前草稿将被丢弃，重新读取最新有效配置。此操作不会修改服务器设置。', okText: '放弃修改并重新载入', cancelText: '保留草稿', onOk: () => controller.reload() })
  }
  const groups = [
    { title: '注册与外部能力', keys: ['registration_enabled', 'mcp_enabled'] },
    { title: '任务与自动分析', keys: ['task_max_concurrency', 'task_max_per_user', 'auto_analyze_enabled'] },
    { title: '开放 API', keys: ['open_api_rate_limit_per_minute'] },
  ]
  return <div className="admin-system-settings"><StudioPage title="系统设置" hint="只管理六项业务运行设置，保存后立即生效；部署密钥和数据库连接仍由运维管理。" action={<Button icon={<ReloadOutlined />} loading={state.loading} disabled={state.saving || state.denied} onClick={() => void controller.refresh()}>刷新有效配置</Button>}>
    {state.denied ? <Alert showIcon type="error" message="系统设置访问已停止" description="当前会话或管理员权限已失效，管理数据与未保存修改已清空。" action={<Link to="/projects">返回个人作品</Link>} /> : <Space direction="vertical" size={20} style={{ width: '100%', minWidth: 0 }}>
      <Alert type="info" showIcon message="后台覆盖优先于部署配置，部署配置优先于代码默认" description="恢复部署值只取消对应后台覆盖，不会修改环境变量或 .env。注册与 MCP 导航在页面可见时每 30 秒同步，切回前台立即同步；网络异常时界面可能暂时保留旧入口，后端仍执行最新限制。" />
      {state.error && <Alert showIcon type="error" message={state.error} action={<Button size="small" disabled={state.loading || state.saving} onClick={() => void controller.refresh()}>刷新核实</Button>} />}
      {state.notice && <Alert type={state.reviewRequired ? 'warning' : 'success'} showIcon message={state.notice} />}
      {state.reviewRequired && <Alert showIcon type="warning" message="保存已暂停，请先核实当前有效配置" description="刷新会保留草稿。若服务器配置已有变化，请放弃草稿重新载入后再编辑，避免静默覆盖其他管理员的修改。" action={<Button size="small" disabled={state.loading || state.saving} onClick={resetDraft}>重新载入</Button>} />}
      {!state.data ? state.loading ? <Skeleton active paragraph={{ rows: 8 }} /> : <Typography.Text type="secondary">尚未加载有效设置，请刷新重试；这不代表正在使用代码默认值。</Typography.Text> : <>
        <Typography.Text type="secondary">配置版本 #{state.data.revision} · {state.data.updated_at ? `最近保存：${formatTime(state.data.updated_at)}` : '尚未保存后台覆盖'} · 六项均无需重启</Typography.Text>
        {groups.map((group) => <section className="system-setting-group" key={group.title} aria-label={group.title}>
          <h2>{group.title}</h2>
          {state.data!.items.filter((item) => group.keys.includes(item.key)).map((item) => <SystemSettingRow key={item.key} item={item} changes={state.changes} invalid={!isBooleanSetting(item) && state.invalidNumbers.includes(item.key)} disabled={blocked}
            onBoolean={(value) => { if (isBooleanSetting(item)) controller.edit(item.key, value) }}
            onNumber={(value) => { if (!isBooleanSetting(item)) controller.editNumber(item.key, value) }}
            onRestore={() => confirm({ title: `恢复「${SYSTEM_SETTING_LABELS[item.key]}」的部署值？`, content: `保存后将采用部署值「${settingValue(item.deployment_value)}」。此操作先加入待保存修改，不会立即更改运行设置。`, okText: '加入待恢复修改', cancelText: '取消', onOk: () => controller.restore(item.key) })} />)}
        </section>)}
        <div className="system-settings-savebar">
          <Space wrap><Button type="primary" icon={<SaveOutlined />} loading={state.saving} disabled={blocked || !Object.keys(state.changes).length || state.invalidNumbers.length > 0} onClick={() => void controller.save()}>保存设置</Button><Button disabled={!dirty || state.saving || state.loading} onClick={resetDraft}>放弃修改</Button><Typography.Text type="secondary">{dirty ? '有未保存修改；关闭页面会丢弃草稿。' : '当前没有未保存修改。'}</Typography.Text></Space>
          {can(actor, 'audit.read') && <Link to="/admin/audit-logs">查看操作日志</Link>}
        </div>
      </>}
    </Space>}
  </StudioPage></div>
}

export default function AdminSystemSettingsPage() {
  const user = useAuthStore((state) => state.user)
  return user && can(user, 'settings.read') ? <Settings key={user.id} actorId={user.id} /> : <Result status="403" title="需要管理员权限" />
}
