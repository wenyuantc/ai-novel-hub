import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { Button, Input, Space, Table, Tooltip, Typography } from 'antd'
import { MAX_STAGE_NAME, moveItem, newStageRow, type StageRow } from '../../api/careers'

interface Props {
  value: StageRow[]
  onChange: (next: StageRow[]) => void
  disabled?: boolean
  /** 被角色引用的阶段 id：删除按钮禁用（后端也会 409） */
  lockedIds?: ReadonlySet<number>
  /** 校验错误（validateStages 的结果） */
  error?: string | null
}

/** 阶段阶梯编辑：顺序即 position，上下移动排序、增删；不引入拖拽依赖。 */
export default function StageListEditor({ value, onChange, disabled, lockedIds, error }: Props) {
  const patch = (key: string, p: Partial<StageRow>) => onChange(value.map((r) => (r.key === key ? { ...r, ...p } : r)))
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Table<StageRow>
        size="small"
        rowKey="key"
        pagination={false}
        dataSource={value}
        locale={{ emptyText: '还没有阶段' }}
        columns={[
          { title: '阶', width: 44, render: (_, __, i) => i + 1 },
          {
            title: '名称',
            width: 180,
            render: (_, r) => (
              <Input
                size="small"
                value={r.name}
                maxLength={MAX_STAGE_NAME}
                disabled={disabled}
                placeholder="例如：筑基"
                onChange={(e) => patch(r.key, { name: e.target.value })}
              />
            ),
          },
          {
            title: '描述',
            render: (_, r) => (
              <Input.TextArea
                size="small"
                autoSize={{ minRows: 1, maxRows: 4 }}
                value={r.description}
                disabled={disabled}
                onChange={(e) => patch(r.key, { description: e.target.value })}
              />
            ),
          },
          {
            title: '',
            width: 110,
            render: (_, r, i) => {
              const locked = r.id !== null && !!lockedIds?.has(r.id)
              return (
                <Space size={0}>
                  <Button
                    type="text"
                    size="small"
                    icon={<ArrowUpOutlined />}
                    aria-label="上移"
                    disabled={disabled || i === 0}
                    onClick={() => onChange(moveItem(value, i, -1))}
                  />
                  <Button
                    type="text"
                    size="small"
                    icon={<ArrowDownOutlined />}
                    aria-label="下移"
                    disabled={disabled || i === value.length - 1}
                    onClick={() => onChange(moveItem(value, i, 1))}
                  />
                  <Tooltip title={locked ? '该阶段仍有角色处于其中，无法删除' : undefined}>
                    <Button
                      type="text"
                      size="small"
                      danger
                      icon={<DeleteOutlined />}
                      aria-label="删除阶段"
                      disabled={disabled || locked || value.length <= 1}
                      onClick={() => onChange(value.filter((x) => x.key !== r.key))}
                    />
                  </Tooltip>
                </Space>
              )
            },
          },
        ]}
      />
      <Space>
        <Button size="small" icon={<PlusOutlined />} disabled={disabled} onClick={() => onChange([...value, newStageRow()])}>
          添加阶段
        </Button>
        {error && <Typography.Text type="danger">{error}</Typography.Text>}
        {!error && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            调整顺序不会改变角色所处的阶段；挂在该阶的角色随阶段一起移动。
          </Typography.Text>
        )}
      </Space>
    </Space>
  )
}
