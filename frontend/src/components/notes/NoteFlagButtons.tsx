import { PushpinFilled, PushpinOutlined, StarFilled, StarOutlined } from '@ant-design/icons'
import { App, Button, Space, Tooltip, theme } from 'antd'
import { useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { noteApi, type NoteFlags, type ProjectNote } from '../../api/notes'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { invalidateProject } from '../../utils/projectRefresh'

interface Props {
  projectId: number
  note: Pick<ProjectNote, 'id' | 'title' | 'is_favorite' | 'is_pinned'>
  onChange?: (note: ProjectNote) => void
}

/**
 * 收藏 / 固定两个独立标记。只在服务端返回后更新展示（失败时保持原状态并提示）；
 * 只读账号只显示已有标记，不提供切换。
 */
export default function NoteFlagButtons({ projectId: pid, note, onChange }: Props) {
  const { message } = App.useApp()
  const { token } = theme.useToken()
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(pid)
  const [busy, setBusy] = useState<keyof NoteFlags | null>(null)
  const busyRef = useRef(false)

  const toggle = async (flag: keyof NoteFlags) => {
    if (busyRef.current || !canEditNow()) return
    busyRef.current = true
    setBusy(flag)
    try {
      const updated = await noteApi.setFlags(pid, note.id, { [flag]: !note[flag] })
      if (!canReadNow()) return
      onChange?.(updated)
      invalidateProject(pid, ['notes'])
    } catch (err) {
      if (canReadNow()) message.error(errorMessage(err))
    } finally {
      busyRef.current = false
      if (canReadNow()) setBusy(null)
    }
  }

  const star = note.is_favorite ? <StarFilled style={{ color: token.colorWarning }} /> : <StarOutlined />
  const pin = note.is_pinned ? <PushpinFilled style={{ color: token.colorPrimary }} /> : <PushpinOutlined />

  if (!canEdit) {
    if (!note.is_favorite && !note.is_pinned) return null
    return <Space size={4}>
      {note.is_favorite && <Tooltip title="已收藏"><span role="img" aria-label="已收藏">{star}</span></Tooltip>}
      {note.is_pinned && <Tooltip title="已固定"><span role="img" aria-label="已固定">{pin}</span></Tooltip>}
    </Space>
  }

  const favoriteLabel = note.is_favorite ? `取消收藏「${note.title}」` : `收藏「${note.title}」`
  const pinLabel = note.is_pinned ? `取消固定「${note.title}」` : `固定「${note.title}」`
  return <Space size={0}>
    <Tooltip title={note.is_favorite ? '取消收藏' : '收藏'}>
      <Button type="text" size="small" icon={star} aria-label={favoriteLabel} aria-pressed={note.is_favorite}
        loading={busy === 'is_favorite'} disabled={busy !== null} onClick={() => { void toggle('is_favorite') }} />
    </Tooltip>
    <Tooltip title={note.is_pinned ? '取消固定（不再出现在章节编辑页笔记栏）' : '固定到章节编辑页笔记栏'}>
      <Button type="text" size="small" icon={pin} aria-label={pinLabel} aria-pressed={note.is_pinned}
        loading={busy === 'is_pinned'} disabled={busy !== null} onClick={() => { void toggle('is_pinned') }} />
    </Tooltip>
  </Space>
}
