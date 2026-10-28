import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { Button, Input, Space, Tag, Typography } from 'antd'
import { headerRow, MAX_HEADERS, type HeaderRow, type HeadersState } from '../../utils/mcp'

interface Props {
  value: HeadersState
  onChange: (value: HeadersState) => void
  /** 已保存的请求头名称（编辑已有插件时）；取值不回显 */
  savedNames: string[]
  disabled?: boolean
}

export const MASK = '••••'

/**
 * 请求头编辑（键值对）。已保存的值不回显：保持原值时只显示名称与掩码，PATCH 不提交 headers；
 * 「重新设置」后需要重新填写全部取值，保存时整体替换。
 */
export default function HeadersEditor({ value, onChange, savedNames, disabled }: Props) {
  if (value.mode === 'keep')
    return (
      <Space direction="vertical" size={4} style={{ width: '100%' }}>
        {savedNames.length ? (
          <Space wrap size={[4, 4]}>
            {savedNames.map((name) => (
              <Tag key={name} style={{ maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {name}: {MASK}
              </Tag>
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">没有请求头</Typography.Text>
        )}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          已保存的取值加密存储，不会显示。不修改时保持原值。
        </Typography.Text>
        <Space wrap>
          <Button
            size="small"
            disabled={disabled}
            onClick={() => onChange({ mode: 'edit', rows: savedNames.length ? savedNames.map((n) => headerRow(n)) : [headerRow()] })}
          >
            {savedNames.length ? '重新设置' : '添加请求头'}
          </Button>
          {savedNames.length > 0 && (
            <Button size="small" danger disabled={disabled} onClick={() => onChange({ mode: 'edit', rows: [] })}>
              清空请求头
            </Button>
          )}
        </Space>
      </Space>
    )

  const rows = value.rows
  const setRows = (next: HeaderRow[]) => onChange({ mode: 'edit', rows: next })
  const patch = (key: number, part: Partial<HeaderRow>) => setRows(rows.map((r) => (r.key === key ? { ...r, ...part } : r)))
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      {rows.map((row) => (
        <div key={row.key} style={{ display: 'flex', gap: 8, flexWrap: 'wrap', width: '100%' }}>
          <Input
            aria-label="请求头名称"
            placeholder="名称，如 Authorization"
            value={row.name}
            disabled={disabled}
            maxLength={100}
            onChange={(e) => patch(row.key, { name: e.target.value })}
            style={{ flex: '1 1 140px', minWidth: 0 }}
          />
          <Input.Password
            aria-label="请求头取值"
            placeholder="取值"
            value={row.value}
            disabled={disabled}
            maxLength={4096}
            autoComplete="new-password"
            visibilityToggle
            onChange={(e) => patch(row.key, { value: e.target.value })}
            style={{ flex: '2 1 180px', minWidth: 0 }}
          />
          <Button
            type="text"
            danger
            aria-label="删除请求头"
            icon={<DeleteOutlined />}
            disabled={disabled}
            onClick={() => setRows(rows.filter((r) => r.key !== row.key))}
          />
        </div>
      ))}
      {savedNames.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          保存后整体替换原有请求头；{rows.length === 0 ? '不填写任何请求头即为清空。' : '请重新填写每一项的取值。'}
        </Typography.Text>
      )}
      <Space wrap>
        <Button size="small" icon={<PlusOutlined />} disabled={disabled || rows.length >= MAX_HEADERS} onClick={() => setRows([...rows, headerRow()])}>
          添加请求头
        </Button>
        {savedNames.length > 0 && (
          <Button size="small" disabled={disabled} onClick={() => onChange({ mode: 'keep' })}>
            取消，保持原值
          </Button>
        )}
      </Space>
    </Space>
  )
}
