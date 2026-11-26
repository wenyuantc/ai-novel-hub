import { Alert, App, Button, Empty, List, Modal } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { MAX_MCP_PLUGINS, mcpApi } from '../../api/mcp'
import type { McpCatalogEntry, McpPlugin } from '../../api/types'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useLoader } from '../../hooks/useLoader'

interface Props {
  plugins: McpPlugin[]
  onEnabled: (plugin: McpPlugin) => void
  onClose: () => void
}

export default function McpCatalogModal({ plugins, onEnabled, onClose }: Props) {
  const { message } = App.useApp()
  const { data, loading, error, reload } = useLoader(mcpApi.catalog)
  const [pending, setPending] = useState<string | null>(null)
  const full = plugins.length >= MAX_MCP_PLUGINS
  const width = useModalWidth(640)

  const choose = async (item: McpCatalogEntry, existing?: McpPlugin) => {
    setPending(item.id)
    try {
      const saved = existing
        ? await mcpApi.refresh(existing.id, item.version)
        : await mcpApi.enableCatalog(item.id, item.version)
      onEnabled(saved)
      message.success(existing ? `「${saved.name}」工具已更新` : `已启用「${saved.name}」`)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setPending(null)
    }
  }

  return (
    <Modal open title="服务器插件列表" footer={null} onCancel={onClose} width={width}>
      {full && <Alert type="info" showIcon message={`已达到 ${MAX_MCP_PLUGINS} 个插件的上限，请先移除一个再添加。`} />}
      {error ? <Alert type="error" showIcon message={error} action={<Button onClick={reload}>重试</Button>} /> : (
        <List
          loading={loading}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="部署者尚未登记可用插件" /> }}
          dataSource={data ?? []}
          renderItem={(item) => {
            const owned = plugins.find((plugin) => plugin.catalog_id === item.id)
            const newer = owned && owned.catalog_version !== item.version
            const actionable = owned ? newer && owned.enabled : !full
            const label = !item.available ? '节点不可用' : newer ? '更新工具' : owned ? '已添加' : '启用'
            return (
              <List.Item extra={
                <Button
                  disabled={!item.available || !actionable || pending !== null}
                  loading={pending === item.id}
                  onClick={() => void choose(item, owned)}
                >
                  {label}
                </Button>
              }>
                <List.Item.Meta
                  style={{ minWidth: 0, overflowWrap: 'anywhere', paddingRight: 12 }}
                  title={`${item.name} · ${item.version}`}
                  description={item.description}
                />
              </List.Item>
            )
          }}
        />
      )}
    </Modal>
  )
}
