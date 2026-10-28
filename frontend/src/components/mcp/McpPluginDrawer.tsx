import { CodeOutlined, ReloadOutlined, SendOutlined } from '@ant-design/icons'
import { Alert, App, Button, Drawer, Empty, Input, List, Modal, Select, Space, Switch, Tag, Typography, theme } from 'antd'
import type { CSSProperties } from 'react'
import { useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { mcpApi } from '../../api/mcp'
import type { McpCallResult, McpPlugin, McpTool, McpToolPrefUpdate } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { argumentsTemplate, callErrors, parseToolArguments, pluginStatus } from '../../utils/mcp'
import { formatTime } from '../../utils/time'

interface Props {
  plugin: McpPlugin | null
  onClose: () => void
  /** 刷新 / 工具开关之后的最新插件 */
  onChanged: (plugin: McpPlugin) => void
}

const mono: CSSProperties = {
  whiteSpace: 'pre-wrap',
  overflowWrap: 'anywhere',
  margin: 0,
  maxHeight: 360,
  overflow: 'auto',
  fontSize: 12,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
}

/** 插件详情：测试连接 / 刷新工具、工具开关（启用 / 免确认）、查看参数 schema、手动试用调用（结果按纯文本显示）。 */
export default function McpPluginDrawer({ plugin, onClose, onChanged }: Props) {
  const width = useModalWidth(720)
  return (
    <Drawer title={plugin ? `插件：${plugin.name}` : ''} width={width} open={plugin !== null} onClose={onClose} destroyOnHidden>
      {plugin && <Inner key={plugin.id} plugin={plugin} onChanged={onChanged} />}
    </Drawer>
  )
}

function Inner({ plugin, onChanged }: { plugin: McpPlugin; onChanged: (plugin: McpPlugin) => void }) {
  const { message } = App.useApp()
  const { token } = theme.useToken()
  const [refreshing, setRefreshing] = useState(false)
  const [pending, setPending] = useState<string | null>(null)
  const [schemaOf, setSchemaOf] = useState<McpTool | null>(null)
  const status = pluginStatus(plugin)

  const refresh = async () => {
    setRefreshing(true)
    try {
      onChanged(await mcpApi.refresh(plugin.id))
      message.success('连接成功，工具列表已更新')
    } catch (err) {
      message.error(errorMessage(err))
      // 失败原因写在 last_error 里：重新读取以显示
      try {
        onChanged(await mcpApi.get(plugin.id))
      } catch {
        /* 读取失败时保留原状态 */
      }
    } finally {
      setRefreshing(false)
    }
  }

  const setPref = async (tool: McpTool, body: McpToolPrefUpdate) => {
    setPending(tool.name)
    try {
      onChanged(await mcpApi.updateTool(plugin.id, tool.name, body))
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setPending(null)
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space wrap>
        <Tag color={status.color}>{status.label}</Tag>
        {plugin.last_tested_at && <Typography.Text type="secondary">最近测试 {formatTime(plugin.last_tested_at)}</Typography.Text>}
        <Button icon={<ReloadOutlined />} loading={refreshing} onClick={() => void refresh()}>
          测试连接并刷新工具
        </Button>
      </Space>
      {!plugin.enabled && <Alert type="info" showIcon message="插件已停用：智能体不会使用它的工具，仍可在这里试用" />}
      {plugin.last_error && <Alert type="error" showIcon message="最近一次连接失败" description={<span style={{ overflowWrap: 'anywhere' }}>{plugin.last_error}</span>} />}
      <div>
        <Typography.Title level={5}>工具（{plugin.tools.length}）</Typography.Title>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
          智能体调用插件工具默认每次都要确认；「免确认」的工具会直接调用。「只读」是插件自己声明的提示，不代表免确认。外部调用无法撤销。
        </Typography.Paragraph>
        {plugin.tools.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={plugin.tools_refreshed_at ? '该服务没有提供工具' : '尚未获取工具列表，请先测试连接'} />
        ) : (
          <List
            size="small"
            bordered
            dataSource={plugin.tools}
            renderItem={(tool) => (
              <List.Item style={{ display: 'block' }}>
                <Space direction="vertical" size={4} style={{ width: '100%', minWidth: 0 }}>
                  <Space wrap size={4}>
                    <Typography.Text code style={{ overflowWrap: 'anywhere' }}>
                      {tool.name}
                    </Typography.Text>
                    {tool.read_only_hint === true && <Tag color="green">只读提示</Tag>}
                    {tool.read_only_hint === false && <Tag color="orange">可能有副作用</Tag>}
                  </Space>
                  {tool.description && (
                    <Typography.Paragraph type="secondary" ellipsis={{ rows: 2, expandable: true, symbol: '展开' }} style={{ marginBottom: 0, fontSize: 12 }}>
                      {tool.description}
                    </Typography.Paragraph>
                  )}
                  <Space wrap size={12}>
                    <Space size={4}>
                      <Switch size="small" aria-label={`启用工具 ${tool.name}`} checked={tool.enabled} loading={pending === tool.name} onChange={(enabled) => void setPref(tool, { enabled })} />
                      <Typography.Text style={{ fontSize: 12 }}>启用</Typography.Text>
                    </Space>
                    <Space size={4}>
                      <Switch size="small" aria-label={`免确认 ${tool.name}`} checked={tool.auto_confirm} loading={pending === tool.name} onChange={(auto_confirm) => void setPref(tool, { auto_confirm })} />
                      <Typography.Text style={{ fontSize: 12 }}>免确认</Typography.Text>
                    </Space>
                    <Button size="small" type="link" icon={<CodeOutlined />} onClick={() => setSchemaOf(tool)}>
                      查看参数
                    </Button>
                  </Space>
                </Space>
              </List.Item>
            )}
          />
        )}
      </div>
      {plugin.tools.length > 0 && <ToolTrial plugin={plugin} />}
      <Modal open={schemaOf !== null} title={schemaOf ? `参数：${schemaOf.name}` : ''} footer={null} onCancel={() => setSchemaOf(null)} destroyOnHidden>
        <pre style={{ ...mono, background: token.colorFillQuaternary, padding: 8, borderRadius: token.borderRadius }}>
          {schemaOf ? JSON.stringify(schemaOf.input_schema, null, 2) : ''}
        </pre>
      </Modal>
    </Space>
  )
}

function ToolTrial({ plugin }: { plugin: McpPlugin }) {
  const { token } = theme.useToken()
  const [toolName, setToolName] = useState<string | undefined>(undefined)
  const [text, setText] = useState('')
  const [calling, setCalling] = useState(false)
  const [result, setResult] = useState<McpCallResult | null>(null)
  const [errors, setErrors] = useState<{ detail: string; items: string[] } | null>(null)
  const tool = plugin.tools.find((t) => t.name === toolName)

  const call = async () => {
    if (!tool) return
    const parsed = parseToolArguments(text)
    if ('error' in parsed) {
      setErrors({ detail: parsed.error, items: [] })
      return
    }
    setCalling(true)
    setErrors(null)
    setResult(null)
    try {
      setResult(await mcpApi.call(plugin.id, tool.name, parsed.args))
    } catch (err) {
      setErrors({ detail: errorMessage(err), items: err instanceof ApiError ? callErrors(err.body) : [] })
    } finally {
      setCalling(false)
    }
  }

  return (
    <div>
      <Typography.Title level={5}>试用</Typography.Title>
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        由你直接发起，不经确认。结果只显示文本内容（图片等会被省略）。
      </Typography.Paragraph>
      <Space direction="vertical" style={{ width: '100%' }}>
        <Select
          aria-label="选择工具"
          placeholder="选择工具"
          value={toolName}
          onChange={(name: string) => {
            setToolName(name)
            const next = plugin.tools.find((t) => t.name === name)
            setText(next ? argumentsTemplate(next.input_schema) : '')
            setResult(null)
            setErrors(null)
          }}
          options={plugin.tools.map((t) => ({ value: t.name, label: t.name }))}
          style={{ width: '100%' }}
        />
        <Input.TextArea
          aria-label="调用参数（JSON）"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="参数（JSON 对象）"
          autoSize={{ minRows: 3, maxRows: 10 }}
          disabled={!tool}
          style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' }}
        />
        <Button type="primary" icon={<SendOutlined />} disabled={!tool} loading={calling} onClick={() => void call()}>
          调用
        </Button>
        {errors && (
          <Alert
            type="error"
            showIcon
            message={errors.detail}
            description={errors.items.length ? <ul style={{ margin: 0, paddingLeft: 18 }}>{errors.items.map((e, i) => <li key={i}>{e}</li>)}</ul> : undefined}
          />
        )}
        {result && (
          <>
            {result.is_error && <Alert type="warning" showIcon message="工具返回了错误" />}
            {result.truncated && <Alert type="info" showIcon message="结果过长，已截断显示" />}
            <pre style={{ ...mono, background: token.colorFillQuaternary, padding: 8, borderRadius: token.borderRadius }}>{result.text || '（空结果）'}</pre>
          </>
        )}
      </Space>
    </div>
  )
}
