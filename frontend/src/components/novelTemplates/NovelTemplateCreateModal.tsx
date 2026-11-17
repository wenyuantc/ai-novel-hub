import { Alert, App, Button, Collapse, Form, Input, Modal, Pagination, Popconfirm, Radio, Select, Space, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, errorMessage } from '../../api/client'
import { novelTemplatesApi, type NovelTemplateDetail, type NovelTemplateOverrides, type NovelTemplatePage, type NovelTemplateSummary } from '../../api/novelTemplates'
import type { ProjectCreate } from '../../api/types'
import { capturePenNameActor } from '../../hooks/usePenNames'
import { clearCreationIntent, createIntentKey, readCreationIntent, sendCreationIntent, storeCreationIntent, type TemplateCreationIntent } from '../../utils/novelTemplateCreation'
import { normalizeTags, type ProjectInfoValues } from '../../utils/projectFields'
import ProjectInfoFields from '../ProjectInfoFields'
import PenNameSelect from '../shelf/PenNameSelect'

interface Props { actorId: number; onClose: () => void; onSwitchBlank: () => void; onCreated: (id: number) => void }
type Values = ProjectCreate & ProjectInfoValues

export default function NovelTemplateCreateModal({ actorId, onClose, onSwitchBlank, onCreated }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<Values>()
  const [pending, setPending] = useState<TemplateCreationIntent | null>(() => readCreationIntent(actorId))
  const [carriedOverrides, setCarriedOverrides] = useState<NovelTemplateOverrides>(() => readCreationIntent(actorId)?.request.overrides ?? {})
  const [selected, setSelected] = useState<NovelTemplateSummary | null>(null)
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const [list, setList] = useState<NovelTemplatePage | null>(null)
  const [loadingList, setLoadingList] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [listVersion, setListVersion] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const [superseded, setSuperseded] = useState(false)
  const [latest, setLatest] = useState<NovelTemplateDetail | null>(null)
  const actor = useRef(capturePenNameActor())
  const alive = useRef(true)
  const listSeq = useRef(0)
  const pendingRef = useRef<TemplateCreationIntent | null>(pending)
  const flight = useRef(false)
  const requestController = useRef<AbortController | null>(null)
  const current = useCallback(() => alive.current && actor.current(), [])

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; listSeq.current += 1; requestController.current?.abort() }
  }, [])
  useEffect(() => {
    const seq = ++listSeq.current
    const controller = new AbortController()
    const isCurrent = () => current() && !controller.signal.aborted && listSeq.current === seq
    void Promise.resolve().then(() => { if (isCurrent()) { setLoadingList(true); setListError(null) } })
    void novelTemplatesApi.list({ page, page_size: 50, q: query }, { signal: controller.signal, isCurrent }).then(
      (result) => { if (isCurrent()) setList(result) },
      (err: unknown) => { if (isCurrent()) { setList(null); setListError(errorMessage(err)) } },
    ).finally(() => { if (isCurrent()) setLoadingList(false) })
    return () => { listSeq.current += 1; controller.abort() }
  }, [page, query, listVersion, current])

  const markSuperseded = () => {
    setSuperseded(true)
    setError('创建请求已更新，请关闭并重新打开建书窗口。')
  }
  const resetIntent = () => {
    const key = pendingRef.current?.request.intent_key
    if (key && !clearCreationIntent(actorId, key)) { markSuperseded(); return false }
    setCarriedOverrides(pendingRef.current?.request.overrides ?? {})
    pendingRef.current = null
    setPending(null); setError(null); setStale(false); setLatest(null)
    return true
  }
  const readLatest = async () => {
    const intent = pendingRef.current
    if (!intent || flight.current || !current()) return
    const controller = new AbortController()
    requestController.current = controller
    const isCurrent = () => current() && !controller.signal.aborted
    flight.current = true; setBusy(true)
    try {
      const result = await novelTemplatesApi.get(intent.template_id, { signal: controller.signal, isCurrent })
      if (isCurrent()) setLatest(result)
    } catch (err) { if (isCurrent()) setError(errorMessage(err)) }
    finally { flight.current = false; if (isCurrent()) setBusy(false); if (requestController.current === controller) requestController.current = null }
  }

  const captureOverrides = (values: Values): NovelTemplateOverrides => {
    const result: NovelTemplateOverrides = { ...carriedOverrides }
    if (form.isFieldTouched('genre')) result.genre = values.genre?.trim() ?? ''
    if (form.isFieldTouched('premise')) result.premise = values.premise?.trim() ?? ''
    if (form.isFieldTouched('synopsis')) result.synopsis = values.synopsis?.trim() ?? ''
    if (form.isFieldTouched('theme')) result.theme = values.theme?.trim() ?? ''
    if (form.isFieldTouched('tags')) result.tags = normalizeTags(values.tags)
    if (form.isFieldTouched('selling_points')) result.selling_points = values.selling_points?.trim() ?? ''
    if (form.isFieldTouched('audience')) result.audience = values.audience?.trim() ?? ''
    if (form.isFieldTouched('target_words')) result.target_words = values.target_words ?? null
    return result
  }

  const submit = async () => {
    if (flight.current || stale || superseded || !current()) return
    flight.current = true
    let intent = pendingRef.current
    if (!intent) {
      if (!selected) { setError('请选择一个小说模板。'); flight.current = false; return }
      const values = await form.validateFields().catch(() => null)
      if (!values || !current()) { flight.current = false; return }
      try {
        const overrides = captureOverrides(values)
        intent = { version: 1, actor_id: actorId, template_id: selected.id, template_name: selected.name, request: {
          expected_revision: selected.revision, intent_key: createIntentKey(), title: values.title.trim(),
          pen_name_id: values.pen_name_id ?? null, ...(Object.keys(overrides).length ? { overrides } : {}),
        } }
        if (readCreationIntent(actorId)) { markSuperseded(); flight.current = false; return }
        if (!storeCreationIntent(intent)) {
          setError('浏览器无法保存重试信息，请允许本站的会话存储后重试。'); flight.current = false; return
        }
      } catch (err) { setError(errorMessage(err)); flight.current = false; return }
      pendingRef.current = intent
      setPending(intent)
    }
    const controller = new AbortController()
    requestController.current = controller
    const isCurrent = () => current() && !controller.signal.aborted
    setBusy(true); setError(null)
    try {
      const { result: project, accepted } = await sendCreationIntent(intent, () => novelTemplatesApi.createProject(intent.template_id, intent.request, { signal: controller.signal, isCurrent }), isCurrent)
      if (isCurrent()) {
        if (!accepted || readCreationIntent(actorId)) { markSuperseded(); return }
        pendingRef.current = null; setPending(null)
        message.success('作品已从模板创建')
        onCreated(project.id)
      }
    } catch (err) {
      if (!isCurrent()) return
      const stored = readCreationIntent(actorId)
      if (stored && stored.request.intent_key !== intent.request.intent_key) { markSuperseded(); return }
      setError(errorMessage(err))
      if (err instanceof ApiError && err.status === 409 && err.body?.code === 'stale_revision') setStale(true)
    } finally {
      flight.current = false
      if (isCurrent()) setBusy(false)
      if (requestController.current === controller) requestController.current = null
    }
  }

  return <Modal open title="新建作品" onCancel={onClose} maskClosable={!busy} closable={!busy} keyboard={!busy}
    onOk={() => void submit()} okText={pending ? '使用同一请求重试' : '创建并查看作品'} cancelText="取消"
    confirmLoading={busy} okButtonProps={{ disabled: stale || superseded || (!pending && !selected) }} cancelButtonProps={{ disabled: busy }}>
    <Form form={form} layout="vertical" preserve={false} disabled={busy || !!pending} initialValues={{
      title: pending?.request.title, pen_name_id: pending?.request.pen_name_id, ...pending?.request.overrides,
    }}>
      <Form.Item label="创建方式"><Radio.Group value="template" disabled={!!pending} onChange={onSwitchBlank} options={[{ label: '空白创建', value: 'blank' }, { label: '使用小说模板', value: 'template' }]} /></Form.Item>
      {!pending && <>
        <Form.Item label="小说模板" required>
          <Select<number> aria-label="选择小说模板" showSearch filterOption={false} onSearch={(value) => { setQuery(value); setPage(1) }} loading={loadingList}
            placeholder="搜索并选择个人模板" value={selected?.id} onChange={(id) => setSelected(list?.items.find((item) => item.id === id) ?? (selected?.id === id ? selected : null))}
            options={[...(selected && !list?.items.some((item) => item.id === selected.id) ? [selected] : []), ...(list?.items ?? [])].map((item) => ({ value: item.id, label: `${item.name} · 版本 ${item.revision}` }))} />
        </Form.Item>
        {listError && <Alert type="error" showIcon message={listError} action={<Button size="small" onClick={() => setListVersion((value) => value + 1)}>重试</Button>} />}
        {(list?.total ?? 0) > 50 && <Pagination current={page} total={list?.total} pageSize={50} showSizeChanger={false} onChange={setPage} size="small" />}
        {!loadingList && !listError && !list?.total && <Typography.Text type="secondary">暂无匹配模板。<Link to="/novel-templates">管理小说模板</Link></Typography.Text>}
        {selected && <Alert type="info" showIcon message={`${selected.name} · 版本 ${selected.revision}`} description={`角色 ${selected.counts.characters}、世界观 ${selected.counts.world_settings}、组织 ${selected.counts.organizations}、职业 ${selected.counts.careers}、章纲 ${selected.counts.outlines}。新书正文为空。`} />}
      </>}
      <Form.Item name="title" label="新书名" rules={[{ required: true, whitespace: true, message: '请输入书名' }, { max: 200, message: '不超过 200 字' }]}><Input placeholder="例如：星河尽头" /></Form.Item>
      <Form.Item name="pen_name_id" label="作品笔名" extra="由本次创建选择；未设置时使用账号名"><PenNameSelect /></Form.Item>
      <Collapse ghost size="small" items={[{ key: 'overrides', label: '可选：修改本书立项信息', children: <>
        <Typography.Text type="secondary">未修改的字段沿用模板；仅本次填写或清空的字段覆盖模板内容。</Typography.Text>
        <Form.Item name="genre" label="题材" rules={[{ max: 100, message: '不超过 100 字' }]}><Input /></Form.Item>
        <Form.Item name="premise" label="一句话设定" rules={[{ max: 5000, message: '不超过 5000 字' }]}><Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} /></Form.Item>
        <ProjectInfoFields />
      </> }]} />
    </Form>
    {pending && <Alert type="info" showIcon style={{ marginBottom: 12 }} message={`待确认的创建请求：${pending.template_name ?? list?.items.find((item) => item.id === pending.template_id && item.revision === pending.request.expected_revision)?.name ?? '所选模板'} · 版本 ${pending.request.expected_revision}`}
      description="重试会使用同一请求寻找已创建的作品；未确认结果前，书名、模板和笔名保持不变。" />}
    {error && <Alert type="error" showIcon message={error} description={pending ? '请求信息已保留，可以使用同一请求重试。' : undefined} />}
    {stale && !superseded && <Alert type="warning" showIcon message="模板版本已变化" description={<Space wrap><span>请核对最新版本，当前书名及输入会保留。</span><Button size="small" loading={busy} onClick={() => void readLatest()}>读取最新版本</Button>
      {latest && <><span>{latest.name} · 版本 {latest.revision} · 角色 {latest.counts.characters}、世界观 {latest.counts.world_settings}、组织 {latest.counts.organizations}、职业 {latest.counts.careers}、章纲 {latest.counts.outlines}</span><Button size="small" onClick={() => { if (resetIntent()) setSelected(latest) }}>保留输入并采用此版本</Button></>}</Space>} />}
    {pending && !superseded && <Popconfirm title="开始另一次创建？" description="上次请求可能已成功，可先重试同一请求或在书架核对。" okText="开始另一次" cancelText="继续等待" onConfirm={() => { resetIntent() }}>
      <Button type="link" disabled={busy}>开始另一次创建</Button>
    </Popconfirm>}
  </Modal>
}
