import { Alert, App, Form, Input, Modal, Radio, Select } from 'antd'
import { useState } from 'react'
import { apiKeyApi } from '../../api/apiKeys'
import { errorMessage } from '../../api/client'
import type { ApiKey, Project } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { buildApiKeyInput, buildApiKeyUpdate, hasApiKeyScopeChanged, type ApiKeyFormValues } from '../../utils/apiKeys'
import { formatTime, parseServerTime } from '../../utils/time'

interface Props {
  item: ApiKey | null
  projects: Project[]
  projectsLoading: boolean
  onClose: () => void
  onSaved: () => void
}

/** 创建和编辑平台密钥，只配置已授权的作品与有效期。 */
export default function ApiKeyFormModal({ item, projects, projectsLoading, onClose, onSaved }: Props) {
  const { message } = App.useApp()
  const width = useModalWidth(560)
  const [form] = Form.useForm<ApiKeyFormValues>()
  const [saving, setSaving] = useState(false)
  const allProjects = Form.useWatch('all_projects', form) ?? item?.all_projects ?? false
  const projectIds = Form.useWatch('project_ids', form) ?? item?.project_ids ?? []
  const scopeChanged = item === null || hasApiKeyScopeChanged({ all_projects: allProjects, project_ids: projectIds }, item)
  const expiry = Form.useWatch('expiry', form) ?? (item ? 'keep' : '90')

  const submit = async () => {
    let values: ApiKeyFormValues
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    setSaving(true)
    try {
      if (item) await apiKeyApi.update(item.id, buildApiKeyUpdate(values, item))
      else await apiKeyApi.create(buildApiKeyInput(values))
      // 创建响应的完整 Key 不放入列表；之后由所有者主动查看。
      message.success(item ? '密钥配置已保存' : '密钥已创建，可点击眼睛图标查看或复制')
      onSaved()
      onClose()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const expiryOptions = [
    ...(item ? [{ value: 'keep', label: item.expires_at ? `保持当前：${formatTime(item.expires_at)}` : '保持当前：不过期' }] : []),
    { value: '30', label: '30 天' },
    { value: '90', label: '90 天' },
    { value: '180', label: '180 天' },
    { value: 'custom', label: '指定到期时间' },
    { value: 'never', label: '不过期' },
  ]

  return (
    <Modal
      open
      title={item ? '编辑 API 密钥' : '创建 API 密钥'}
      width={width}
      onCancel={onClose}
      onOk={() => void submit()}
      okText={item ? '保存配置' : '创建密钥'}
      cancelText="取消"
      confirmLoading={saving}
      closable={!saving}
      keyboard={!saving}
      maskClosable={!saving}
      cancelButtonProps={{ disabled: saving }}
      destroyOnHidden
    >
      <Alert
        type="info"
        showIcon
        message="只读访问已授权作品"
        description="持有此密钥的应用可读取小说、章节和当前创作设定，可能包含尚未写入正文的剧情。"
        style={{ marginBottom: 20 }}
      />
      <Form<ApiKeyFormValues>
        form={form}
        layout="vertical"
        disabled={saving}
        initialValues={{
          name: item?.name ?? '',
          all_projects: item?.all_projects ?? false,
          project_ids: item?.project_ids ?? [],
          expiry: item ? 'keep' : '90',
          custom_expires_at: item?.expires_at ? parseServerTime(item.expires_at).format('YYYY-MM-DDTHH:mm') : undefined,
        }}
      >
        <Form.Item name="name" label="密钥名称" rules={[{ required: true, whitespace: true, message: '请输入密钥名称' }, { max: 100 }]}>
          <Input maxLength={100} placeholder="例如：个人阅读器、内容同步工具" autoComplete="off" />
        </Form.Item>
        <Form.Item name="all_projects" label="授权小说" extra={item ? '未调整范围保留原授权（含暂不可见作品）；更改范围按当前选择替换。' : undefined}>
          <Radio.Group>
            <Radio value={false}>指定小说</Radio>
            <Radio value>全部自己的小说</Radio>
          </Radio.Group>
        </Form.Item>
        {allProjects ? (
          <Alert type="warning" showIcon message="此密钥也可读取之后新建的小说。" style={{ marginTop: -8, marginBottom: 20 }} />
        ) : (
          <Form.Item name="project_ids" label="选择小说" rules={scopeChanged ? [{ required: true, type: 'array', min: 1, message: '至少选择一部小说' }] : []}>
            <Select
              mode="multiple"
              loading={projectsLoading}
              options={projects.map((p) => ({ value: p.id, label: p.title }))}
              optionFilterProp="label"
              placeholder={projectsLoading ? '正在加载小说…' : '选择允许第三方读取的小说'}
              notFoundContent={projectsLoading ? '正在加载…' : '暂无可选择的小说'}
              style={{ width: '100%' }}
            />
          </Form.Item>
        )}
        <Form.Item name="expiry" label="有效期" extra={item?.status === 'expired' ? '已过期的密钥可通过调整有效期恢复使用。' : undefined}>
          <Select options={expiryOptions} />
        </Form.Item>
        {expiry === 'custom' && (
          <Form.Item name="custom_expires_at" label="到期时间（本地时间）" rules={[{ required: true, message: '请选择到期时间' }]}>
            <Input type="datetime-local" step={60} />
          </Form.Item>
        )}
      </Form>
    </Modal>
  )
}
