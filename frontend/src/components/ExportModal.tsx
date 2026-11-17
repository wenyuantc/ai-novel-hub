import { Alert, App, Button, Checkbox, Collapse, Input, Modal, Radio, Space, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { agentApi } from '../api/agent'
import { chapterApi } from '../api/chapters'
import { errorMessage } from '../api/client'
import { downloadDocument, downloadEpub, type DocumentFormat, type StoryContent, type StoryScope, type StoryUnit } from '../api/exports'
import { noteApi } from '../api/notes'
import { sceneApi } from '../api/scenes'
import { SETTING_ENTRY_TYPE_LABELS, SETTING_ENTRY_TYPE_OPTIONS, settingFieldsApi } from '../api/settingFields'
import type { ChapterBrief, SettingEntryType } from '../api/types'
import { capturePenNameActor } from '../hooks/usePenNames'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore } from '../stores/project'
import { epubAuthorOptions, epubAuthorText, prepareShelfExportScope } from './export/epub'
import ExportPicker, { EXPORT_PICKER_PAGE_SIZE, type ExportPickerLoader } from './export/ExportPicker'
import { buildDocumentRequest, type ExportItem } from './export/selection'

interface Props {
  projectId: number
  open: boolean
  onClose: () => void
  /** 书架没有当前作品 store，打开时需要先加载授权作用域。 */
  fromShelf?: boolean
}

type Format = DocumentFormat | 'epub'

/** 作品可阅读文档组合导出，EPUB 保持原有导出方式。 */
export default function ExportModal({ projectId, open, onClose, fromShelf = false }: Props) {
  return open ? <ExportModalScope key={`${projectId}:${fromShelf}`} projectId={projectId} fromShelf={fromShelf} onClose={onClose} /> : null
}

function ExportModalScope({ projectId, onClose, fromShelf }: Omit<Props, 'open'>) {
  const scopeVersion = useProjectStore((state) => state.scopeVersion)
  const project = useProjectStore((state) => state.project)
  const error = useProjectStore((state) => state.error)
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>(fromShelf ? 'loading' : 'ready')
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    if (!fromShelf) return
    const controller = new AbortController()
    const actorCurrent = capturePenNameActor()
    let active = true
    const isCurrent = () => active && !controller.signal.aborted && actorCurrent()
    void prepareShelfExportScope(projectId, { signal: controller.signal, isCurrent }).then((scope) => {
      if (isCurrent()) setStatus(scope ? 'ready' : 'error')
    })
    return () => { active = false; controller.abort() }
  }, [fromShelf, projectId, attempt])
  if (status === 'loading') return <Modal open title="导出小说" footer={<Button onClick={onClose}>取消</Button>} onCancel={onClose}>
    <Typography.Text type="secondary">正在加载作品及导出权限…</Typography.Text>
  </Modal>
  const projectScope = captureProjectScope(projectId)
  if (status === 'error' || !project || project.id !== projectId || !isProjectScopeCurrent(projectScope, 'canExport')) {
    return <Modal open title="导出小说" footer={<Space><Button onClick={onClose}>关闭</Button>{fromShelf && <Button onClick={() => { setStatus('loading'); setAttempt((value) => value + 1) }}>重试</Button>}</Space>} onCancel={onClose}>
      <Alert type="error" showIcon message={error ?? '无法读取作品或没有导出权限，请刷新后重试。'} />
    </Modal>
  }
  const defaultAuthor = project.pen_name?.name ?? project.owner?.username ?? ''
  return <ExportModalInner key={`${projectId}:${scopeVersion}`} projectId={projectId} onClose={onClose} defaultAuthor={defaultAuthor} />
}

function ExportModalInner({ projectId, onClose, defaultAuthor }: { projectId: number; onClose: () => void; defaultAuthor: string }) {
  const { message } = App.useApp()
  const [format, setFormat] = useState<Format>('txt')
  const [scope, setScope] = useState<StoryScope>('all')
  const [unit, setUnit] = useState<StoryUnit>('chapter')
  const [content, setContent] = useState<StoryContent>('prose')
  const [chapterSelection, setChapterSelection] = useState<ExportItem[]>([])
  const [sceneSelection, setSceneSelection] = useState<ExportItem[]>([])
  const [epubSelection, setEpubSelection] = useState<ExportItem[]>([])
  const [settings, setSettings] = useState<ExportItem[]>([])
  const [conversations, setConversations] = useState<ExportItem[]>([])
  const [notes, setNotes] = useState<ExportItem[]>([])
  const [exporting, setExporting] = useState(false)
  const [authorDraft, setAuthorDraft] = useState<string | null>(null)
  const [includeSynopsis, setIncludeSynopsis] = useState(true)
  const projectScope = useMemo(() => captureProjectScope(projectId), [projectId])
  const mounted = useRef(false)
  const requestId = useRef(0)
  const pending = useRef<AbortController | null>(null)
  const busy = useRef(false)
  const current = useCallback(() => mounted.current && isProjectScopeCurrent(projectScope, 'canExport'), [projectScope])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false; requestId.current += 1; pending.current?.abort() }
  }, [])

  const [chapterState, setChapterState] = useState<{ data?: ChapterBrief[]; loading: boolean; error: string | null }>({
    loading: true, error: null,
  })
  const [chapterRequestVersion, setChapterRequestVersion] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    const isCurrent = () => current() && !controller.signal.aborted
    void chapterApi.list(projectId, controller.signal, isCurrent).then((data) => {
      if (isCurrent()) setChapterState({ data, loading: false, error: null })
    }).catch((err: unknown) => {
      if (isCurrent()) setChapterState({ loading: false, error: errorMessage(err) })
    })
    return () => controller.abort()
  }, [projectId, current, chapterRequestVersion])
  const { data: chapters, loading: loadingChapters, error: chapterError } = chapterState
  const reloadChapters = () => {
    setChapterState({ loading: true, error: null })
    setChapterRequestVersion((version) => version + 1)
  }

  const chapterLoader = useCallback<ExportPickerLoader>(async (page) => {
    if (!chapters) throw new Error('章节目录尚未加载，请重试')
    const items = chapters.map((chapter) => ({
      key: String(chapter.id), label: `第${chapter.chapter_no}章 ${chapter.title || '未命名'}`,
      description: chapter.status === 'generating' ? '生成中' : undefined, disabled: chapter.status === 'generating',
    }))
    return { items: items.slice((page - 1) * EXPORT_PICKER_PAGE_SIZE, page * EXPORT_PICKER_PAGE_SIZE), total: items.length }
  }, [chapters])

  const sceneLoader = useCallback<ExportPickerLoader>(async (page, _query, _category, signal) => {
    if (!chapters) throw new Error('章节目录尚未加载，请重试')
    const chapterStatuses = new Map(chapters.map((chapter) => [chapter.id, chapter.status]))
    const result = await sceneApi.directory(projectId, { offset: (page - 1) * EXPORT_PICKER_PAGE_SIZE, limit: EXPORT_PICKER_PAGE_SIZE },
      signal, () => current() && !signal.aborted)
    return { total: result.total, items: result.items.map((scene) => {
      const status = chapterStatuses.get(scene.chapter_id)
      return {
        key: String(scene.id),
        label: `第${scene.chapter_no}章 ${scene.chapter_title || '未命名'} / ${scene.title || `场景 ${scene.sort + 1}`}`,
        description: status === 'generating' ? '生成中' : status === undefined ? '章节状态未知，请刷新' : undefined,
        disabled: status === undefined || status === 'generating',
      }
    }) }
  }, [chapters, current, projectId])

  const settingLoader = useCallback<ExportPickerLoader>(async (page, query, category, signal) => {
    const result = await settingFieldsApi.listEntries(projectId, {
      page, page_size: EXPORT_PICKER_PAGE_SIZE, q: query || undefined,
      types: category ? [category as SettingEntryType] : undefined,
    }, signal, () => current() && !signal.aborted)
    return { total: result.total, items: result.items.map((entry) => ({
      key: `${entry.reference.kind}:${entry.reference.id}`, label: entry.name,
      description: SETTING_ENTRY_TYPE_LABELS[entry.type],
    })) }
  }, [current, projectId])

  const chatLoader = useCallback<ExportPickerLoader>(async (page, query, _category, signal) => {
    const result = await agentApi.list(projectId, signal, () => current() && !signal.aborted)
    const entries = result.filter((item) => !query || item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()))
      .map((item) => ({ key: String(item.id), label: item.title.trim() || `未命名会话 #${item.id}` }))
    return { total: entries.length, items: entries.slice((page - 1) * EXPORT_PICKER_PAGE_SIZE, page * EXPORT_PICKER_PAGE_SIZE) }
  }, [current, projectId])

  const noteLoader = useCallback<ExportPickerLoader>(async (page, query, _category, signal) => {
    const result = await noteApi.list(projectId, { page, page_size: EXPORT_PICKER_PAGE_SIZE, q: query || undefined },
      signal, () => current() && !signal.aborted)
    return { total: result.total, items: result.items.map((note) => ({ key: String(note.id), label: note.title })) }
  }, [current, projectId])

  const close = () => {
    mounted.current = false
    requestId.current += 1
    pending.current?.abort()
    onClose()
  }

  const handleExport = async () => {
    if (busy.current || !current()) return
    let body
    if (format !== 'epub') {
      try {
        body = buildDocumentRequest({ format, scope, unit, content, chapters: chapterSelection, scenes: sceneSelection,
          settings, conversations, notes })
      } catch (err) {
        message.error(errorMessage(err))
        return
      }
    }
    const ticket = ++requestId.current
    const controller = new AbortController()
    pending.current = controller
    busy.current = true
    setExporting(true)
    const isCurrent = () => current() && requestId.current === ticket && !controller.signal.aborted
    try {
      const result = body
        ? await downloadDocument(projectId, body, { isCurrent, signal: controller.signal })
        : await downloadEpub(projectId, {
          chapterIds: epubSelection.map((item) => Number(item.key)), ...epubAuthorOptions(authorDraft), includeSynopsis,
        }, { isCurrent, signal: controller.signal })
      if (!isCurrent() || !result) return
      message.success(`已导出：${result.filename}`)
      if (result.warnings.length) message.warning({ content: result.warnings.join('；'), duration: 8 })
      close()
    } catch (err) {
      if (isCurrent()) message.error(errorMessage(err))
    } finally {
      if (requestId.current === ticket) {
        pending.current = null
        busy.current = false
        if (mounted.current) setExporting(false)
      }
    }
  }

  const chapterPicker = (selected: ExportItem[], onChange: (items: ExportItem[]) => void, title: string) => {
    if (chapterError) return <Alert type="error" showIcon message={chapterError}
      action={<Button size="small" onClick={reloadChapters}>重试</Button>} />
    if (loadingChapters || !chapters) return <Typography.Text type="secondary">正在加载章节目录…</Typography.Text>
    return <ExportPicker title={title} selected={selected} onChange={onChange} load={chapterLoader}
      isCurrent={current} disabled={exporting} />
  }

  const hasSelectionError = format !== 'epub' && ((scope === 'selected' && !(unit === 'chapter' ? chapterSelection : sceneSelection).length)
    || (scope === 'none' && !settings.length && !conversations.length && !notes.length))

  return (
    <Modal open title="导出小说" okText="导出" cancelText="取消" width={720}
      onOk={() => void handleExport()} onCancel={close} confirmLoading={exporting}
      okButtonProps={{ disabled: hasSelectionError || !projectScope }}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text type="secondary">仅导出已保存的内容；未保存的编辑草稿不会包含在文件中。</Typography.Text>
        <div>
          <Typography.Text strong>格式</Typography.Text><br />
          <Radio.Group aria-label="导出格式" value={format} disabled={exporting} onChange={(event) => setFormat(event.target.value as Format)}>
            <Space wrap>
              <Radio.Button value="txt">TXT</Radio.Button>
              <Radio.Button value="md">Markdown</Radio.Button>
              <Radio.Button value="docx">Word</Radio.Button>
              <Radio.Button value="epub">EPUB</Radio.Button>
            </Space>
          </Radio.Group>
        </div>
        {format === 'epub' ? <>
          <Space wrap>
            <span>作者：</span>
            <Input aria-label="EPUB 作者" value={epubAuthorText(defaultAuthor, authorDraft)} onChange={(event) => setAuthorDraft(event.target.value)}
              maxLength={100} placeholder="留空则不署名" style={{ width: 200, maxWidth: '100%' }} disabled={exporting} />
            <Checkbox checked={includeSynopsis} disabled={exporting} onChange={(event) => setIncludeSynopsis(event.target.checked)}>包含简介页</Checkbox>
          </Space>
          <Typography.Text type="secondary">EPUB 不勾选章节时导出全部；生成中及正文为空的章节会跳过，有分卷时目录按卷嵌套。</Typography.Text>
          {chapterPicker(epubSelection, setEpubSelection, 'EPUB 章节')}
        </> : <>
          <div>
            <Typography.Text strong>正文与摘要范围</Typography.Text><br />
            <Radio.Group aria-label="正文与摘要范围" value={scope} disabled={exporting} onChange={(event) => setScope(event.target.value as StoryScope)}>
              <Space wrap>
                <Radio value="all">全部</Radio><Radio value="selected">自选</Radio><Radio value="none">不包含</Radio>
              </Space>
            </Radio.Group>
          </div>
          {scope !== 'none' && <>
            <div>
              <Typography.Text strong>选择单位</Typography.Text><br />
              <Radio.Group aria-label="选择单位" value={unit} disabled={exporting} onChange={(event) => setUnit(event.target.value as StoryUnit)}>
                <Radio value="chapter">章节</Radio><Radio value="scene">场景</Radio>
              </Radio.Group>
            </div>
            <div>
              <Typography.Text strong>导出内容</Typography.Text><br />
              <Radio.Group aria-label="导出内容" value={content} disabled={exporting} onChange={(event) => setContent(event.target.value as StoryContent)}>
                <Space wrap><Radio value="prose">正文</Radio><Radio value="summary">摘要</Radio><Radio value="both">正文和摘要</Radio></Space>
              </Radio.Group>
            </div>
            <Typography.Text type="secondary">章节摘要来自最新分析，场景摘要来自已保存的场景；生成中的章节不会导出。</Typography.Text>
          </>}
          {scope === 'selected' && (unit === 'chapter'
            ? chapterPicker(chapterSelection, setChapterSelection, '章节')
            : chapterError ? <Alert type="error" showIcon message={chapterError} action={<Button size="small" onClick={reloadChapters}>重试</Button>} />
              : loadingChapters || !chapters ? <Typography.Text type="secondary">正在加载章节状态…</Typography.Text>
                : <ExportPicker title="场景" selected={sceneSelection} onChange={setSceneSelection} load={sceneLoader} isCurrent={current} disabled={exporting} />)}
          <div>
            <Typography.Text strong>创作资料（可按项选择）</Typography.Text><br />
            <Typography.Text type="secondary">设定为当前本书值，可能包含计划章节变化；聊天仅包含本人会话。</Typography.Text>
            <Collapse items={[
              { key: 'settings', label: `设定 · 已选 ${settings.length}`, children: <ExportPicker title="设定" selected={settings} onChange={setSettings}
                load={settingLoader} isCurrent={current} categories={SETTING_ENTRY_TYPE_OPTIONS} search disabled={exporting} /> },
              { key: 'chat', label: `聊天 · 已选 ${conversations.length}`, children: <ExportPicker title="聊天" selected={conversations} onChange={setConversations}
                load={chatLoader} isCurrent={current} search disabled={exporting} /> },
              { key: 'notes', label: `笔记 · 已选 ${notes.length}`, children: <ExportPicker title="笔记" selected={notes} onChange={setNotes}
                load={noteLoader} isCurrent={current} search disabled={exporting} /> },
            ]} />
          </div>
          {hasSelectionError && <Typography.Text type="danger">{scope === 'selected' ? '请选择章节或场景；空选不会自动导出全部。' : '请选择正文、摘要或至少一项创作资料。'}</Typography.Text>}
        </>}
      </Space>
    </Modal>
  )
}
