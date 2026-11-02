import { SaveOutlined } from '@ant-design/icons'
import { Alert, Button, Checkbox, Col, Form, Input, InputNumber, Row, Select, Skeleton, Space, Switch, Tag, Typography } from 'antd'
import type { BackupConfiguration, BackupSecretKey, BackupSettingsChanges, BackupSettingsOut } from '../../api/types'
import { formatTime } from '../../utils/time'
import type { AdminBackupsController, BackupsState } from './adminBackupsController'
import { BACKUP_SECRET_LABELS } from './backupSettings'

interface Props { state: BackupsState; controller: AdminBackupsController; onReload: () => void }
interface SecretProps { name: BackupSecretKey; configured: boolean; value: string | null | undefined; disabled: boolean; onChange: (value: string | null | undefined) => void }
function SecretField({ name, configured, value, disabled, onChange }: SecretProps) {
  return <Form.Item label={<span>{BACKUP_SECRET_LABELS[name]} <Tag>{configured ? '已设置' : '未设置'}</Tag></span>} htmlFor={`backup-${name}`} extra={name === 'backup_password' ? '留空保留；16至1024个字符，不去除首尾空格。请离线保存，旧文件仍使用原密码。' : '留空保留；读取接口和页面不回显已保存的凭据。'}>
    <Space direction="vertical" style={{ width: '100%' }}>
      <Input.Password id={`backup-${name}`} autoComplete="new-password" maxLength={1024} placeholder="留空保留已保存值" value={typeof value === 'string' ? value : ''} disabled={disabled || value === null} onChange={(e) => onChange(e.target.value)} />
      <Checkbox checked={value === null} disabled={disabled} onChange={(e) => onChange(e.target.checked ? null : undefined)}>保存时清除已保存的{BACKUP_SECRET_LABELS[name]}</Checkbox>
    </Space>
  </Form.Item>
}
function Editor({ data, changes, state, controller }: { data: BackupSettingsOut; changes: BackupSettingsChanges; state: BackupsState; controller: AdminBackupsController }) {
  const disabled = state.saving || state.reviewRequired || state.denied
  const effective: BackupConfiguration = { ...data, ...changes }
  const value = <K extends keyof BackupConfiguration>(key: K): BackupConfiguration[K] => effective[key]
  const secret = (name: BackupSecretKey) => <SecretField name={name} configured={data[`${name}_configured`]} value={changes[name]} disabled={disabled} onChange={(v) => controller.edit(name, v)} />
  const text = (key: 's3_endpoint' | 's3_region' | 's3_bucket' | 's3_prefix' | 'smtp_host' | 'smtp_from_address', label: string, placeholder?: string) => <Form.Item label={label} htmlFor={`backup-${key}`}><Input id={`backup-${key}`} value={value(key)} disabled={disabled} placeholder={placeholder} onChange={(e) => controller.edit(key, e.target.value)} /></Form.Item>
  const number = (key: 'local_keep' | 's3_keep' | 'smtp_port', label: string) => <Form.Item label={label} htmlFor={`backup-${key}`} validateStatus={state.invalidNumbers.includes(key) ? 'error' : undefined} help={state.invalidNumbers.includes(key) ? `请输入1至${key === 'smtp_port' ? 65535 : 1000}的整数` : undefined}><InputNumber id={`backup-${key}`} min={1} max={key === 'smtp_port' ? 65535 : 1000} precision={0} value={state.invalidNumbers.includes(key) ? null : value(key)} disabled={disabled} onChange={(v) => controller.editNumber(key, v)} /></Form.Item>
  const toggle = (key: 'schedule_enabled' | 'local_enabled' | 's3_enabled' | 'encryption_enabled', label: string) => <Form.Item label={label} htmlFor={`backup-${key}`}><Switch id={`backup-${key}`} checked={value(key)} disabled={disabled} aria-label={label} checkedChildren="开启" unCheckedChildren="关闭" onChange={(v) => controller.edit(key, v)} /></Form.Item>
  return <Form layout="vertical" className="backup-settings-form">
    <section aria-labelledby="backup-plan-title"><h2 id="backup-plan-title">计划与文件加密</h2>
      <Row gutter={24}><Col xs={24} sm={12}>{toggle('schedule_enabled', '每日定时备份')}</Col><Col xs={24} sm={12}><Form.Item label="执行时间（北京时间 / Asia/Shanghai）" htmlFor="backup-schedule_time"><Input id="backup-schedule_time" type="time" value={value('schedule_time')} disabled={disabled} onChange={(e) => controller.edit('schedule_time', e.target.value)} /></Form.Item></Col></Row>
      <Typography.Paragraph type="secondary">关闭定时后仍可手动执行。首次开启前需配置好存储、加密及失败邮件；时间修改安排下一未来时刻，不重复执行同一日计划。</Typography.Paragraph>
      {toggle('encryption_enabled', '备份文件加密')}{secret('backup_password')}
      {!value('encryption_enabled') && <Alert type="warning" showIcon message="之后受理的备份将不加密，包含整个平台的账号、配置及全部作品。" />}
    </section>
    <section aria-labelledby="backup-storage-title"><h2 id="backup-storage-title">存储与独立保留</h2>
      <Typography.Paragraph type="secondary">本地与 S3 可以分别开启；各自保留最近成功产物，默认各7份。保存份数不会立即删除旧文件，下一次该处成功后再清理。关闭目的地也不会删除存量。</Typography.Paragraph>
      <Row gutter={24}><Col xs={24} sm={12}>{toggle('local_enabled', '本地存储')}{number('local_keep', '本地保留份数')}</Col><Col xs={24} sm={12}>{toggle('s3_enabled', 'S3 兼容存储')}{number('s3_keep', 'S3 保留份数')}</Col></Row>
      <Typography.Paragraph type="secondary">本地保存到部署配置指定的持久目录；路径由运维管理。</Typography.Paragraph>
      {text('s3_endpoint', 'S3 HTTPS 端点', 'https://s3.example.com')}
      <Row gutter={24}><Col xs={24} sm={12}>{text('s3_region', 'S3 区域')}</Col><Col xs={24} sm={12}>{text('s3_bucket', 'S3 Bucket')}</Col></Row>
      {text('s3_prefix', 'S3 对象前缀')}{secret('s3_access_key')}{secret('s3_secret_key')}
    </section>
    <section aria-labelledby="backup-mail-title"><h2 id="backup-mail-title">失败邮件</h2>
      <Typography.Paragraph type="secondary">备份、已启用存储或清理异常时发送安全摘要，不附备份文件。只支持校验证书的 SSL / STARTTLS；私网地址需由部署显式许可。</Typography.Paragraph>
      {text('smtp_host', 'SMTP 主机', 'smtp.example.com')}
      <Row gutter={24}><Col xs={24} sm={12}>{number('smtp_port', 'SMTP 端口')}</Col><Col xs={24} sm={12}><Form.Item label="SMTP 安全方式" htmlFor="backup-smtp_security"><Select id="backup-smtp_security" value={value('smtp_security')} disabled={disabled} options={[{ value: 'ssl', label: 'SSL' }, { value: 'starttls', label: 'STARTTLS' }]} onChange={(v: 'ssl' | 'starttls') => controller.edit('smtp_security', v)} /></Form.Item></Col></Row>
      {secret('smtp_username')}{secret('smtp_password')}{text('smtp_from_address', '发件人邮箱')}
      <Form.Item label="接收人邮箱（最多10个）" htmlFor="backup-smtp_recipients" extra="输入邮箱后按回车添加，也可用逗号分隔。"><Select id="backup-smtp_recipients" mode="tags" tokenSeparators={[',', ';', ' ']} value={value('smtp_recipients')} disabled={disabled} placeholder="添加接收人邮箱" onChange={(recipients: string[]) => controller.edit('smtp_recipients', recipients)} /></Form.Item>
    </section>
  </Form>
}
export default function BackupSettingsForm({ state, controller, onReload }: Props) {
  const data = state.settings.data
  const dirty = Object.keys(state.changes).length > 0 || state.invalidNumbers.length > 0
  return <Space direction="vertical" size={16} style={{ width: '100%' }}>
    <Space wrap><Button onClick={() => void controller.refreshSettings()} loading={state.settings.loading} disabled={state.saving}>刷新核实配置</Button><Typography.Text type="secondary">刷新保留草稿；重新载入会丢弃未保存修改。</Typography.Text></Space>
    {state.settings.error && <Alert showIcon type="error" message={state.settings.error} />}
    {state.reviewRequired && <Alert showIcon type="warning" message="配置保存已暂停" description="版本冲突或提交结果未知。请核实当前配置后，放弃草稿并重新载入；不会自动覆盖或重发保存。" action={<Button onClick={onReload}>重新载入</Button>} />}
    {!data ? state.settings.loading ? <Skeleton active paragraph={{ rows: 8 }} /> : <Typography.Text type="secondary">尚未加载配置；这不代表服务器正在使用默认值。</Typography.Text> : <>
      <Typography.Text type="secondary">配置版本 #{data.revision} · {data.updated_at ? `最近保存 ${formatTime(data.updated_at)}` : '尚未保存'} · 保存后对新备份生效</Typography.Text>
      <Editor data={data} changes={state.changes} state={state} controller={controller} />
      <Space wrap><Button type="primary" icon={<SaveOutlined />} loading={state.saving} disabled={!dirty || state.reviewRequired || state.settings.loading || state.invalidNumbers.length > 0} onClick={() => void controller.save()}>保存备份配置</Button><Button disabled={!dirty || state.saving} onClick={onReload}>放弃修改</Button><Typography.Text type="secondary">{dirty ? '有未保存修改；秘密不会写入浏览器存储。' : '当前没有未保存修改。'}</Typography.Text></Space>
    </>}
  </Space>
}
