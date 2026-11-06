import { PlusOutlined } from '@ant-design/icons'
import { Button, Empty, List, Popconfirm, Space, Tag, Typography } from 'antd'
import type { SettingTextField } from '../../api/types'

interface Props {
  fields: readonly SettingTextField[]
  canEdit: boolean
  busy: boolean
  onCreate: () => void
  onEdit: (field: SettingTextField) => void
  onDelete: (field: SettingTextField) => Promise<void>
  onBind?: (field: SettingTextField) => void
  hiddenFieldIds: ReadonlySet<number>
  onVisibility: (field: SettingTextField) => void
}

export default function TextFieldsPanel({ fields, canEdit, busy, onCreate, onEdit, onDelete, onBind, hiddenFieldIds, onVisibility }: Props) {
  return (
    <div>
      <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
        <Typography.Title level={5} style={{ margin: 0 }}>本条目原有字段</Typography.Title>
        <Button size="small" icon={<PlusOutlined />} disabled={!canEdit || busy} onClick={onCreate}>新增字段</Button>
      </Space>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>在这里维护故事开始时的基础内容，后续变化可单独记录。</Typography.Paragraph>
      <List
        rowKey="id"
        size="small"
        dataSource={[...fields]}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无自定义字段" /> }}
        renderItem={(field) => (
          <List.Item
            actions={[
              <Button key="edit" type="link" size="small" onClick={() => onEdit(field)}>{canEdit ? '编辑' : '查看'}</Button>,
              ...(onBind ? [<Button key="bind" type="link" size="small" disabled={!canEdit || busy} onClick={() => onBind(field)}>关联共享字段</Button>] : []),
              <Popconfirm
                key="delete"
                title="删除这个字段？"
                description="若该字段仍有生效的剧情变化，需要先删除这些变化。"
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true, disabled: !canEdit || busy }}
                onConfirm={() => onDelete(field)}
              >
                <Button type="link" size="small" danger disabled={!canEdit || busy}>删除</Button>
              </Popconfirm>,
            ]}
          >
            <List.Item.Meta
              title={<Space wrap>{field.label}{hiddenFieldIds.has(field.id) && <Tag>对 AI 隐藏</Tag>}</Space>}
              description={<>
                <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} ellipsis={{ rows: 3, expandable: true }}>{field.base_value || '（无基础内容）'}</Typography.Paragraph>
                {canEdit && <Button type="link" size="small" style={{ paddingInline: 0 }} disabled={busy}
                  aria-label={`设置「${field.label}」的 AI 可见性`} onClick={() => onVisibility(field)}>设置 AI 可见性</Button>}
              </>}
            />
          </List.Item>
        )}
      />
    </div>
  )
}
