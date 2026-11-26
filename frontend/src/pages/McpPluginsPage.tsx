import { PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Col, Empty, Row, Space, Spin, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../api/client'
import { MAX_MCP_PLUGINS, mcpApi } from '../api/mcp'
import type { McpPlugin } from '../api/types'
import McpPluginCard, { type McpPluginAction } from '../components/mcp/McpPluginCard'
import McpCatalogModal from '../components/mcp/McpCatalogModal'
import McpPluginDrawer from '../components/mcp/McpPluginDrawer'
import McpPluginFormModal from '../components/mcp/McpPluginFormModal'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'

const listPlugins = () => mcpApi.list()

/** 个人 MCP 插件与部署者提供的服务器目录。 */
export default function McpPluginsPage() {
  const { message, modal } = App.useApp()
  const isMobile = useIsMobile()
  const { data, loading, error, reload, setData } = useLoader(listPlugins)
  const plugins = data ?? []
  const [form, setForm] = useState<{ plugin: McpPlugin | null } | null>(null)
  const [catalogOpen, setCatalogOpen] = useState(false)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const detail = plugins.find((p) => p.id === detailId) ?? null

  const replace = (plugin: McpPlugin) => setData((prev) => (prev ?? []).map((p) => (p.id === plugin.id ? plugin : p)))

  const withBusy = async (plugin: McpPlugin, operation: () => Promise<void>) => {
    setBusyId(plugin.id)
    try {
      await operation()
    } finally {
      setBusyId(null)
    }
  }

  const onAction = (plugin: McpPlugin, action: McpPluginAction) => {
    if (action === 'open') setDetailId(plugin.id)
    else if (action === 'edit') setForm({ plugin })
    else if (action === 'refresh')
      void withBusy(plugin, async () => {
        try {
          replace(await mcpApi.refresh(plugin.id, plugin.catalog_version))
          message.success(`「${plugin.name}」连接成功，工具列表已更新`)
        } catch (err) {
          message.error(errorMessage(err))
          try {
            replace(await mcpApi.get(plugin.id))
          } catch {
            /* 保留原状态 */
          }
        }
      })
    else if (action === 'delete')
      modal.confirm({
        title: `删除插件「${plugin.name}」？`,
        content: plugin.transport === 'stdio' ? '从个人插件列表移除后，智能体会话中的这个插件也会失效。' : '已保存的请求头一并删除。智能体会话中启用的这个插件会自动失效。',
        okText: '删除',
        okButtonProps: { danger: true },
        onOk: async () => {
          try {
            await mcpApi.remove(plugin.id)
            message.success('已删除')
            reload()
          } catch (err) {
            message.error(errorMessage(err))
            throw err
          }
        },
      })
  }

  const toggle = (plugin: McpPlugin, enabled: boolean) =>
    void withBusy(plugin, async () => {
      try {
        replace(await mcpApi.update(plugin.id, { enabled }))
      } catch (err) {
        message.error(errorMessage(err))
      }
    })

  const disabled = data === undefined || error !== null
  return (
    <div style={{ padding: isMobile ? 12 : 24, maxWidth: 1400, margin: '0 auto' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 16 }} wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>
            MCP 插件
          </Typography.Title>
          <Typography.Text type="secondary">
            从服务器列表启用本机插件，或添加远程服务，然后在项目智能体会话中选择工具。每人最多 {MAX_MCP_PLUGINS} 个插件。
          </Typography.Text>
        </div>
        <Space wrap>
          <Button disabled={disabled} onClick={() => setCatalogOpen(true)}>服务器插件列表</Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            disabled={disabled || plugins.length >= MAX_MCP_PLUGINS}
            onClick={() => setForm({ plugin: null })}
          >
            添加远程插件
          </Button>
        </Space>
      </Space>
      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ marginBottom: 16 }}
          action={<Button size="small" onClick={reload}>重试</Button>}
        />
      )}
      {loading && !data ? (
        <Spin style={{ display: 'block', margin: 60 }} />
      ) : plugins.length ? (
        <Row gutter={[16, 16]}>
          {plugins.map((p) => (
            <Col xs={24} md={12} xl={8} key={p.id}>
              <McpPluginCard plugin={p} busy={busyId === p.id} onAction={onAction} onToggle={toggle} />
            </Col>
          ))}
        </Row>
      ) : (
        !error && <Empty description="还没有添加插件" />
      )}
      {catalogOpen && (
        <McpCatalogModal plugins={plugins} onClose={() => setCatalogOpen(false)} onEnabled={(saved) =>
          setData((prev) => [...(prev ?? []).filter((p) => p.id !== saved.id), saved], { invalidatePending: true })
        } />
      )}
      {form && (
        <McpPluginFormModal
          plugin={form.plugin}
          onClose={() => setForm(null)}
          onSaved={(saved) => (saved && form.plugin ? replace(saved) : reload())}
        />
      )}
      <McpPluginDrawer plugin={detail} onClose={() => setDetailId(null)} onChanged={replace} />
    </div>
  )
}

