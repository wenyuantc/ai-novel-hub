import { DeleteOutlined, EditOutlined, ReloadOutlined, ToolOutlined } from '@ant-design/icons'
import { Button, Card, Space, Switch, Tag, Typography } from 'antd'
import { MCP_TRANSPORT_LABELS } from '../../api/mcp'
import type { McpPlugin } from '../../api/types'
import { pluginStatus } from '../../utils/mcp'
import { formatTime } from '../../utils/time'

export type McpPluginAction = 'open' | 'refresh' | 'edit' | 'delete'

interface Props {
  plugin: McpPlugin
  /** 正在刷新 / 切换开关 */
  busy?: boolean
  onAction: (plugin: McpPlugin, action: McpPluginAction) => void
  onToggle: (plugin: McpPlugin, enabled: boolean) => void
}

/** MCP 插件卡片：状态、最后错误、工具数量、启用开关。 */
export default function McpPluginCard({ plugin, busy, onAction, onToggle }: Props) {
  const status = pluginStatus(plugin)
  const enabledTools = plugin.tools.filter((t) => t.enabled).length
  return (
    <Card
      size="small"
      style={{ height: '100%' }}
      title={
        <Space wrap size={4} style={{ whiteSpace: 'normal' }}>
          <span style={{ overflowWrap: 'anywhere' }}>{plugin.name}</span>
          <Tag color={status.color}>{status.label}</Tag>
        </Space>
      }
      extra={
        <Switch
          size="small"
          aria-label={`启用插件 ${plugin.name}`}
          checked={plugin.enabled}
          loading={busy}
          onChange={(v) => onToggle(plugin, v)}
        />
      }
      actions={[
        <Button key="open" type="link" size="small" icon={<ToolOutlined />} onClick={() => onAction(plugin, 'open')}>
          工具
        </Button>,
        <Button key="refresh" type="link" size="small" icon={<ReloadOutlined />} loading={busy} onClick={() => onAction(plugin, 'refresh')}>
          测试
        </Button>,
        <Button key="edit" type="link" size="small" icon={<EditOutlined />} onClick={() => onAction(plugin, 'edit')}>
          编辑
        </Button>,
        <Button key="delete" type="link" size="small" danger icon={<DeleteOutlined />} onClick={() => onAction(plugin, 'delete')}>
          删除
        </Button>,
      ]}
      styles={{ actions: { flexWrap: 'wrap' } }}
    >
      <Space direction="vertical" size={4} style={{ width: '100%', minWidth: 0 }}>
        <Space wrap size={[4, 4]}>
          <Tag>{MCP_TRANSPORT_LABELS[plugin.transport]}</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            标识 {plugin.slug}
          </Typography.Text>
          {plugin.has_headers && <Tag>请求头 {plugin.header_names.length} 个</Tag>}
        </Space>
        <Typography.Text type="secondary" style={{ fontSize: 12, overflowWrap: 'anywhere', wordBreak: 'break-all' }}>
          {plugin.url}
        </Typography.Text>
        <Typography.Text style={{ fontSize: 12 }}>
          {plugin.tools_refreshed_at
            ? `工具 ${plugin.tools.length} 个（启用 ${enabledTools}）· 刷新于 ${formatTime(plugin.tools_refreshed_at)}`
            : '尚未获取工具列表，请先测试连接'}
        </Typography.Text>
        {plugin.last_error && (
          <Typography.Text type="danger" style={{ fontSize: 12, overflowWrap: 'anywhere' }}>
            {plugin.last_error}
          </Typography.Text>
        )}
      </Space>
    </Card>
  )
}
