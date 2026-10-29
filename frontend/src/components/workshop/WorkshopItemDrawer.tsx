import { Alert, App, Button, Drawer, Form, Modal, Space, Spin, Tag, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, errorMessage } from '../../api/client'
import { promptApi } from '../../api/prompts'
import { workshopApi } from '../../api/workshop'
import type { WorkshopItem, WorkshopMetadata } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { useModalWidth } from '../../hooks/useIsMobile'
import { useAuthStore } from '../../stores/auth'
import { useStyleStore } from '../../stores/styles'
import { formatIssue, issuesFromError } from '../../utils/prompts'
import { formatTime } from '../../utils/time'
import { WORKSHOP_CATEGORIES, WORKSHOP_KINDS, workshopImportPairs } from '../../utils/workshop'
import PromptDiffModal from '../prompts/PromptDiffModal'
import WorkshopContent from './WorkshopContent'
import WorkshopMetadataFields from './WorkshopMetadataFields'
import WorkshopItemControls from './WorkshopItemControls'

interface Props {
  itemId: number
  onClose: () => void
  onChanged: () => void
  onUpdate: (item: WorkshopItem) => void
}

/** 每次打开按 id 加载最新详情；key 由父级设置，避免详情与操作状态串到另一个条目。 */
export default function WorkshopItemDrawer({ itemId, onClose, onChanged, onUpdate }: Props) {
  const width = useModalWidth(800)
  const loader = useCallback(() => workshopApi.get(itemId), [itemId])
  const { data, error, setData } = useLoader(loader)
  return <Drawer open title="工坊详情" width={width} onClose={onClose} destroyOnHidden>
    {error ? <Alert showIcon type="error" message={error} /> : data ? <WorkshopItemDetail key={data.id} item={data}
      onItem={(item) => { setData(() => item); onChanged() }} onDeleted={() => { onChanged(); onClose() }} onUpdate={onUpdate} /> : <Spin />}
  </Drawer>
}

interface DetailProps {
  item: WorkshopItem
  onItem: (item: WorkshopItem) => void
  onDeleted: () => void
  onUpdate: (item: WorkshopItem) => void
}
export function WorkshopItemDetail({ item, onItem, onDeleted, onUpdate }: DetailProps) {
  const { message, modal } = App.useApp()
  const isAdmin = useAuthStore((s) => s.user?.is_admin ?? false)
  const active = item.status === 'active'
  const [busy, setBusy] = useState(false)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [metadataOpen, setMetadataOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const templateLoader = useCallback(() => item.kind === 'prompt' && item.prompt_key
    ? promptApi.get(item.prompt_key) : Promise.resolve(null), [item.kind, item.prompt_key])
  const { data: template, loading: templateLoading, error: templateError, setData: setTemplate } = useLoader(templateLoader)
  const run = async (action: () => Promise<WorkshopItem>) => {
    setBusy(true)
    setError(null)
    try { onItem(await action()) } catch (err) { setError(errorMessage(err)) } finally { setBusy(false) }
  }
  const importItem = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await workshopApi.import(item.id)
      setPreviewOpen(false)
      onItem({ ...item, imported: result.imported, download_count: result.download_count })
      message.success(item.kind === 'style' ? '已导入到我的风格库' : '已覆盖并启用模板；导入前内容可从历史版本恢复')
      if (item.kind === 'style') {
        await useStyleStore.getState().load(true)
        const refreshError = useStyleStore.getState().error
        if (refreshError) setError(`导入已成功，风格库刷新失败：${refreshError}`)
      }
    } catch (err) {
      setPreviewOpen(false)
      const issues = issuesFromError(err)
      setError([errorMessage(err), ...issues.map(formatIssue)].join('\n'))
    } finally { setBusy(false) }
  }
  const previewImport = async () => {
    if (!item.prompt_key) return
    setBusy(true)
    setError(null)
    try {
      // 同一详情中可能已经导入或在别页恢复过模板，不能沿用打开抽屉时的旧快照。
      const current = await promptApi.get(item.prompt_key)
      setTemplate(() => current)
      setPreviewOpen(true)
    } catch (err) {
      setError(`无法加载当前生效模板：${errorMessage(err)}`)
    } finally { setBusy(false) }
  }
  const remove = () => modal.confirm({
    title: '删除这个工坊条目？', content: '该条目和提交历史会被删除，已导入的副本会保留。', okText: '删除', cancelText: '取消', okButtonProps: { danger: true },
    onOk: async () => {
      setBusy(true)
      try { await workshopApi.remove(item.id); message.success('已删除'); onDeleted() }
      catch (err) { message.error(errorMessage(err)); throw err } finally { setBusy(false) }
    },
  })
  return <Space direction="vertical" size="middle" style={{ width: '100%', overflowWrap: 'anywhere' }}>
    <Typography.Title level={4} style={{ margin: 0 }}>{item.name}</Typography.Title>
    <Space wrap><Tag>{WORKSHOP_KINDS[item.kind]}</Tag><Tag>{WORKSHOP_CATEGORIES[item.category]}</Tag>
      {item.is_official && <Tag color="blue">官方</Tag>}{!active && <Tag color="orange">{item.hidden_by === 'admin' ? '管理员下架' : '作者下架'}</Tag>}
      {item.imported && <Tag color="green">已导入</Tag>}{item.tags.map((t) => <Tag key={t}>{t}</Tag>)}
    </Space>
    <Typography.Text>作者：{item.author_name ?? '匿名'}{item.is_anonymous && item.author_username && `（匿名分享，真实账号：${item.author_username}）`}</Typography.Text>
    <Typography.Text type="secondary">更新于 {formatTime(item.updated_at)} · 点赞 {item.like_count} · 下载 {item.download_count}</Typography.Text>
    {item.prompt_key && <Typography.Text code>{item.prompt_key}</Typography.Text>}
    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{item.description || '暂无说明'}</Typography.Paragraph>
    {error && <Alert type="error" showIcon message={error} style={{ whiteSpace: 'pre-wrap' }} />}
    {templateError && <Alert type="error" showIcon message="无法导入此模板" description={`当前系统模板不可用或已不存在：${templateError}`} />}
    <WorkshopItemControls item={item} isAdmin={isAdmin} busy={busy}
      importDisabled={item.kind === 'prompt' && (templateLoading || !template || !!templateError)}
      onLike={() => void run(() => item.liked ? workshopApi.unlike(item.id) : workshopApi.like(item.id))}
      onImport={() => item.kind === 'prompt' ? void previewImport() : void importItem()}
      onUpdate={() => onUpdate(item)}
      onHide={() => modal.confirm({ title: '下架这个条目？', content: '下架后其他用户无法浏览和导入。', okText: '下架', cancelText: '取消', onOk: () => run(() => workshopApi.hide(item.id)) })}
      onRestore={() => void run(() => workshopApi.unhide(item.id))}
      onMetadata={() => setMetadataOpen(true)} onDelete={remove} />
    <Link to={item.kind === 'style' ? '/styles' : `/prompt-templates?key=${encodeURIComponent(item.prompt_key ?? '')}`}>{item.kind === 'style' ? '打开风格库' : '打开模板与历史版本'}</Link>
    {item.is_mine && item.hidden_by === 'admin' && !isAdmin && <Alert showIcon type="warning" message="该条目已被管理员下架，请联系管理员处理。" />}
    <WorkshopContent kind={item.kind} content={item.content} />
    {previewOpen && template && <PromptDiffModal open title="导入前确认模板差异" hint="删除的文字来自我当前生效的模板，新增的文字来自工坊。确认后覆盖并启用；原内容可在历史版本恢复。"
      pairs={workshopImportPairs(template, item.content)} onClose={() => { if (!busy) setPreviewOpen(false) }}
      footer={<Space wrap><Button disabled={busy} onClick={() => setPreviewOpen(false)}>取消</Button><Button type="primary" loading={busy} onClick={() => void importItem()}>确认覆盖并启用</Button></Space>} />}
    {metadataOpen && <MetadataModal item={item} onClose={() => setMetadataOpen(false)} onSaved={onItem} />}
  </Space>
}
function MetadataModal({ item, onClose, onSaved }: { item: WorkshopItem; onClose: () => void; onSaved: (item: WorkshopItem) => void }) {
  const [form] = Form.useForm<WorkshopMetadata>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const save = async () => {
    let value: WorkshopMetadata
    try { value = await form.validateFields() } catch { return }
    setBusy(true)
    try { onSaved(await workshopApi.metadata(item.id, value)); onClose() }
    catch (err) { setError(err instanceof ApiError ? err.detail : errorMessage(err)) } finally { setBusy(false) }
  }
  return <Modal open title="管理分类和标签" okText="保存" cancelText="取消" onCancel={() => { if (!busy) onClose() }} onOk={() => void save()} confirmLoading={busy} destroyOnHidden>
    <Typography.Paragraph type="secondary">正文由作者提交更新，管理员仅能调整分类和标签。</Typography.Paragraph>
    {error && <Alert showIcon type="error" message={error} />}
    <Form layout="vertical" form={form} initialValues={{ category: item.category, tags: item.tags }}><WorkshopMetadataFields disabled={busy} /></Form>
  </Modal>
}
