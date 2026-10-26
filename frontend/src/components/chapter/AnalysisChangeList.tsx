import { ArrowRightOutlined } from '@ant-design/icons'
import { App, Button, Empty, List, Popconfirm, Space, Tag, Typography } from 'antd'
import { useState, type ReactNode } from 'react'
import { analysisApi, CHANGE_STATUS, describeChange, groupChanges } from '../../api/analysis'
import { errorMessage } from '../../api/client'
import type { AnalysisChange } from '../../api/types'
import ForeshadowConfirmModal from '../foreshadows/ForeshadowConfirmModal'

interface Props {
  changes: AnalysisChange[]
  /** 撤销 / 确认 / 拒绝成功后，用服务端返回的条目更新列表 */
  onChanged: (updated: AnalysisChange) => void
}

/** 回写清单：按类型分组，每条「前 → 后」+ 状态；已写入可撤销，待确认可确认 / 拒绝，跳过 / 失败显示原因。 */
export default function AnalysisChangeList({ changes, onChanged }: Props) {
  const { message } = App.useApp()
  const [pending, setPending] = useState<number | null>(null)
  // foreshadow_new 确认时弹出可编辑表单（overrides）
  const [confirmingFs, setConfirmingFs] = useState<AnalysisChange | null>(null)
  const groups = groupChanges(changes)

  const act = async (c: AnalysisChange, fn: (id: number) => Promise<AnalysisChange>, done: string) => {
    setPending(c.id)
    try {
      onChanged(await fn(c.id))
      message.success(done)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setPending(null)
    }
  }

  if (groups.length === 0) {
    return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本次分析没有识别到设定变化" />
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        常规变化与按编号匹配的伏笔埋设 / 回收已自动写入，可逐条撤销；角色死亡 / 失踪、新建关系、新识别的伏笔需确认后才写入（新伏笔确认时可修改标题等字段）；名字或伏笔编号对不上的只做提示。
      </Typography.Text>
      {groups.map((g) => (
        <div key={g.kind}>
          <Typography.Text strong>
            {g.label}（{g.items.length}）
          </Typography.Text>
          <List
            size="small"
            dataSource={g.items}
            renderItem={(c) => {
              const view = describeChange(c)
              const st = CHANGE_STATUS[c.status]
              const actions: ReactNode[] = []
              if (c.status === 'applied') {
                actions.push(
                  <Popconfirm
                    key="revert"
                    title="撤销这条回写？"
                    description={
                      c.kind === 'foreshadow_new'
                        ? '将删除由这条确认创建的伏笔；若伏笔已被修改则无法撤销。'
                        : '设定恢复为分析前的值；若已被后续修改则无法撤销。'
                    }
                    okText="撤销"
                    cancelText="取消"
                    onConfirm={() => act(c, analysisApi.revert, '已撤销')}
                  >
                    <Button type="link" size="small" loading={pending === c.id}>
                      撤销
                    </Button>
                  </Popconfirm>,
                )
              } else if (c.status === 'pending') {
                actions.push(
                  <Button
                    key="confirm"
                    type="link"
                    size="small"
                    loading={pending === c.id}
                    onClick={() =>
                      c.kind === 'foreshadow_new'
                        ? setConfirmingFs(c)
                        : void act(c, (id) => analysisApi.confirm(id), '已确认并写入')
                    }
                  >
                    {c.kind === 'foreshadow_new' ? '确认…' : '确认'}
                  </Button>,
                  <Button
                    key="reject"
                    type="link"
                    size="small"
                    danger
                    disabled={pending === c.id}
                    onClick={() => void act(c, analysisApi.reject, '已拒绝')}
                  >
                    拒绝
                  </Button>,
                )
              }
              return (
                <List.Item key={c.id} actions={actions}>
                  <div style={{ minWidth: 0 }}>
                    <Space wrap size={4}>
                      <Typography.Text>{view.subject}</Typography.Text>
                      {view.field && <Typography.Text type="secondary">{view.field}</Typography.Text>}
                      <Tag color={st.color}>{st.label}</Tag>
                    </Space>
                    <div style={{ wordBreak: 'break-all' }}>
                      <Typography.Text type="secondary">{view.before}</Typography.Text>
                      <ArrowRightOutlined style={{ margin: '0 6px', fontSize: 12 }} />
                      <Typography.Text>{view.after}</Typography.Text>
                    </div>
                    {c.reason && (
                      <Typography.Text type={c.status === 'failed' ? 'danger' : 'secondary'} style={{ fontSize: 12 }}>
                        {c.reason}
                      </Typography.Text>
                    )}
                  </div>
                </List.Item>
              )
            }}
          />
        </div>
      ))}
      <ForeshadowConfirmModal change={confirmingFs} onClose={() => setConfirmingFs(null)} onConfirmed={onChanged} />
    </Space>
  )
}
