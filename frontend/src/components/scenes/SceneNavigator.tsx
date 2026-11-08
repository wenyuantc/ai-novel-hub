import { ArrowDownOutlined, ArrowUpOutlined, HolderOutlined, PlusOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Checkbox, Drawer, Empty, Space, Tag, theme, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { readChapterScenes, sceneApi } from '../../api/scenes'
import type { ScenePage } from '../../api/types'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import type { SceneDraftController } from './sceneDraftController'
import { sceneDraftDirty } from './sceneDraftController'
import { moveScene, sameSceneIds, sceneLabel } from './scenePresentation'

interface Props {
  projectId: number; chapterId: number; page: ScenePage | null; sceneId?: number; disabled: boolean
  controller: SceneDraftController; ensureSaved: () => Promise<boolean>; onChanged: () => void
  onSelect: (id: number) => Promise<boolean>
  onDraftChange: (dirty: boolean) => void
  open?: boolean
  onClose?: () => void
}
interface OrderDraft { ids: number[]; revision: number; conflict: boolean; latest?: ScenePage }
/** 章内轻量场景目录；拖动和上下移共用拟排序，冲突时保留原方案供核对。 */
export default function SceneNavigator({ projectId, chapterId, page, sceneId, disabled, controller, ensureSaved, onChanged, onSelect, onDraftChange, open: openProp, onClose }: Props) {
  const { message } = App.useApp()
  const { token } = theme.useToken()
  const { canEdit, canEditNow, canReadNow } = useProjectAccess(projectId)
  const isMobile = useIsMobile()
  const [open, setOpen] = useState(false)
  const [order, setOrder] = useState<OrderDraft>()
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')
  const [checked, setChecked] = useState(false)
  useEffect(() => { onDraftChange(!!order) }, [order, onDraftChange])
  useEffect(() => () => onDraftChange(false), [onDraftChange])
  const dragged = useRef<number | null>(null)
  const ids = order?.ids ?? page?.items.map((item) => item.id) ?? []
  const move = (id: number, to: number) => {
    if (!page || page.content_revision === null || !canEditNow() || disabled) return
    setOrder((old) => ({ ids: moveScene(old?.ids ?? page.items.map((item) => item.id), id, to), revision: old?.revision ?? page.content_revision!, conflict: old?.conflict ?? false, latest: old?.latest }))
  }
  const create = async () => {
    if (!canEditNow() || working || disabled || !(await ensureSaved())) return
    const originalScene = controller.getSnapshot().scene
    if (originalScene?.id !== sceneId) return
    const revision = originalScene?.content_revision
    if (revision === undefined) return
    setWorking(true); setError('')
    try {
      const added = await sceneApi.create(projectId, chapterId, { expected_content_revision: revision, title: '新场景', after_scene_id: sceneId })
      if (!canEditNow()) return
      const current = controller.getSnapshot()
      if (current.scene?.id !== originalScene?.id || sceneDraftDirty(current)) {
        await controller.load()
        onChanged()
        if (canReadNow() && current.scene?.id === originalScene?.id) message.warning('新场景已创建；创建期间输入的草稿已保留，请先核对最新版本再切换')
        return
      }
      await controller.load(added.id, false)
      setOrder(undefined); onChanged(); message.success('已创建独立场景')
    } catch (err) { if (canEditNow()) setError(errorMessage(err)) }
    finally { if (canReadNow()) setWorking(false) }
  }
  const saveOrder = async () => {
    if (!order || order.conflict || !canEditNow() || working || disabled) return
    // 未保存稿先保存，但不能偷偷使用已变更的排序版本；保存只改变正文时可沿用同一目录身份。
    const before = controller.getSnapshot().scene?.content_revision
    if (!(await ensureSaved())) return
    const revision = controller.getSnapshot().scene?.content_revision
    const expected = before === order.revision && revision !== undefined ? revision : order.revision
    setWorking(true); setError('')
    try {
      await sceneApi.reorder(projectId, chapterId, { expected_content_revision: expected, scene_ids: order.ids })
      if (!canEditNow()) return
      setOrder(undefined); await controller.load(); onChanged(); message.success('场景顺序已保存')
    } catch (err) {
      if (!canEditNow()) return
      setError(errorMessage(err))
      if (err instanceof ApiError && err.status === 409) setOrder((old) => old ? { ...old, conflict: true } : old)
    } finally { if (canReadNow()) setWorking(false) }
  }
  const review = async () => {
    setWorking(true); setChecked(false)
    try { const latest = await readChapterScenes(projectId, chapterId); if (canReadNow()) setOrder((old) => old ? { ...old, latest } : old) }
    catch (err) { if (canReadNow()) setError(errorMessage(err)) }
    finally { if (canReadNow()) setWorking(false) }
  }
  const list = <Space direction="vertical" size={8} style={{ width: '100%' }}>
    <Space wrap><Typography.Text strong>本章场景（{page?.total ?? 0}）</Typography.Text><Button size="small" icon={<PlusOutlined />} disabled={!canEdit || disabled || working || !!order} onClick={() => void create()}>新增场景</Button><Button size="small" disabled={disabled || working || !!order} onClick={() => void controller.load().then(() => onChanged())}>刷新目录</Button></Space>
    <Typography.Text type="secondary">仅限本章；可拖动手柄或用上移／下移按钮，完成后保存顺序。</Typography.Text>
    {error && <Alert type="error" showIcon message={error} />}
    {!ids.length && <Empty description="场景目录加载中" />}
    {ids.map((id, index) => {
      const scene = page?.items.find((item) => item.id === id)
      if (!scene) return null
      const planning = scene.planning?.effective
      return <Card key={id} size="small" style={{ borderColor: sceneId === id ? token.colorPrimary : undefined }}
        onDragOver={(event) => { if (!disabled && canEdit) event.preventDefault() }} onDrop={(event) => { event.preventDefault(); if (dragged.current !== null) move(dragged.current, index); dragged.current = null }}>
        <Space direction="vertical" size={4} style={{ width: '100%' }}><Space wrap>
          <span draggable={canEdit && !disabled && !working} onDragStart={() => { dragged.current = id }} onDragEnd={() => { dragged.current = null }} title="拖动调整本章场景顺序" style={{ cursor: 'grab' }}><HolderOutlined aria-label="场景拖动手柄" /></span>
          <Button type={id === sceneId ? 'primary' : 'text'} size="small" style={{ maxWidth: '100%', height: 'auto', whiteSpace: 'normal', textAlign: 'start' }} disabled={disabled || working || !!order} onClick={() => void onSelect(id).then((selected) => { if (selected) { setOpen(false); onClose?.() } })}>{sceneLabel(scene)}</Button>
          <Tag>{scene.word_count ? `${scene.word_count} 字` : '未写'}</Tag>
        </Space>
        {scene.summary && <Typography.Paragraph ellipsis={{ rows: 2 }} style={{ marginBottom: 0 }}>{scene.summary}</Typography.Paragraph>}
        {planning && <Space size={4} wrap><Typography.Text type="secondary">POV：{planning.pov_character_name ?? '未指定'}（{planning.sources.pov === 'chapter' ? '继承' : '覆盖'}）</Typography.Text>{planning.tags.map((tag) => <Tag key={tag.id} color={tag.tone}>{tag.name}</Tag>)}</Space>}
        <Space><Button size="small" icon={<ArrowUpOutlined />} aria-label={`上移${sceneLabel(scene)}`} disabled={!canEdit || disabled || working || index === 0} onClick={() => move(id, index - 1)}>上移</Button><Button size="small" icon={<ArrowDownOutlined />} aria-label={`下移${sceneLabel(scene)}`} disabled={!canEdit || disabled || working || index === ids.length - 1} onClick={() => move(id, index + 1)}>下移</Button></Space>
      </Space></Card>
    })}
    {order && <Space wrap><Button type="primary" loading={working} disabled={disabled || order.conflict || !canEdit} onClick={() => void saveOrder()}>保存本章顺序</Button><Button disabled={working} onClick={() => { setOrder(undefined); setError('') }}>放弃拟排序</Button></Space>}
    {order?.conflict && <Alert type="warning" message="拟排序已保留，尚未替换版本" description={<Space direction="vertical"><Button loading={working} onClick={() => void review()}>加载最新顺序供核对</Button>{order.latest && <>
      <Typography.Text>最新顺序：{order.latest.items.map(sceneLabel).join(' → ')}</Typography.Text>
      {sameSceneIds(order.ids, order.latest.items.map((item) => item.id)) ? <><Checkbox checked={checked} onChange={(event) => setChecked(event.target.checked)}>已核对，仍采用上方拟排序</Checkbox><Button disabled={!checked || !canEdit} onClick={() => { if (!canEditNow() || order.latest?.content_revision === null || order.latest?.content_revision === undefined) return; setOrder({ ...order, revision: order.latest.content_revision, conflict: false, latest: undefined }); setError('') }}>使用已核对版本，保留拟排序</Button></> : <Typography.Text type="danger">场景已增删，请先保留记录，再放弃拟排序并重新加载；不能自动合并身份列表。</Typography.Text>}
    </>}</Space>} />}
  </Space>
  if (openProp !== undefined) {
    return (
      <Drawer
        title="场景目录与排序管理"
        open={openProp}
        placement={isMobile ? 'bottom' : 'right'}
        width="min(540px, 100vw)"
        height={isMobile ? '80vh' : undefined}
        onClose={onClose}
      >
        {list}
      </Drawer>
    )
  }
  return isMobile ? <><Button block onClick={() => setOpen(true)} style={{ marginBottom: 12 }}>场景目录 · {page?.items.find((item) => item.id === sceneId)?.title || '当前场景'}</Button><Drawer title="本章场景" open={open} placement="bottom" height="80vh" onClose={() => setOpen(false)}>{list}</Drawer></> : <div role="region" aria-label="本章场景导航与排序" tabIndex={0} style={{ marginBlock: 12, maxHeight: 360, overflow: 'auto', padding: 2 }}>{list}</div>
}
