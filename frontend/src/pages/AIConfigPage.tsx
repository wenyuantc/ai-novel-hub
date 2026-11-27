import { CloudDownloadOutlined, PlusOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  AutoComplete,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  aiConfigApi,
  EMBEDDING_HELP,
  embeddingDimension,
  MODEL_TYPE_LABELS,
  modelTypesFor,
  PROVIDER_LABELS,
  providersFor,
  THINKING_LABELS,
} from '../api/aiConfigs'
import { errorMessage } from '../api/client'
import type {
  AIConfig,
  AIConfigCreate,
  AIConfigTestResult,
  ModelInfo,
  ModelType,
  ProviderName,
  ThinkingLevel,
} from '../api/types'
import { useAIConfigStore } from '../stores/aiConfigs'
import VideoProbeModal from '../components/media/VideoProbeModal'
import UsagePriceModal from '../components/aiUsage/UsagePriceModal'
import { useAiUsageSession } from '../components/aiUsage/useAiUsageRequest'
import { formatTime } from '../utils/time'

interface FormValues {
  name: string
  provider: ProviderName
  base_url?: string
  api_key?: string
  model: string
  model_type: ModelType
  is_default: boolean
  thinking_level: ThinkingLevel
  context_window_k?: number | null
  max_output_k?: number | null
}

const THINKING_OPTIONS = (Object.keys(THINKING_LABELS) as ThinkingLevel[]).map((v) => ({
  value: v,
  label: THINKING_LABELS[v],
}))

/** 列表中的参数标签 */
function paramTags(c: AIConfig): string[] {
  // 思考与上下文参数仅作用于文本生成
  if (c.model_type !== 'text') return []
  const k = (v: number | null) => (v ? `${v}K` : '默认')
  return [`思考 ${THINKING_LABELS[c.thinking_level]}`, `上下文 ${k(c.context_window_k)}`, `输出 ${k(c.max_output_k)}`]
}

const MODEL_HINTS: Record<ModelType, Partial<Record<ProviderName, string>>> = {
  text: {
    openai_compat: '例如：gpt-4o-mini、deepseek-chat、qwen2.5:14b',
    anthropic: '例如：claude-sonnet-4-5',
    gemini: '例如：gemini-2.5-flash',
  },
  image: {
    openai_compat: '例如：gpt-image-1、dall-e-3',
    anthropic: '填写接口提供的图片模型名',
    gemini: '例如：imagen-4.0-generate-001',
  },
  video: {
    volcengine: '填写 Seedance 模型或推理接入点 ID',
    openai_compat: '填写兼容服务提供的视频模型名',
    minimax: '例如：MiniMax-H3',
    aliyun_wan: '例如：wan3.0-video',
  },
  embedding: {
    openai_compat: '例如：text-embedding-3-small、bge-m3、nomic-embed-text',
    anthropic: 'Anthropic 不提供向量模型',
    gemini: '例如：gemini-embedding-001、text-embedding-004',
  },
}

const MODEL_TYPE_COLORS: Record<ModelType, string> = { text: 'green', image: 'purple', embedding: 'cyan', video: 'blue' }

export default function AIConfigPage() {
  const { message } = App.useApp()
  const { configs, loading, load } = useAIConfigStore()
  const [editing, setEditing] = useState<AIConfig | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const usageSession = useAiUsageSession()
  const [priceTarget, setPriceTarget] = useState<{ identity: string; config: AIConfig } | null>(null)
  const priceConfig = priceTarget?.identity === usageSession?.key && priceTarget
    ? configs.find((item) => item.id === priceTarget.config.id && item.updated_at === priceTarget.config.updated_at)
    : undefined
  const [probeTarget, setProbeTarget] = useState<{ sessionKey: string; config: AIConfig } | null>(null)
  const probeConfig = probeTarget && probeTarget.sessionKey === usageSession?.key
    ? configs.find((item) => item.id === probeTarget.config.id && item.updated_at === probeTarget.config.updated_at)
    : undefined
  const [testing, setTesting] = useState<number | null>(null)
  const [testResults, setTestResults] = useState<Record<number, AIConfigTestResult>>({})
  const [form] = Form.useForm<FormValues>()
  const provider = Form.useWatch('provider', form)
  const modelType = Form.useWatch('model_type', form) ?? 'text'
  const [models, setModels] = useState<ModelInfo[]>([])
  const [fetchingModels, setFetchingModels] = useState(false)

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
      model_type: 'text',
      is_default: !configs.some((c) => c.model_type === 'text'),
      thinking_level: 'off',
      context_window_k: null,
      max_output_k: null,
    })
    setModels([])
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
      model_type: c.model_type,
      is_default: c.is_default,
      thinking_level: c.thinking_level,
      context_window_k: c.context_window_k,
      max_output_k: c.max_output_k,
    })
    setModels([])
    setModalOpen(true)
  }

  const handleSave = async () => {
    const v = await form.validateFields()
    const isText = v.model_type === 'text'
    const body: AIConfigCreate = {
      name: v.name.trim(),
      provider: v.provider,
      base_url: v.base_url?.trim() ? v.base_url.trim() : null,
      model: v.model.trim(),
      model_type: v.model_type,
      is_default: v.is_default,
      // 图片 / 向量模型不使用思考与上下文参数，统一清空
      thinking_level: isText ? v.thinking_level : 'off',
      // null = 清空恢复默认
      context_window_k: isText ? (v.context_window_k ?? null) : null,
      max_output_k: isText ? (v.max_output_k ?? null) : null,
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

  const handleFetchModels = async () => {
    const v = await form.validateFields(['provider', 'base_url'])
    const key = (form.getFieldValue('api_key') as string | undefined)?.trim() ?? ''
    setFetchingModels(true)
    try {
      const list = await aiConfigApi.listModels({
        provider: v.provider,
        base_url: v.base_url?.trim() ? v.base_url.trim() : null,
        api_key: key || null,
        // 编辑时 Key 留空则使用已保存的 Key
        config_id: editing?.id ?? null,
      })
      setModels(list)
      if (list.length) message.success(`已获取 ${list.length} 个模型`)
      else message.warning('接口未返回任何模型，请手动填写')
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setFetchingModels(false)
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
          <Space wrap>
            <Link to="/settings/ai-usage">AI 用量与费用</Link>
            <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增配置</Button>
          </Space>
        }
      >
        <Typography.Paragraph type="secondary">
          文本、图片、向量和视频模型可分别设置默认配置。API Key 在服务端加密存储，此处仅显示掩码。向量模型{EMBEDDING_HELP}。
        </Typography.Paragraph>
        <Table<AIConfig>
          rowKey="id"
          loading={loading}
          dataSource={configs}
          pagination={false}
          scroll={{ x: 960 }}
          columns={[
            {
              title: '配置',
              key: 'name',
              width: 240,
              render: (_, r) => (
                <Space direction="vertical" size={2}>
                  <Space size={6}>
                    <Typography.Text strong>{r.name}</Typography.Text>
                    <Tag
                      color={MODEL_TYPE_COLORS[r.model_type]}
                      bordered={false}
                      style={{ marginInlineEnd: 0 }}
                    >
                      {MODEL_TYPE_LABELS[r.model_type]}
                    </Tag>
                    {r.is_default && (
                      <Tag color="blue" bordered={false} style={{ marginInlineEnd: 0 }}>
                        默认
                      </Tag>
                    )}
                  </Space>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {PROVIDER_LABELS[r.provider]}
                  </Typography.Text>
                </Space>
              ),
            },
            {
              title: '模型 / 参数',
              key: 'model',
              render: (_, r) => (
                <Space direction="vertical" size={4}>
                  <Typography.Text style={{ wordBreak: 'break-all' }}>{r.model}</Typography.Text>
                  <Space size={4} wrap>
                    {paramTags(r).map((t) => (
                      <Tag key={t} bordered={false} style={{ marginInlineEnd: 0, fontSize: 12 }}>
                        {t}
                      </Tag>
                    ))}
                  </Space>
                </Space>
              ),
            },
            {
              title: '接入',
              key: 'endpoint',
              width: 240,
              render: (_, r) => (
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  {r.base_url ? (
                    <Typography.Text style={{ fontSize: 12 }} ellipsis={{ tooltip: r.base_url }}>
                      {r.base_url}
                    </Typography.Text>
                  ) : (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      官方默认地址
                    </Typography.Text>
                  )}
                  {r.api_key_masked ? (
                    <Typography.Text code style={{ fontSize: 12 }}>
                      {r.api_key_masked}
                    </Typography.Text>
                  ) : (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      未设置 Key
                    </Typography.Text>
                  )}
                </Space>
              ),
            },
            {
              title: '连通性',
              key: 'test',
              width: 160,
              render: (_, r) => {
                if (r.model_type === 'video') return <Button type="link" size="small" disabled={!usageSession}
                  onClick={() => usageSession && setProbeTarget({ sessionKey: usageSession.key, config: r })}>测试生成</Button>
                const t = testResults[r.id]
                if (!t) return <Typography.Text type="secondary">未测试</Typography.Text>
                return (
                  <Space direction="vertical" size={0}>
                    <Space size={4} wrap>
                      <Tag color={t.ok ? 'success' : 'error'} style={{ marginInlineEnd: 0 }}>
                        {t.ok ? '正常' : '失败'} · {t.latency_ms} ms
                      </Tag>
                      {t.ok && r.model_type === 'embedding' && embeddingDimension(t.message) !== null && (
                        <Tag color="cyan" bordered={false} style={{ marginInlineEnd: 0 }}>
                          维度 {embeddingDimension(t.message)}
                        </Tag>
                      )}
                    </Space>
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
              width: 200,
              render: (_, r) => (
                <Space size={0} wrap>
                  {r.model_type !== 'video' && <Button type="link" size="small" loading={testing === r.id}
                    onClick={() => void handleTest(r)}>测试</Button>}
                  <Button type="link" size="small" onClick={() => openEdit(r)}>
                    编辑
                  </Button>
                  {usageSession && <Button type="link" size="small" onClick={() => setPriceTarget({ identity: usageSession.key, config: r })}>
                    设置单价
                  </Button>}
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

      {usageSession && probeConfig && <VideoProbeModal key={usageSession.key + ':' + probeConfig.id}
        session={usageSession} config={probeConfig} onClose={() => setProbeTarget(null)} />}
      {usageSession && priceConfig && <UsagePriceModal key={`${usageSession.key}:${priceConfig.id}`} config={priceConfig}
        session={usageSession} onClose={() => setPriceTarget(null)} />}

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
          <Form.Item
            name="model_type"
            label="模型类型"
            rules={[{ required: true }]}
            extra={modelType === 'embedding' ? EMBEDDING_HELP : undefined}
          >
            <Radio.Group
              optionType="button"
              buttonStyle="solid"
              onChange={(event) => {
                const next = event.target.value as ModelType
                setModels([])
                if (!modelTypesFor(provider).includes(next)) form.setFieldValue('provider', providersFor(next)[0])
              }}
              options={modelTypesFor().map((t) => ({
                value: t,
                label: MODEL_TYPE_LABELS[t],
              }))}
            />
          </Form.Item>
          <Form.Item name="provider" label="接口类型" rules={[{ required: true }]}>
            <Select
              onChange={() => setModels([])}
              options={providersFor(modelType).map((p) => ({
                value: p,
                label: PROVIDER_LABELS[p],
              }))}
            />
          </Form.Item>
          <Form.Item
            name="base_url"
            label="Base URL"
            extra={
              provider === 'aliyun_wan'
                ? '填写百炼工作空间所在地域的接口地址，例如 https://你的工作空间ID.cn-beijing.maas.aliyuncs.com/api/v1。'
                : modelType === 'video' && provider === 'openai_compat'
                ? '填写提供 /videos 的兼容服务地址，包含版本前缀（如 /v1）。'
                : provider === 'openai_compat'
                  ? '可填写中转地址或本地 LLM，例如 https://api.deepseek.com/v1、http://host.docker.internal:11434/v1（Docker 部署下的本机 Ollama，需部署方开启 ALLOW_PRIVATE_AI_ENDPOINTS）；留空使用 OpenAI 官方地址'
                : '一般留空使用官方地址；使用代理时填写'
            }
            rules={[{ required: modelType === 'video' && (provider === 'openai_compat' || provider === 'aliyun_wan'), message: '请输入视频服务地址' },
              { type: 'url', message: '请输入合法的 URL（以 http:// 或 https:// 开头）' }]}
          >
            <Input placeholder={modelType === 'video' ? undefined : 'https://api.openai.com/v1'} maxLength={500} />
          </Form.Item>
          <Form.Item
            name="api_key"
            label="API Key"
            extra={
              editing
                ? `当前：${editing.api_key_masked || '（未设置）'}；留空表示不修改`
                : provider === 'openai_compat' && modelType !== 'video'
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
          <Form.Item label="模型" required extra={models.length ? `可选 ${models.length} 个模型，也可手动输入` : undefined}>
            <Space.Compact style={{ width: '100%', display: 'flex' }}>
              <Form.Item
                name="model"
                noStyle
                rules={[{ required: true, whitespace: true, message: '请输入模型名' }]}
              >
                <AutoComplete
                  style={{ flex: 1, minWidth: 0 }}
                  placeholder={MODEL_HINTS[modelType][provider ?? 'openai_compat']}
                  maxLength={200}
                  options={models.map((m) => ({
                    value: m.id,
                    label: m.name && m.name !== m.id ? `${m.id}（${m.name}）` : m.id,
                  }))}
                  filterOption={(input, option) =>
                    String(option?.value ?? '')
                      .toLowerCase()
                      .includes(input.trim().toLowerCase())
                  }
                />
              </Form.Item>
              {modelType !== 'video' && <Button
                icon={<CloudDownloadOutlined />}
                loading={fetchingModels}
                onClick={() => void handleFetchModels()}
              >
                获取模型
              </Button>}
            </Space.Compact>
          </Form.Item>
          {modelType === 'text' && (
            <>
              <Form.Item
                name="thinking_level"
                label="思考等级"
                extra="关闭时不发送思考参数（部分模型默认即会思考，无法强制关闭）。xhigh / max 并非所有模型都支持，不支持时请改用 high"
              >
                <Select options={THINKING_OPTIONS} />
              </Form.Item>
              <Space size="middle" style={{ display: 'flex' }} align="start">
                <Form.Item name="context_window_k" label="上下文大小" extra="留空使用系统默认；用于截断章节前文">
                  <InputNumber
                    min={1}
                    max={2000}
                    precision={0}
                    addonAfter="K"
                    placeholder="如 128"
                    style={{ width: 200 }}
                  />
                </Form.Item>
                <Form.Item
                  name="max_output_k"
                  label="最大输出"
                  extra="留空使用接口默认"
                  dependencies={['context_window_k']}
                  rules={[
                    ({ getFieldValue }) => ({
                      validator: (_, value?: number | null) => {
                        const ctx = getFieldValue('context_window_k') as number | null | undefined
                        if (value != null && ctx != null && value >= ctx) {
                          return Promise.reject(new Error('需小于上下文大小'))
                        }
                        return Promise.resolve()
                      },
                    }),
                  ]}
                >
                  <InputNumber
                    min={1}
                    max={256}
                    precision={0}
                    addonAfter="K"
                    placeholder="如 8"
                    style={{ width: 200 }}
                  />
                </Form.Item>
              </Space>
            </>
          )}
          <Form.Item name="is_default" valuePropName="checked">
            <Checkbox>设为默认{MODEL_TYPE_LABELS[modelType]}</Checkbox>
          </Form.Item>
        </Form>
        {editing && <Alert type="info" showIcon message="修改接口类型或 Base URL 后建议重新测试连通性" />}
      </Modal>
    </div>
  )
}
