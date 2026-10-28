import { Alert, App, Form, Input, List, Modal, Radio, Space, Tabs, Tag, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { mcpApi } from '../../api/mcp'
import type { McpImportItem, McpPlugin, McpPluginCreate } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import {
  headerRow,
  parseMcpServersJson,
  pluginCreateBody,
  pluginUpdateBody,
  stdioNotice,
  type HeadersState,
  type PluginFormValues,
} from '../../utils/mcp'
import HeadersEditor from './HeadersEditor'

interface Props {
  /** null = 添加（表单 / 粘贴 JSON 两个 Tab）；否则编辑该插件 */
  plugin: McpPlugin | null
  onClose: () => void
  /** 有插件被创建或修改 */
  onSaved: (plugin: McpPlugin | null) => void
}

const JSON_PLACEHOLDER = `{
  "mcpServers": {
    "dictionary": {
      "type": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}`

/** 添加 / 编辑 MCP 插件。只支持远程 Streamable HTTP 与 SSE；请求头加密存储、不回显。 */
export default function McpPluginFormModal({ plugin, onClose, onSaved }: Props) {
  const { message } = App.useApp()
  const width = useModalWidth(640)
  const [form] = Form.useForm<PluginFormValues>()
  const [tab, setTab] = useState<'form' | 'json'>('form')
  const [headers, setHeaders] = useState<HeadersState>(plugin ? { mode: 'keep' } : { mode: 'edit', rows: [headerRow()] })
  const [headersError, setHeadersError] = useState<string | null>(null)
  const [jsonText, setJsonText] = useState('')
  const [jsonNotice, setJsonNotice] = useState<{ error?: string; stdio?: string[]; invalid?: { name: string; reason: string }[] } | null>(null)
  const [results, setResults] = useState<McpImportItem[] | null>(null)
  const [saving, setSaving] = useState(false)

  const submitForm = async () => {
    let values: PluginFormValues
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    const built = plugin ? pluginUpdateBody(plugin, values, headers) : null
    const created = plugin ? null : pluginCreateBody(values, headers)
    const problem = (built && 'error' in built && built.error) || (created && 'error' in created && created.error)
    if (problem) {
      setHeadersError(problem)
      return
    }
    setHeadersError(null)
    setSaving(true)
    try {
      const saved =
        plugin && built && 'body' in built
          ? await mcpApi.update(plugin.id, built.body)
          : await mcpApi.create((created as { body: McpPluginCreate }).body)
      message.success(plugin ? '已保存' : `已添加「${saved.name}」，可以测试连接并刷新工具`)
      onSaved(saved)
      onClose()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const submitJson = async () => {
    const parsed = parseMcpServersJson(jsonText)
    if (!parsed.ok) {
      setJsonNotice({ error: parsed.error })
      return
    }
    setJsonNotice({ stdio: parsed.stdio, invalid: parsed.invalid })
    if (Object.keys(parsed.servers).length === 0) return
    setSaving(true)
    try {
      const r = await mcpApi.import(parsed.servers)
      setResults(r.items)
      const created = r.items.filter((i) => i.status === 'created')
      if (created.length) {
        message.success(`已添加 ${created.length} 个插件`)
        onSaved(null)
      }
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const formBody = (
    <Form
      form={form}
      layout="vertical"
      initialValues={plugin ? { name: plugin.name, slug: plugin.slug, transport: plugin.transport, url: plugin.url } : { transport: 'streamable_http' }}
    >
      <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, message: '请输入名称' }]}>
        <Input maxLength={100} />
      </Form.Item>
      <Form.Item
        name="slug"
        label="标识"
        tooltip="工具在智能体中显示为 mcp__标识__工具名；留空时由名称生成（名称不是英文时请手动填写）"
        rules={[{ pattern: /^[a-z0-9][a-z0-9-]{0,23}$/, message: '只能使用小写字母、数字和连字符，以字母或数字开头，不超过 24 个字符' }]}
      >
        <Input maxLength={24} placeholder="例如 dictionary" />
      </Form.Item>
      <Form.Item name="transport" label="连接方式">
        <Radio.Group
          options={[
            { value: 'streamable_http', label: 'Streamable HTTP' },
            { value: 'sse', label: 'SSE（旧版）' },
          ]}
        />
      </Form.Item>
      <Form.Item
        name="url"
        label="地址"
        rules={[
          { required: true, whitespace: true, message: '请输入地址' },
          { pattern: /^https?:\/\//i, message: '地址须以 http:// 或 https:// 开头' },
        ]}
        extra="只能接入远程服务；内网地址默认会被拦截。凭据请放在请求头里，不要写在地址中。"
      >
        <Input maxLength={500} placeholder="https://example.com/mcp" />
      </Form.Item>
      <Form.Item label="请求头" validateStatus={headersError ? 'error' : undefined} help={headersError ?? undefined}>
        <HeadersEditor value={headers} onChange={(v) => { setHeaders(v); setHeadersError(null) }} savedNames={plugin?.header_names ?? []} disabled={saving} />
      </Form.Item>
    </Form>
  )

  const jsonBody = (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Text type="secondary">
        粘贴标准的 mcpServers 配置；每个服务单独创建。stdio（command）类型不支持，会被跳过。
      </Typography.Text>
      <Input.TextArea
        aria-label="mcpServers JSON"
        value={jsonText}
        onChange={(e) => {
          setJsonText(e.target.value)
          setJsonNotice(null)
        }}
        placeholder={JSON_PLACEHOLDER}
        autoSize={{ minRows: 8, maxRows: 16 }}
        style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' }}
      />
      {jsonNotice?.error && <Alert type="error" showIcon message={jsonNotice.error} />}
      {jsonNotice?.stdio && jsonNotice.stdio.length > 0 && <Alert type="warning" showIcon message={stdioNotice(jsonNotice.stdio)} />}
      {jsonNotice?.invalid?.map((i) => <Alert key={i.name} type="error" showIcon message={`${i.name}：${i.reason}`} />)}
      {results && (
        <List
          size="small"
          bordered
          dataSource={results}
          renderItem={(item) => (
            <List.Item>
              <Space wrap size={4} style={{ minWidth: 0 }}>
                <Tag color={item.status === 'created' ? 'success' : 'error'}>{item.status === 'created' ? '已添加' : '失败'}</Tag>
                <Typography.Text style={{ overflowWrap: 'anywhere' }}>{item.name}</Typography.Text>
                {item.detail && <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>{item.detail}</Typography.Text>}
              </Space>
            </List.Item>
          )}
        />
      )}
    </Space>
  )

  return (
    <Modal
      open
      title={plugin ? `编辑插件：${plugin.name}` : '添加 MCP 插件'}
      width={width}
      okText={plugin || tab === 'form' ? '保存' : '导入'}
      confirmLoading={saving}
      onOk={() => void (tab === 'json' && !plugin ? submitJson() : submitForm())}
      cancelText="取消"
      onCancel={onClose}
      destroyOnHidden
    >
      {plugin ? (
        formBody
      ) : (
        <Tabs
          activeKey={tab}
          onChange={(k) => setTab(k as 'form' | 'json')}
          items={[
            { key: 'form', label: '填写表单', children: formBody },
            { key: 'json', label: '粘贴 JSON', children: jsonBody },
          ]}
        />
      )}
    </Modal>
  )
}
