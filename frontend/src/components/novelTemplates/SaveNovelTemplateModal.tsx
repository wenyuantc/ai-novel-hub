import { Alert, App, Button, Checkbox, Form, Input, Modal, Select, Space, Spin, Typography } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage, request } from '../../api/client'
import { novelTemplatesApi, type NovelTemplateDetail, type NovelTemplatePreview, type NovelTemplateSelection, type NovelTemplateSummary, type TemplateCandidates } from '../../api/novelTemplates'
import type { Project } from '../../api/types'
import { capturePenNameActor, usePenNameIdentity } from '../../hooks/usePenNames'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../../stores/project'
import { addTemplateDependencies, EMPTY_TEMPLATE_SELECTION, templateSelectionSize, templateSizeLabel } from '../../utils/novelTemplateSelection'
import IncludedDefinitions from './IncludedDefinitions'

interface Props {
  projectId?: number
  projectTitle?: string
  replacement?: NovelTemplateSummary
  onClose: () => void
  onSaved: () => void
}

export default function SaveNovelTemplateModal(props: Props) {
  const identity = usePenNameIdentity()
  return <SaveModalInner key={`${identity}:${props.projectId ?? ''}:${props.replacement?.id ?? ''}`} {...props} />
}

function SaveModalInner({ projectId, projectTitle, replacement, onClose, onSaved }: Props) {
  const { message } = App.useApp()
  const [sources, setSources] = useState<Project[]>([])
  const [sourceId, setSourceId] = useState<number | undefined>(projectId)
  const [name, setName] = useState(projectTitle ? `${projectTitle}模板` : '')
  const [description, setDescription] = useState('')
  const [selection, setSelection] = useState<NovelTemplateSelection>(() => ({ ...EMPTY_TEMPLATE_SELECTION }))
  const [candidates, setCandidates] = useState<TemplateCandidates | null>(null)
  const [preview, setPreview] = useState<NovelTemplatePreview | null>(null)
  const [loadingSources, setLoadingSources] = useState(false)
  const [loadingCandidates, setLoadingCandidates] = useState(false)
  const [candidateVersion, setCandidateVersion] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [staleRevision, setStaleRevision] = useState(false)
  const [latest, setLatest] = useState<NovelTemplateDetail | null>(null)
  const [revision, setRevision] = useState(replacement?.revision ?? 0)
  const alive = useRef(true)
  const generation = useRef(0)
  const flight = useRef(false)
  const activeController = useRef<AbortController | null>(null)
  const actorCurrent = useRef(capturePenNameActor())
  const sourceScope = useRef(projectId ? captureProjectScope(projectId) : null)

  const current = useCallback(() => alive.current && actorCurrent.current() && (!projectId || (
    isProjectScopeCurrent(sourceScope.current) && useProjectStore.getState().project?.access.role === 'owner'
  )), [projectId])
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; generation.current += 1; activeController.current?.abort() }
  }, [])

  useEffect(() => {
    if (projectId) return
    const controller = new AbortController()
    const isCurrent = () => current() && !controller.signal.aborted
    void Promise.resolve().then(() => { if (isCurrent()) setLoadingSources(true) })
    void request<Project[]>('/projects', { signal: controller.signal, isCurrent }).then(
      (items) => { if (isCurrent()) setSources(items.filter((item) => item.access?.role === 'owner' && item.access.can_read)) },
      (err: unknown) => { if (isCurrent()) setError(errorMessage(err)) },
    ).finally(() => { if (isCurrent()) setLoadingSources(false) })
    return () => controller.abort()
  }, [projectId, current])

  useEffect(() => {
    if (!sourceId) return
    const seq = ++generation.current
    const controller = new AbortController()
    const isCurrent = () => current() && !controller.signal.aborted && generation.current === seq
    void Promise.resolve().then(() => { if (isCurrent()) { setLoadingCandidates(true); setError(null) } })
    void novelTemplatesApi.candidates(sourceId, { signal: controller.signal, isCurrent }).then(
      (result) => { if (isCurrent()) setCandidates(result) },
      (err: unknown) => { if (isCurrent()) { setCandidates(null); setError(errorMessage(err)) } },
    ).finally(() => { if (isCurrent()) setLoadingCandidates(false) })
    return () => { generation.current += 1; controller.abort() }
  }, [sourceId, candidateVersion, current])

  const updateSelection = (next: NovelTemplateSelection) => { setSelection(next); setPreview(null); setError(null) }
  const chooseSource = (id: number) => {
    generation.current += 1
    setSourceId(id); setCandidates(null); setSelection({ ...EMPTY_TEMPLATE_SELECTION }); setPreview(null); setError(null)
  }
  const runPreview = async () => {
    if (!sourceId || busy || flight.current || !current()) return
    if (templateSelectionSize(selection) > 2000 || selection.outline_ids.length > 2000) {
      setError('设定与章纲各最多选择 2000 项，请减少选择。'); return
    }
    const seq = ++generation.current
    const controller = new AbortController()
    flight.current = true
    activeController.current = controller
    const isCurrent = () => current() && !controller.signal.aborted && generation.current === seq
    setBusy(true); setError(null); setPreview(null)
    try {
      const result = await novelTemplatesApi.preview(sourceId, selection, { signal: controller.signal, isCurrent })
      if (isCurrent()) setPreview(result)
    } catch (err) {
      if (isCurrent()) {
        setError(errorMessage(err))
        if (err instanceof ApiError && [403, 404].includes(err.status)) {
          setCandidates(null)
          setPreview(null)
        }
      }
    }
    finally { flight.current = false; if (isCurrent()) setBusy(false); if (activeController.current === controller) activeController.current = null }
  }
  const loadLatest = async () => {
    if (!replacement || busy || flight.current || !current()) return
    const controller = new AbortController()
    activeController.current = controller
    const isCurrent = () => current() && !controller.signal.aborted
    flight.current = true
    setBusy(true)
    try {
      const value = await novelTemplatesApi.get(replacement.id, { signal: controller.signal, isCurrent })
      if (isCurrent()) setLatest(value)
    } catch (err) { if (isCurrent()) setError(errorMessage(err)) }
    finally { flight.current = false; if (isCurrent()) setBusy(false); if (activeController.current === controller) activeController.current = null }
  }
  const save = async () => {
    if (!sourceId || !preview || !preview.included_definitions || preview.missing_dependencies.length || busy || flight.current || staleRevision || !current()) return
    const cleanName = name.trim()
    if (!replacement && (!cleanName || Array.from(cleanName).length > 100)) { setError('请输入不超过 100 字的模板名称。'); return }
    const seq = ++generation.current
    const controller = new AbortController()
    flight.current = true
    activeController.current = controller
    const isCurrent = () => current() && !controller.signal.aborted && generation.current === seq
    setBusy(true); setError(null)
    const capture = { source_project_id: sourceId, selection, source_stamp: preview.source_stamp }
    try {
      if (replacement) await novelTemplatesApi.replace(replacement.id, { ...capture, expected_revision: revision }, { signal: controller.signal, isCurrent })
      else await novelTemplatesApi.create({ ...capture, name: cleanName, description: description.trim() }, { signal: controller.signal, isCurrent })
      if (isCurrent()) { message.success(replacement ? '模板内容已更新' : '已保存为小说模板'); onSaved() }
    } catch (err) {
      if (!isCurrent()) return
      setError(errorMessage(err))
      if (err instanceof ApiError && [403, 404].includes(err.status)) {
        setCandidates(null)
        setPreview(null)
      }
      if (err instanceof ApiError && err.status === 409) {
        if (err.body?.code === 'stale_revision') setStaleRevision(true)
        else setPreview(null)
      }
    } finally { flight.current = false; if (isCurrent()) setBusy(false); if (activeController.current === controller) activeController.current = null }
  }

  return <Modal open title={replacement ? `从作品更新模板：${replacement.name}` : '保存为小说模板'} onCancel={onClose}
    footer={<Space wrap><Button onClick={onClose} disabled={busy}>取消</Button><Button onClick={() => void runPreview()} disabled={!sourceId || !candidates || busy} loading={busy && !preview}>预览所选内容</Button><Button type="primary" onClick={() => void save()} loading={busy} disabled={!preview || !preview.included_definitions || !!preview.missing_dependencies.length || staleRevision}>确认保存</Button></Space>}
    width={760} maskClosable={!busy} closable={!busy} keyboard={!busy}>
    <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
      <Typography.Text type="secondary">保存已提交的当前档案值（包含已体现的计划变化），不包含未保存草稿。新书以这些值作为起点；正文、章节和历史不会复制。缩略图随所选设定保存。</Typography.Text>
      {error && <Alert type="error" showIcon message={error} description="当前选择和输入已保留；如来源已变化，请重新预览。" />}
      {staleRevision && <Alert type="warning" showIcon message="模板已在其他位置修改" description={<Space wrap><span>请核对最新版本后再替换，当前选择不会丢失。</span><Button size="small" onClick={() => void loadLatest()} loading={busy}>读取最新版本</Button>{latest && <><span>当前版本：{latest.revision}，名称：{latest.name}</span><Button size="small" onClick={() => { setRevision(latest.revision); setLatest(null); setStaleRevision(false); setError(null) }}>保留选择并采用此版本</Button></>}</Space>} />}
      {!projectId && <Form layout="vertical"><Form.Item label="来源作品" required><Select<number> aria-label="来源作品" showSearch optionFilterProp="label" disabled={busy} loading={loadingSources} value={sourceId} onChange={chooseSource}
        placeholder="选择自己的作品" options={sources.map((item) => ({ value: item.id, label: item.title }))} /></Form.Item></Form>}
      {projectId && <Typography.Text>来源作品：{projectTitle}</Typography.Text>}
      {!replacement && <Form layout="vertical"><Form.Item label="模板名称" required><Input aria-label="模板名称" maxLength={100} value={name} onChange={(event) => setName(event.target.value)} disabled={busy} /></Form.Item><Form.Item label="说明"><Input.TextArea aria-label="模板说明" maxLength={2000} value={description} onChange={(event) => setDescription(event.target.value)} disabled={busy} autoSize={{ minRows: 2, maxRows: 4 }} /></Form.Item></Form>}
      <Checkbox checked={selection.include_config} disabled={busy || !sourceId} onChange={(event) => updateSelection({ ...selection, include_config: event.target.checked })}>保存常用配置、风格、字段与分类</Checkbox>
      <Spin spinning={loadingCandidates}>
        {sourceId && !candidates && !loadingCandidates && <Button onClick={() => setCandidateVersion((value) => value + 1)}>重新加载来源资料</Button>}
        {candidates && <Form layout="vertical" disabled={busy}>
          <Form.Item label="角色"><Select<number[]> mode="multiple" aria-label="选择角色" showSearch optionFilterProp="label" value={selection.character_ids} onChange={(values) => updateSelection({ ...selection, character_ids: values })} options={candidates.characters.map((item) => ({ value: item.id, label: item.name }))} placeholder="可不选择" /></Form.Item>
          <Form.Item label="世界观（含地点）"><Select<number[]> mode="multiple" aria-label="选择世界观" showSearch optionFilterProp="label" value={selection.world_ids} onChange={(values) => updateSelection({ ...selection, world_ids: values })} options={candidates.world.map((item) => ({ value: item.id, label: item.title }))} placeholder="可不选择" /></Form.Item>
          <Form.Item label="组织"><Select<number[]> mode="multiple" aria-label="选择组织" showSearch optionFilterProp="label" value={selection.organization_ids} onChange={(values) => updateSelection({ ...selection, organization_ids: values })} options={candidates.organizations.map((item) => ({ value: item.id, label: item.name }))} placeholder="可不选择" /></Form.Item>
          <Form.Item label="职业"><Select<number[]> mode="multiple" aria-label="选择职业" showSearch optionFilterProp="label" value={selection.career_ids} onChange={(values) => updateSelection({ ...selection, career_ids: values })} options={candidates.careers.map((item) => ({ value: item.id, label: item.name }))} placeholder="可不选择" /></Form.Item>
          <Form.Item label="章纲（含细纲）"><Select<number[]> mode="multiple" aria-label="选择章纲" showSearch optionFilterProp="label" value={selection.outline_ids} onChange={(values) => updateSelection({ ...selection, outline_ids: values })} options={candidates.outlines.map((item) => ({ value: item.id, label: `第 ${item.chapter_no} 章 · ${item.title}` }))} placeholder="可不选择" /></Form.Item>
        </Form>}
      </Spin>
      <Typography.Text type="secondary">上限：设定 2000 项、章纲 2000 项、细纲 5000 项；内容 10 MiB，图片合计 50 MiB。</Typography.Text>
      {preview && <div aria-label="模板内容预览">
        <Typography.Text strong>预览结果</Typography.Text>
        <p>角色 {preview.counts.characters} · 世界观 {preview.counts.world_settings} · 组织 {preview.counts.organizations} · 职业 {preview.counts.careers} · 章纲 {preview.counts.outlines} · 细纲 {preview.counts.plans} · 图片 {preview.counts.images}</p>
        <p>保留关联 {preview.included_relations} 项，省略关联 {preview.omitted_relations} 项；预计 {templateSizeLabel(preview.estimated_bytes)}。</p>
        {preview.included_definitions ? <IncludedDefinitions definitions={preview.included_definitions} /> : <Alert type="error" showIcon message="预览未包含配套定义清单，请更新服务后重新预览。" />}
        {preview.warnings.map((warning, index) => <Alert key={`${index}:${warning}`} type="warning" showIcon message={warning} />)}
        {preview.missing_dependencies.length > 0 && <Alert type="warning" showIcon message="请先补选必要依赖" description={<><ul>{preview.missing_dependencies.map((item) => <li key={`${item.kind}:${item.id}:${item.reason}`}>{item.name}：{item.reason}</li>)}</ul><Button size="small" onClick={() => updateSelection(addTemplateDependencies(selection, preview.missing_dependencies))}>补选这些条目</Button><Typography.Text type="secondary"> 补选后请重新预览。</Typography.Text></>} />}
      </div>}
    </Space>
  </Modal>
}
