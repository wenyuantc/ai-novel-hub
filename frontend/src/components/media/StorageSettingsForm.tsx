import { Button, Checkbox, Col, Form, Input, InputNumber, Row, Select, Space } from 'antd'
import { useState } from 'react'
import type { MediaLocationInput, MediaStorageKind, MediaStorageLocation, MediaStoragePatch } from '../../api/types'
import { locationInput } from './storageDraft'

interface Props {
  location: MediaStorageLocation
  revision: number
  quotaBytes: number
  disabled: boolean
  onSave: (patch: MediaStoragePatch) => void
  onTest: (location: MediaLocationInput) => void
}

const GiB = 1024 ** 3

export default function StorageSettingsForm({ location, revision, quotaBytes, disabled, onSave, onTest }: Props) {
  const baseline = locationInput({ ...location.configuration, kind: location.kind })
  const [draft, setDraft] = useState<MediaLocationInput>(baseline)
  const [quota, setQuota] = useState<number | null>(quotaBytes / GiB)
  const [attempted, setAttempted] = useState(false)
  const values = locationInput(draft)
  const changedLocation = JSON.stringify(values) !== JSON.stringify(baseline)
  const quotaInBytes = Math.round((quota ?? 0) * GiB)
  const validQuota = Number.isSafeInteger(quotaInBytes) && quotaInBytes > 0
  const reuse = location.kind === 's3' && values.endpoint === baseline.endpoint
  const validEndpoint = values.kind === 'local' || /^https:\/\/[^/\s]+/i.test(values.endpoint ?? '')
  const requireAccessKey = !reuse || !location.has_access_key
  const requireSecretKey = !reuse || !location.has_secret_key

  function change<K extends keyof MediaLocationInput>(name: K, value: MediaLocationInput[K]) {
    setDraft(old => ({ ...old, [name]: value }))
  }

  function submit(test: boolean) {
    if (disabled) return
    setAttempted(true)
    if (values.kind === 's3' && (!validEndpoint || !values.bucket || (requireAccessKey && !values.access_key) || (requireSecretKey && !values.secret_key))) return
    if (test) { onTest(values); return }
    if (!validQuota || (!changedLocation && quotaInBytes === quotaBytes)) return
    onSave({ revision, ...(changedLocation ? { location: values } : {}), ...(quotaInBytes !== quotaBytes ? { default_quota_bytes: quotaInBytes } : {}) })
  }

  function input(name: 'endpoint' | 'region' | 'bucket' | 'prefix', label: string) {
    const message = name === 'bucket' && !values.bucket ? '请输入存储桶名称' : name === 'endpoint' && !validEndpoint ? '请输入 HTTPS 端点地址' : undefined
    return <Form.Item label={label} htmlFor={'media-' + name} validateStatus={attempted && message ? 'error' : undefined} help={attempted && message}>
      <Input id={'media-' + name} value={draft[name] ?? ''} placeholder={name === 'region' ? 'us-east-1' : undefined} onChange={e => change(name, e.target.value)} />
    </Form.Item>
  }
  function secret(name: 'access_key' | 'secret_key', label: string, required: boolean) {
    const missing = attempted && required && !draft[name]
    return <Form.Item label={label} htmlFor={'media-' + name} validateStatus={missing ? 'error' : undefined}
      help={missing ? '请填写当前端点的凭据' : undefined} extra={required ? undefined : '已设置。留空沿用当前端点的凭据。'}>
      <Input.Password id={'media-' + name} autoComplete="new-password" value={draft[name] ?? ''} onChange={e => change(name, e.target.value)} />
    </Form.Item>
  }
  return <Form layout="vertical" disabled={disabled} onFinish={() => submit(false)}>
    <Row gutter={20}>
      <Col xs={24} sm={12}><Form.Item label="每人存储上限（GiB）" htmlFor="media-quota"
        validateStatus={attempted && !validQuota ? 'error' : undefined} help={attempted && !validQuota ? '请输入有效的正数' : undefined}>
        <InputNumber id="media-quota" style={{ width: '100%' }} value={quota} min={0} onChange={setQuota} />
      </Form.Item></Col>
      <Col xs={24} sm={12}><Form.Item label="新文件存储位置" htmlFor="media-kind">
        <Select<MediaStorageKind> id="media-kind" value={draft.kind} onChange={v => change('kind', v)}
          options={[{ value: 'local', label: '本地目录' }, { value: 's3', label: 'S3 兼容对象存储' }]} />
      </Form.Item></Col>
    </Row>
    {draft.kind === 's3' && <>
      {input('endpoint', 'S3 端点')}
      <Row gutter={20}>
        <Col xs={24} sm={12}>{input('bucket', '存储桶（Bucket）')}</Col>
        <Col xs={24} sm={12}>{input('region', '区域（Region）')}</Col>
      </Row>
      {input('prefix', '对象前缀')}
      <Form.Item><Checkbox checked={draft.path_style ?? true} onChange={e => change('path_style', e.target.checked)}>使用路径式寻址（Path style）</Checkbox></Form.Item>
      <Row gutter={20}>
        <Col xs={24} sm={12}>{secret('access_key', '访问密钥 ID', requireAccessKey)}</Col>
        <Col xs={24} sm={12}>{secret('secret_key', '访问密钥（Secret）', requireSecretKey)}</Col>
      </Row>
    </>}
    <Space wrap>
      <Button type="primary" htmlType="submit" disabled={disabled || (!changedLocation && quotaInBytes === quotaBytes)}>保存设置</Button>
      <Button onClick={() => submit(true)} disabled={disabled}>测试连接</Button>
    </Space>
  </Form>
}
