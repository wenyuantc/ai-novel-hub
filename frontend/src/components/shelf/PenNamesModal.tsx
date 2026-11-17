import { App, Alert, Button, Input, List, Modal, Popconfirm, Space, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { penNamesApi } from '../../api/penNames'
import type { PenName } from '../../api/types'
import { capturePenNameActor } from '../../hooks/usePenNames'

interface Props {
  items: PenName[]
  loading: boolean
  error: string | null
  reload: () => void
  onClose: () => void
  onChanged: (deletedId?: number) => void
}

export default function PenNamesModal({ items, loading, error, reload, onClose, onChanged }: Props) {
  const { message } = App.useApp()
  const [name, setName] = useState('')
  const [editing, setEditing] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [busy, setBusy] = useState(false)
  const mounted = useRef(true)
  const pending = useRef(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const close = () => { mounted.current = false; onClose() }
  const run = async (operation: (isCurrent: () => boolean) => Promise<unknown>, success: string, deletedId?: number) => {
    if (pending.current) return
    const actorCurrent = capturePenNameActor()
    const isCurrent = () => mounted.current && actorCurrent()
    if (!isCurrent()) return
    pending.current = true
    setBusy(true)
    try {
      await operation(isCurrent)
      if (!isCurrent()) return
      setName('')
      setEditing(null)
      message.success(success)
      onChanged(deletedId)
    } catch (err) {
      if (isCurrent()) {
        message.error(errorMessage(err))
        // CAS 冲突或已被删除时重新读取事实，不自动重放写入。
        reload()
      }
    } finally {
      pending.current = false
      if (isCurrent()) setBusy(false)
    }
  }

  return (
    <Modal title="管理笔名" open onCancel={close} footer={<Button onClick={close}>关闭</Button>} destroyOnHidden>
      <Typography.Paragraph type="secondary">笔名仅自己管理。删除后会解除所有作品的该笔名，作品仍会保留。</Typography.Paragraph>
      <Space.Compact style={{ width: '100%' }}>
        <Input aria-label="新笔名" value={name} maxLength={100} placeholder="输入新笔名" disabled={busy} onChange={(e) => setName(e.target.value)} onPressEnter={() => {
          if (name.trim()) void run((isCurrent) => penNamesApi.create(name.trim(), { isCurrent }), '笔名已创建')
        }} />
        <Button type="primary" disabled={busy || !name.trim()} onClick={() => void run((isCurrent) => penNamesApi.create(name.trim(), { isCurrent }), '笔名已创建')}>添加</Button>
      </Space.Compact>
      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={reload}>重试</Button>} style={{ marginTop: 12 }} />}
      <List
        loading={loading}
        dataSource={items}
        locale={{ emptyText: '还没有笔名' }}
        renderItem={(item) => (
          <List.Item key={item.id}>
            {editing === item.id ? (
              <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
                <Input aria-label={`修改笔名 ${item.name}`} maxLength={100} value={editName} disabled={busy} onChange={(e) => setEditName(e.target.value)} />
                <Space><Button type="primary" disabled={busy || !editName.trim()} onClick={() => void run((isCurrent) => penNamesApi.rename(item.id, editName.trim(), item.revision, { isCurrent }), '笔名已更新')}>保存</Button><Button disabled={busy} onClick={() => setEditing(null)}>取消</Button></Space>
              </Space>
            ) : (
              <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
                <Typography.Text>{item.name}</Typography.Text>
                <Space><Button disabled={busy} onClick={() => { setEditing(item.id); setEditName(item.name) }}>改名</Button>
                  <Popconfirm title={`删除笔名「${item.name}」？`} description="将解除所有作品的该笔名，作品不会删除。" okText="删除" cancelText="取消" onConfirm={() => run((isCurrent) => penNamesApi.remove(item.id, item.revision, { isCurrent }), '笔名已删除', item.id)}><Button danger disabled={busy}>删除</Button></Popconfirm>
                </Space>
              </Space>
            )}
          </List.Item>
        )}
      />
    </Modal>
  )
}
