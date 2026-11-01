import { useAuthorizationCleanup } from '../../hooks/useAuthorizationCleanup'
import { useSettingsConfirmation } from '../systemSettings/useSettingsConfirmation'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { App, Alert, Button, Drawer, Empty, Input, List, Popconfirm, Space, Spin, Table, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { errorMessage } from '../../api/client'
import { SNAPSHOT_REASON_LABELS, snapshotApi } from '../../api/outlines'
import type { OutlineSnapshot, OutlineSnapshotDetail } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'
import { volumeLabel } from '../../utils/outline'

interface Props {
  projectId: number
  open: boolean
  onClose: () => void
  /** 恢复成功后刷新大纲页 */
  onRestored: () => void
}

/**
 * 大纲快照：列表（时间、原因、备注、大纲 / 细纲 / 章节关联数）、详情（章纲清单 + 恢复提示）、恢复（恢复前自动为当前状态存快照，
 * 恢复它即可撤销）、手动保存、删除。每个项目保留最近 20 个。
 */
export default function OutlineSnapshotDrawer({ projectId, open, onClose, onRestored }: Props) {
  return (
    <Drawer title="大纲快照" open={open} onClose={onClose} width={640} destroyOnHidden>
      {open && <Inner projectId={projectId} onRestored={onRestored} />}
    </Drawer>
  )
}

function Inner({ projectId, onRestored }: { projectId: number; onRestored: () => void }) {
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(projectId)
  const { message, modal } = App.useApp()
  const confirm = useSettingsConfirmation(modal, !canEdit)
  const loader = useCallback(() => snapshotApi.list(projectId), [projectId])
  const { data: items = [], loading, reload } = useLoader(loader)
  const [note, setNote] = useState('')
  const [saving, setSaving] = useState(false)
  const [pending, setPending] = useState<number | null>(null)
  useAuthorizationCleanup(() => setPending(null))
  const [detail, setDetail] = useState<OutlineSnapshotDetail | null>(null)

  const handleCreate = async () => {
    if (!canEditNow()) return
    setSaving(true)
    try {
      if (!canEditNow()) return
      await snapshotApi.create(projectId, note.trim())
      if (!canEditNow()) return
      message.success('已保存快照')
      setNote('')
      reload()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const showDetail = async (s: OutlineSnapshot) => {
    if (!canReadNow()) return
    setPending(s.id)
    try {
      const detail = await snapshotApi.get(projectId, s.id)
      if (canReadNow()) setDetail(detail)
    } catch (err) {
      if (canReadNow()) message.error(errorMessage(err))
    } finally {
      if (canReadNow()) setPending(null)
    }
  }

  const doRestore = async (s: OutlineSnapshot) => {
    if (!canEditNow()) return
    setPending(s.id)
    try {
      if (!canEditNow()) return
      const r = await snapshotApi.restore(projectId, s.id)
      if (!canEditNow()) return
      modal.success({
        title: '已恢复大纲',
        content: (
          <Space direction="vertical">
            <span>
              恢复了 {r.outlines} 条章纲、{r.plans} 条细纲，重新关联 {r.links_restored} 个章节。
            </span>
            <span>恢复前的状态已存为快照 #{r.backup.id}（「恢复前」），恢复它即可撤销本次恢复。</span>
            {r.warnings.map((w) => (
              <Typography.Text key={w} type="warning">
                {w}
              </Typography.Text>
            ))}
          </Space>
        ),
      })
      setDetail(null)
      reload()
      onRestored()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setPending(null)
    }
  }

  const confirmRestore = async (s: OutlineSnapshot) => {
    if (!canEditNow()) return
    // 先取详情拿到恢复提示（如快照后发生过章节号平移）
    let warnings: string[]
    try {
      if (!canEditNow()) return
      warnings = (await snapshotApi.get(projectId, s.id)).warnings
      if (!canEditNow()) return
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
      return
    }
    confirm({
      title: `恢复到快照 #${s.id}？`,
      content: (
        <Space direction="vertical">
          <span>当前的全部大纲与细纲会被替换为快照中的内容；任何章节及其正文都不会删除，快照中的大纲会重新关联原来的章节。</span>
          <span>恢复前会自动为当前状态保存一个快照，可随时撤销。</span>
          {warnings.map((w) => (
            <Typography.Text key={w} type="warning">
              {w}
            </Typography.Text>
          ))}
        </Space>
      ),
      okText: '恢复',
      cancelText: '取消',
      onOk: () => doRestore(s),
    })
  }

  const handleDelete = async (s: OutlineSnapshot) => {
    if (!canEditNow()) return
    setPending(s.id)
    try {
      if (!canEditNow()) return
      await snapshotApi.remove(projectId, s.id)
      if (!canEditNow()) return
      message.success('已删除快照')
      if (detail?.id === s.id) setDetail(null)
      reload()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setPending(null)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Space.Compact style={{ width: '100%' }}>
        <Input
          value={note}
          maxLength={200}
          placeholder="快照备注（可选）"
          onChange={(e) => setNote(e.target.value)}
          onPressEnter={() => void handleCreate()}
        />
        <Button type="primary" loading={saving} onClick={() => void handleCreate()} disabled={!canEdit}>
          保存当前大纲为快照
        </Button>
      </Space.Compact>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        向导覆盖、导入替换、重新展开与恢复之前都会自动保存快照；每个项目保留最近 20 个。
      </Typography.Text>
      <Spin spinning={loading}>
        {items.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无快照" />
        ) : (
          <List
            dataSource={items}
            renderItem={(s) => {
              const reason = SNAPSHOT_REASON_LABELS[s.reason]
              return (
                <List.Item
                  key={s.id}
                  actions={[
                    <Button key="view" type="link" size="small" loading={pending === s.id && detail?.id !== s.id} onClick={() => void showDetail(s)}>
                      详情
                    </Button>,
                    <Button key="restore" type="link" size="small" disabled={!canEdit || (pending !== null)} onClick={() => void confirmRestore(s)}>
                      恢复
                    </Button>,
                    <Popconfirm key="del" title="删除该快照？" okText="删除" cancelText="取消" onConfirm={() => handleDelete(s)}>
                      <Button type="link" size="small" danger disabled={!canEdit}>
                        删除
                      </Button>
                    </Popconfirm>,
                  ]}
                >
                  <div>
                    <Space wrap size={4}>
                      <Typography.Text strong>#{s.id}</Typography.Text>
                      <Tag color={reason?.color}>{reason?.label ?? s.reason}</Tag>
                      <Typography.Text type="secondary">{formatTime(s.created_at)}</Typography.Text>
                    </Space>
                    <Typography.Text type="secondary" style={{ display: 'block', fontSize: 12 }}>
                      {s.outline_count} 条章纲 · {s.plan_count} 条细纲 · {s.link_count} 个章节关联
                      {s.note ? ` · ${s.note}` : ''}
                    </Typography.Text>
                  </div>
                </List.Item>
              )
            }}
          />
        )}
      </Spin>
      {detail && (
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text strong>快照 #{detail.id} 的章纲</Typography.Text>
          {detail.warnings.map((w) => (
            <Alert key={w} type="warning" showIcon message={w} />
          ))}
          <Table
            size="small"
            rowKey={(o) => `${o.volume_no}-${o.chapter_no}-${o.title}`}
            pagination={{ pageSize: 20, hideOnSinglePage: true }}
            dataSource={detail.outlines}
            columns={[
              {
                title: '卷',
                width: 140,
                render: (_, o) => volumeLabel({ volumeNo: o.volume_no, volumeTitle: o.volume_title }),
              },
              { title: '章', dataIndex: 'chapter_no', width: 60 },
              { title: '标题', dataIndex: 'title' },
              { title: '细纲', dataIndex: 'plan_count', width: 60 },
            ]}
          />
        </Space>
      )}
    </Space>
  )
}
