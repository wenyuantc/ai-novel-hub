import { PlusOutlined } from '@ant-design/icons'
import { Button, Empty, List, Popconfirm, Space, Typography } from 'antd'
import type { SettingTextField } from '../../api/types'

interface Props {
  fields: readonly SettingTextField[]
  canEdit: boolean
  busy: boolean
  onCreate: () => void
  onEdit: (field: SettingTextField) => void
  onDelete: (field: SettingTextField) => Promise<void>
}

export default function TextFieldsPanel({ fields, canEdit, busy, onCreate, onEdit, onDelete }: Props) {
  return (
    <div>
      <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
        <Typography.Title level={5} style={{ margin: 0 }}>文本自定义字段</Typography.Title>
        <Button size="small" icon={<PlusOutlined />} disabled={!canEdit || busy} onClick={onCreate}>新增字段</Button>
      </Space>
      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>在这里维护故事开始时的基础内容，后续变化可单独记录。</Typography.Paragraph>
      <List
        size="small"
        dataSource={[...fields]}
        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无自定义字段" /> }}
        renderItem={(field) => (
          <List.Item
            actions={[
              <Button key="edit" type="link" size="small" onClick={() => onEdit(field)}>{canEdit ? '编辑' : '查看'}</Button>,
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
              title={field.label}
              description={<Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }} ellipsis={{ rows: 3, expandable: true }}>{field.base_value || '（无基础内容）'}</Typography.Paragraph>}
            />
          </List.Item>
        )}
      />
    </div>
  )
}
