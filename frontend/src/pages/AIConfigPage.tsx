import { PlusOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useEffect, useState } from 'react'
import { aiConfigApi, PROVIDER_LABELS } from '../api/aiConfigs'
import { errorMessage } from '../api/client'
import type { AIConfig, AIConfigCreate, AIConfigTestResult, ProviderName } from '../api/types'
import { useAIConfigStore } from '../stores/aiConfigs'
import { formatTime } from '../utils/time'

interface FormValues {
  name: string
  provider: ProviderName
  base_url?: string
  api_key?: string
  model: string
  is_default: boolean
}

const MODEL_HINTS: Record<ProviderName, string> = {
  openai_compat: '例如：gpt-4o-mini、deepseek-chat、qwen2.5:14b',
  anthropic: '例如：claude-sonnet-4-5',
  gemini: '例如：gemini-2.5-flash',
}

export default function AIConfigPage() {
  const { message } = App.useApp()
  const { configs, loading, load } = useAIConfigStore()
  const [editing, setEditing] = useState<AIConfig | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState<number | null>(null)
  const [testResults, setTestResults] = useState<Record<number, AIConfigTestResult>>({})
  const [form] = Form.useForm<FormValues>()
  const provider = Form.useWatch('provider', form)

  useEffect(() => {
    void load(true)
  }, [load])

  const refresh = () => load(true)

  const openCreate = () => {
    setEditing(null)
    form.setFieldsValue({
      name: '',
      provider: 'openai_compat',
      base_url: '',
      api_key: '',
      model: '',
      is_default: configs.length === 0,
    })
    setModalOpen(true)
  }

  const openEdit = (c: AIConfig) => {
    setEditing(c)
    form.setFieldsValue({
      name: c.name,
      provider: c.provider,
      base_url: c.base_url ?? '',
      api_key: '',
      model: c.model,
      is_default: c.is_default,
    })
    setModalOpen(true)
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    const body: AIConfigCreate = {
      name: v.name.trim(),
      provider: v.provider,
      base_url: v.base_url?.trim() ? v.base_url.trim() : null,
      model: v.model.trim(),
      is_default: v.is_default,
    }
    const key = v.api_key?.trim() ?? ''
    setSaving(true)
    try {
      if (editing) {
        // 编辑时 Key 留空表示不修改
        await aiConfigApi.update(editing.id, key ? { ...body, api_key: key } : body)
        message.success('已保存')
      } else {
        await aiConfigApi.create({ ...body, api_key: key })
        message.success('已添加')
      }
      setModalOpen(false)
      await refresh()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn()
      message.success(ok)
      await refresh()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const handleTest = async (c: AIConfig) => {
    setTesting(c.id)
    try {
      const r = await aiConfigApi.test(c.id)
      setTestResults((prev) => ({ ...prev, [c.id]: r }))
      if (r.ok) message.success(`连通成功（${r.latency_ms} ms）`)
      else message.error(`连通失败：${r.message}`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setTesting(null)
    }
  }

  return (
    <div style={{ maxWidth: 1200, margin: '0 auto', padding: 24 }}>
      <Card
        title="模型配置"
        extra={
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
            新增配置
          </Button>
        }
      >
        <Typography.Paragraph type="secondary">
          支持 OpenAI 兼容接口（含各类中转、DeepSeek、本地 Ollama 等）、Anthropic Claude 与 Google Gemini。API Key
          在服务端加密存储，此处仅显示掩码。生成时未指定配置则使用「默认」配置。
        </Typography.Paragraph>
        <Table<AIConfig>
          rowKey="id"
          loading={loading}
          dataSource={configs}
          pagination={false}
          scroll={{ x: 960 }}
          columns={[
            {
              title: '名称',
              dataIndex: 'name',
              render: (name: string, r) => (
                <Space>
                  {name}
                  {r.is_default && <Tag color="blue">默认</Tag>}
                </Space>
              ),
            },
            { title: '类型', dataIndex: 'provider', render: (p: ProviderName) => PROVIDER_LABELS[p] },
            { title: '模型', dataIndex: 'model' },
            {
              title: 'Base URL',
              dataIndex: 'base_url',
              ellipsis: true,
              render: (u: string | null) => u || <Typography.Text type="secondary">官方默认</Typography.Text>,
            },
            {
              title: 'API Key',
              dataIndex: 'api_key_masked',
              render: (k: string) => <Typography.Text code>{k || '（未设置）'}</Typography.Text>,
            },
            {
              title: '连通性',
              key: 'test',
              width: 200,
              render: (_, r) => {
                const t = testResults[r.id]
                if (!t) return <Typography.Text type="secondary">未测试</Typography.Text>
                return (
                  <Space direction="vertical" size={0}>
                    <Tag color={t.ok ? 'success' : 'error'}>
                      {t.ok ? '正常' : '失败'} · {t.latency_ms} ms
                    </Tag>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: t.message }}>
                      {t.message}
                    </Typography.Text>
                  </Space>
                )
              },
            },
            { title: '更新时间', dataIndex: 'updated_at', width: 150, render: (t: string) => formatTime(t) },
            {
              title: '操作',
              key: 'actions',
              fixed: 'right',
              width: 240,
              render: (_, r) => (
                <Space size={0} wrap>
                  <Button type="link" size="small" loading={testing === r.id} onClick={() => void handleTest(r)}>
                    测试
                  </Button>
                  <Button type="link" size="small" onClick={() => openEdit(r)}>
                    编辑
                  </Button>
                  {!r.is_default && (
                    <Button
                      type="link"
                      size="small"
                      onClick={() => void run(() => aiConfigApi.setDefault(r.id), '已设为默认')}
                    >
                      设为默认
                    </Button>
                  )}
                  <Popconfirm
                    title="删除该配置？"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => run(() => aiConfigApi.remove(r.id), '已删除')}
                  >
                    <Button type="link" size="small" danger>
                      删除
                    </Button>
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        title={editing ? '编辑配置' : '新增配置'}
        open={modalOpen}
        onOk={() => void handleSave()}
        onCancel={() => setModalOpen(false)}
        confirmLoading={saving}
        okText="保存"
        cancelText="取消"
        forceRender
      >
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, message: '请输入名称' }]}>
            <Input placeholder="例如：DeepSeek 主力" maxLength={100} />
          </Form.Item>
          <Form.Item name="provider" label="接口类型" rules={[{ required: true }]}>
            <Select
              options={(Object.keys(PROVIDER_LABELS) as ProviderName[]).map((p) => ({
                value: p,
                label: PROVIDER_LABELS[p],
              }))}
            />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="Base URL"
            extra={
              provider === 'openai_compat'
                ? '可填写中转地址或本地 LLM，例如 https://api.deepseek.com/v1、http://host.docker.internal:11434/v1（Docker 部署下的本机 Ollama，需部署方开启 ALLOW_PRIVATE_AI_ENDPOINTS）；留空使用 OpenAI 官方地址'
                : '一般留空使用官方地址；使用代理时填写'
            }
            rules={[{ type: 'url', message: '请输入合法的 URL（以 http:// 或 https:// 开头）' }]}
          >
            <Input placeholder="https://api.openai.com/v1" maxLength={500} />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            extra={
              editing
                ? `当前：${editing.api_key_masked || '（未设置）'}；留空表示不修改`
                : provider === 'openai_compat'
                  ? '本地 LLM（如 Ollama）可留空'
                  : undefined
            }
            rules={[
              {
                required: !editing && provider !== 'openai_compat',
                whitespace: true,
                message: '请输入 API Key',
              },
            ]}
          >
            <Input.Password placeholder={editing ? '留空不修改' : 'sk-...'} autoComplete="new-password" />
          </Form.Item>
          <Form.Item name="model" label="模型" rules={[{ required: true, whitespace: true, message: '请输入模型名' }]}>
            <Input placeholder={MODEL_HINTS[provider ?? 'openai_compat']} maxLength={200} />
          </Form.Item>
          <Form.Item name="is_default" valuePropName="checked">
            <Checkbox>设为默认配置</Checkbox>
          </Form.Item>
        </Form>
        {editing && (
          <Alert type="info" showIcon message="修改接口类型或 Base URL 后建议重新测试连通性" />
        )}
      </Modal>
    </div>
  )
}
