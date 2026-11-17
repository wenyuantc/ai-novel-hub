import { Alert, App, Button, Card, Empty, Form, Input, List, Modal, Pagination, Popconfirm, Select, Space, Spin, Tag, Typography } from 'antd'
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, errorMessage } from '../api/client'
import { novelTemplatesApi, type NovelTemplateContentGroup, type NovelTemplateDetail, type NovelTemplatePage, type NovelTemplateSummary } from '../api/novelTemplates'
import { StudioPage } from '../components/StudioUI'
import { capturePenNameActor, usePenNameIdentity } from '../hooks/usePenNames'
import { formatTime } from '../utils/time'

const SaveNovelTemplateModal = lazy(() => import('../components/novelTemplates/SaveNovelTemplateModal'))

export default function NovelTemplatesPage() {
  const identity = usePenNameIdentity()
  return <NovelTemplatesContent key={identity} />
}

function NovelTemplatesContent() {
  const { message } = App.useApp()
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const [version, setVersion] = useState(0)
  const [data, setData] = useState<NovelTemplatePage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detail, setDetail] = useState<NovelTemplateSummary | null>(null)
  const [editing, setEditing] = useState<NovelTemplateSummary | null>(null)
  const [replacement, setReplacement] = useState<NovelTemplateSummary | null>(null)
  const [creating, setCreating] = useState(false)
  const [removing, setRemoving] = useState<number | null>(null)
  const alive = useRef(true)
  const actor = useRef(capturePenNameActor())
  const seq = useRef(0)
  const removeController = useRef<AbortController | null>(null)
  const current = useCallback(() => alive.current && actor.current(), [])
  const reload = () => setVersion((value) => value + 1)

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; seq.current += 1; removeController.current?.abort() }
  }, [])
  useEffect(() => {
    const id = ++seq.current
    const controller = new AbortController()
    const isCurrent = () => current() && !controller.signal.aborted && id === seq.current
    void Promise.resolve().then(() => { if (isCurrent()) { setLoading(true); setError(null) } })
    void novelTemplatesApi.list({ page, page_size: 20, q: query }, { signal: controller.signal, isCurrent }).then(
      (result) => { if (isCurrent()) setData(result) },
      (err: unknown) => { if (isCurrent()) { setData(null); setError(errorMessage(err)) } },
    ).finally(() => { if (isCurrent()) setLoading(false) })
    return () => { seq.current += 1; controller.abort() }
  }, [page, query, version, current])

  const remove = async (template: NovelTemplateSummary) => {
    if (!current() || removeController.current) return
    const controller = new AbortController()
    removeController.current = controller
    const isCurrent = () => current() && !controller.signal.aborted
    setRemoving(template.id)
    try {
      await novelTemplatesApi.remove(template.id, template.revision, { signal: controller.signal, isCurrent })
      if (isCurrent()) { message.success('模板已删除'); reload() }
    } catch (err) {
      if (isCurrent()) { message.error(errorMessage(err)); if (err instanceof ApiError && err.status === 409) reload() }
    } finally { if (isCurrent()) setRemoving(null); if (removeController.current === controller) removeController.current = null }
  }
  return <StudioPage title="小说模板" hint="把常用配置和选定的故事资料保存为个人创作起点。"
    action={<Space wrap><Link to="/projects">返回作品书库</Link><Button type="primary" onClick={() => setCreating(true)}>从作品保存</Button></Space>}>
    <Card>
      <Input.Search aria-label="搜索小说模板" placeholder="按模板名称搜索" allowClear enterButton="搜索" maxLength={100}
        onSearch={(value) => { setPage(1); setQuery(value.trim()) }} style={{ maxWidth: 380, marginBottom: 16 }} />
      {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={reload}>重试</Button>} />}
      <Spin spinning={loading}>
        <List dataSource={data?.items ?? []} rowKey="id" locale={{ emptyText: loading || error ? null : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有小说模板，可在自己的作品概览中保存。" /> }}
          renderItem={(item) => <List.Item>
            <div style={{ width: '100%', minWidth: 0 }}>
              <Space wrap style={{ maxWidth: '100%' }}><Typography.Text strong style={{ overflowWrap: 'anywhere' }}>{item.name}</Typography.Text><Tag>版本 {item.revision}</Tag></Space>
              {item.description && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginBlock: 6 }}>{item.description}</p>}
              <Typography.Text type="secondary" style={{ overflowWrap: 'anywhere' }}>来源：{item.source_title} · 保存于 {formatTime(item.created_at)} · 更新于 {formatTime(item.updated_at)}</Typography.Text>
              <p><Typography.Text type="secondary">角色 {item.counts.characters} · 世界观 {item.counts.world_settings} · 组织 {item.counts.organizations} · 职业 {item.counts.careers} · 章纲 {item.counts.outlines} · 图片 {item.counts.images}</Typography.Text></p>
              <Space wrap><Button size="small" onClick={() => setDetail(item)}>查看内容</Button><Button size="small" onClick={() => setEditing(item)}>编辑名称与说明</Button><Button size="small" onClick={() => setReplacement(item)}>从作品更新内容</Button>
                <Popconfirm title="删除这个模板？" description="已用模板创建的作品不会删除。" okText="删除" cancelText="取消" onConfirm={() => void remove(item)}><Button danger size="small" loading={removing === item.id}>删除</Button></Popconfirm>
              </Space>
            </div>
          </List.Item>} />
      </Spin>
      {(data?.total ?? 0) > 20 && <Pagination current={page} pageSize={20} total={data?.total} onChange={setPage} showSizeChanger={false} />}
    </Card>
    {detail && <TemplateDetailModal item={detail} onClose={() => setDetail(null)} />}
    {editing && <TemplateNameModal item={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload() }} />}
    {creating && <Suspense fallback={null}><SaveNovelTemplateModal onClose={() => setCreating(false)} onSaved={() => { setCreating(false); setPage(1); reload() }} /></Suspense>}
    {replacement && <Suspense fallback={null}><SaveNovelTemplateModal replacement={replacement} onClose={() => setReplacement(null)} onSaved={() => { setReplacement(null); reload() }} /></Suspense>}
  </StudioPage>
}

const GROUPS: { value: NovelTemplateContentGroup; label: string }[] = [
  { value: 'characters', label: '角色' }, { value: 'world_settings', label: '世界观' },
  { value: 'organizations', label: '组织' }, { value: 'careers', label: '职业' }, { value: 'outlines', label: '章纲' },
]

function TemplateDetailModal({ item, onClose }: { item: NovelTemplateSummary; onClose: () => void }) {
  const [group, setGroup] = useState<NovelTemplateContentGroup>('characters')
  const [page, setPage] = useState(1)
  const [detail, setDetail] = useState<NovelTemplateDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const actor = useRef(capturePenNameActor())
  const seq = useRef(0)
  useEffect(() => {
    const id = ++seq.current
    const controller = new AbortController()
    const isCurrent = () => actor.current() && !controller.signal.aborted && id === seq.current
    void Promise.resolve().then(() => { if (isCurrent()) { setLoading(true); setError(null) } })
    void novelTemplatesApi.get(item.id, { signal: controller.signal, isCurrent }, { group, page, page_size: 20 }).then(
      (result) => { if (isCurrent()) setDetail(result) },
      (err: unknown) => { if (isCurrent()) { setDetail(null); setError(errorMessage(err)) } },
    ).finally(() => { if (isCurrent()) setLoading(false) })
    return () => { seq.current += 1; controller.abort() }
  }, [item.id, group, page, version])
  return <Modal open title={`模板内容：${item.name}`} onCancel={onClose} footer={<Button onClick={onClose}>关闭</Button>} width={680}>
    {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => setVersion((value) => value + 1)}>重试</Button>} />}
    {detail && <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Text type="secondary">版本 {detail.revision} · {detail.include_config ? '包含常用配置、风格及字段分类' : '未选择常用配置；必要的配套定义仍会保留'}</Typography.Text>
      {detail.config_summary && <div>
        <Typography.Text>题材：{detail.config_summary.genre || '未填写'} · 风格：{detail.config_summary.default_style_name || '未设置'}</Typography.Text>
        {detail.config_summary.premise && <p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginBlock: 6 }}>{detail.config_summary.premise}</p>}
        <Typography.Text type="secondary">字段 {detail.config_summary.field_count} 项 · 分类 {detail.config_summary.category_count} 项</Typography.Text>
      </div>}
      <Select<NovelTemplateContentGroup> aria-label="内容分组" value={group} onChange={(value) => { setGroup(value); setPage(1); setDetail(null) }} options={GROUPS} style={{ width: 160 }} />
      <Spin spinning={loading}><List dataSource={detail.contents[group] ?? []} rowKey="ref" locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此分组没有内容" /> }} renderItem={(entry) => <List.Item><Space>
        {entry.has_image && <img src={novelTemplatesApi.imageUrl(detail.id, entry.ref, detail.revision)} alt={`${entry.name}的缩略图`} width={40} height={40} loading="lazy" style={{ objectFit: 'cover', borderRadius: 4 }} />}
        <span style={{ overflowWrap: 'anywhere' }}>{entry.name}</span>
      </Space></List.Item>} /></Spin>
      {(detail.counts[group] ?? 0) > 20 && <Pagination current={page} pageSize={20} total={detail.counts[group]} onChange={setPage} showSizeChanger={false} />}
    </Space>}
    {!detail && !error && <Spin spinning />}
  </Modal>
}

function TemplateNameModal({ item, onClose, onSaved }: { item: NovelTemplateSummary; onClose: () => void; onSaved: () => void }) {
  const { message } = App.useApp()
  const [name, setName] = useState(item.name)
  const [description, setDescription] = useState(item.description)
  const [revision, setRevision] = useState(item.revision)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [review, setReview] = useState(false)
  const [latest, setLatest] = useState<NovelTemplateDetail | null>(null)
  const alive = useRef(true)
  const actor = useRef(capturePenNameActor())
  const activeController = useRef<AbortController | null>(null)
  const current = () => alive.current && actor.current()
  useEffect(() => { alive.current = true; return () => { alive.current = false; activeController.current?.abort() } }, [])
  const save = async () => {
    if (busy || activeController.current || review || !current()) return
    const clean = name.trim()
    if (!clean || Array.from(clean).length > 100) { setError('请输入不超过 100 字的模板名称。'); return }
    const controller = new AbortController()
    activeController.current = controller
    const isCurrent = () => current() && !controller.signal.aborted
    setBusy(true); setError(null)
    try {
      await novelTemplatesApi.rename(item.id, { name: clean, description: description.trim(), expected_revision: revision }, { signal: controller.signal, isCurrent })
      if (isCurrent()) { message.success('模板信息已保存'); onSaved() }
    } catch (err) {
      if (isCurrent()) { setError(errorMessage(err)); if (err instanceof ApiError && err.status === 409) setReview(true) }
    } finally { if (isCurrent()) setBusy(false); if (activeController.current === controller) activeController.current = null }
  }
  const loadLatest = async () => {
    if (!current() || busy || activeController.current) return
    const controller = new AbortController()
    activeController.current = controller
    const isCurrent = () => current() && !controller.signal.aborted
    setBusy(true)
    try { const value = await novelTemplatesApi.get(item.id, { signal: controller.signal, isCurrent }); if (isCurrent()) setLatest(value) }
    catch (err) { if (isCurrent()) setError(errorMessage(err)) }
    finally { if (isCurrent()) setBusy(false); if (activeController.current === controller) activeController.current = null }
  }
  return <Modal open title="编辑模板信息" onCancel={onClose} onOk={() => void save()} okText="保存" cancelText="取消" confirmLoading={busy} okButtonProps={{ disabled: review }} maskClosable={!busy} closable={!busy}>
    {error && <Alert type="error" showIcon message={error} description="输入已保留。" />}
    {review && <Alert type="warning" showIcon message="模板已变化，请核对后重试" description={<Space wrap><Button size="small" onClick={() => void loadLatest()} loading={busy}>读取最新版本</Button>{latest && <><span>当前名称：{latest.name}，版本 {latest.revision}</span><Button size="small" onClick={() => { setRevision(latest.revision); setReview(false); setLatest(null); setError(null) }}>保留输入并采用此版本</Button></>}</Space>} />}
    <Form layout="vertical" disabled={busy} style={{ marginTop: 16 }}>
      <Form.Item label="模板名称" required><Input aria-label="模板名称" maxLength={100} value={name} onChange={(event) => setName(event.target.value)} /></Form.Item>
      <Form.Item label="说明"><Input.TextArea aria-label="模板说明" maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
    </Form>
  </Modal>
}
