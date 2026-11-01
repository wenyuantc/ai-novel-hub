import { can } from '../../utils/permissions'
import { useAuthorizationScope } from '../../hooks/useAuthorizationScope'
import { Alert, App, Form, Input, Modal, Select, Switch } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { promptApi } from '../../api/prompts'
import { STYLE_LIMITS } from '../../api/styles'
import { workshopApi } from '../../api/workshop'
import type { WorkshopSubmit } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useAuthStore } from '../../stores/auth'
import { formatIssue, issuesFromError } from '../../utils/prompts'
import { STYLE_DIMENSIONS, STYLE_FIELD_KEYS } from '../../utils/styles'
import WorkshopMetadataFields from './WorkshopMetadataFields'

interface Props {
  initial: WorkshopSubmit
  direct?: boolean
  onClose: () => void
  onSaved?: () => void
}
const styleLabels: Record<string, string> = {
  description: '风格描述', ...Object.fromEntries(STYLE_DIMENSIONS.map((d) => [d.key, d.label])),
  taboos: '禁用表达', sample: '示例段落', extra: '自由补充',
}
const styleLimit = (key: string) => key === 'description' ? STYLE_LIMITS.description
  : key === 'taboos' ? STYLE_LIMITS.taboos : key === 'sample' ? STYLE_LIMITS.sample : key === 'extra' ? STYLE_LIMITS.extra : STYLE_LIMITS.dimension

/** 仅打开时挂载，以 initial 为初值；更新的类型与模板 key 不可改变。 */
export default function ShareToWorkshopModal({ initial, direct = false, onClose, onSaved }: Props) {
  const { message } = App.useApp()
  const isAdmin = useAuthStore((s) => can(s.user, 'workshop.publish'))
  const scope = useAuthorizationScope(direct ? 'workshop.publish' : undefined, false, direct)
  const [form] = Form.useForm<WorkshopSubmit>()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const width = useModalWidth(780)
  const loader = useCallback(() => initial.kind === 'prompt' ? promptApi.list() : Promise.resolve([]), [initial.kind])
  const { data: templates = [], error: loadError } = useLoader(loader)
  const save = async () => {
    if (!scope.allowed()) return
    const ticket = scope.begin()
    let values: WorkshopSubmit
    try { values = await form.validateFields() } catch { return }
    if (!scope.current(ticket)) return
    setSaving(true)
    setError(null)
    try {
      const body: WorkshopSubmit = { ...initial, ...values, name: values.name.trim(), description: values.description.trim(), content: { ...initial.content, ...values.content } }
      const result = direct ? await workshopApi.publish(body) : await workshopApi.submit(body)
      if (!scope.current(ticket)) return
      message.success(result.status === 'active' || result.status === 'approved' ? '已发布到工坊，标记为官方' : '已提交，等待管理员审核')
      onSaved?.()
      onClose()
    } catch (err) {
      if (!scope.current(ticket)) return
      const issues = issuesFromError(err)
      setError([errorMessage(err), ...issues.map(formatIssue)].join('\n'))
    } finally {
      if (scope.current(ticket)) setSaving(false)
    }
  }
  return <Modal open title={direct ? '直接发布官方内容' : initial.item_id ? '提交更新' : '分享到工坊'} width={width}
    onCancel={() => { if (!saving) onClose() }} onOk={() => void save()} confirmLoading={saving} cancelButtonProps={{ disabled: saving }}
    okText={isAdmin ? '发布' : '提交审核'} cancelText="取消" maskClosable={false} destroyOnHidden>
    <Alert showIcon type="info" message={isAdmin ? '管理员提交直接发布，并标记为官方。' : initial.item_id ? '更新需要重新审核，通过前保留已发布内容和计数。' : '审核通过后，本实例的其他用户可以浏览和导入。'} style={{ marginBottom: 16 }} />
    {(error || loadError) && <Alert type="error" showIcon message={error ?? loadError} style={{ whiteSpace: 'pre-wrap', marginBottom: 16 }} />}
    <Form form={form} layout="vertical" initialValues={initial} disabled={saving}>
      <Form.Item name="name" label="分享名称" rules={[{ required: true, whitespace: true, message: '请输入分享名称' }, { max: 100 }]}><Input maxLength={100} showCount /></Form.Item>
      <Form.Item name="description" label="分享说明" rules={[{ max: 500 }]}><Input.TextArea maxLength={500} showCount autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
      <WorkshopMetadataFields disabled={saving} />
      <Form.Item name="is_anonymous" label="匿名分享" valuePropName="checked" extra="其他用户看到匿名署名，管理员仍可看到真实作者。"><Switch /></Form.Item>
      {initial.kind === 'prompt' && <>
        <Form.Item name="prompt_key" label="模板" rules={[{ required: true, message: '请选择模板' }]}>
          <Select showSearch optionFilterProp="label" disabled={!!initial.prompt_key || saving}
            options={templates.map((t) => ({ value: t.key, label: `${t.name}（${t.key}）` }))} />
        </Form.Item>
        <Alert type="info" message="保留当前模板所需的变量与锁定变量；发布和导入时都会按系统模板校验。" style={{ marginBottom: 12 }} />
        {(['system', 'user'] as const).map((part) => <Form.Item key={part} name={['content', part]} label={part === 'system' ? '系统段' : '用户段'} rules={[{ max: 20000 }]}>
          <Input.TextArea autoSize={{ minRows: 4, maxRows: 14 }} style={{ fontFamily: 'monospace' }} maxLength={20000} />
        </Form.Item>)}
      </>}
      {initial.kind === 'style' && STYLE_FIELD_KEYS.map((key) => <Form.Item key={key} name={['content', key]} label={styleLabels[key]} rules={[{ max: styleLimit(key) }]}>
        <Input.TextArea autoSize={{ minRows: 2, maxRows: 8 }} maxLength={styleLimit(key)} />
      </Form.Item>)}
    </Form>
  </Modal>
}
