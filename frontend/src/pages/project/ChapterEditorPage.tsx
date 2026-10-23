import {
  ArrowLeftOutlined,
  HistoryOutlined,
  LeftOutlined,
  RightOutlined,
  SaveOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Input,
  Modal,
  Result,
  Row,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useBlocker, useNavigate, useParams } from 'react-router-dom'
import { versionApi } from '../../api/chapterVersions'
import { chapterApi, CHAPTER_STATUS, countWords, generateChapter, polishChapter } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import type { Chapter, ChapterBrief, ChapterVersionBrief, ContinueEvent } from '../../api/types'
import AIConfigSelect from '../../components/AIConfigSelect'
import { GenerateForm, type GenerateOptions, PolishForm } from '../../components/chapter/ChapterAIForms'
import RewritePanel, { type TextSelection } from '../../components/chapter/RewritePanel'
import VersionDrawer from '../../components/chapter/VersionDrawer'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useLoader } from '../../hooks/useLoader'
import { useStreamTask } from '../../hooks/useStreamTask'
import { useProjectStore } from '../../stores/project'
import { readAutoContinue } from '../../utils/preferences'
import { formatTime } from '../../utils/time'
import { useProjectId } from '../../hooks/useProjectId'

/** 路由入口：以章节 id 为 key，切换章节时整体重建编辑器状态。 */
export default function ChapterEditorPage() {
  const pid = useProjectId()
  const { cid } = useParams()
  const chapterId = Number(cid)
  if (!Number.isFinite(chapterId)) return <Result status="404" title="章节不存在" />
  return <ChapterLoader key={chapterId} pid={pid} cid={chapterId} />
}

function ChapterLoader({ pid, cid }: { pid: number; cid: number }) {
  const navigate = useNavigate()
  const chapterLoader = useCallback(() => chapterApi.get(pid, cid), [pid, cid])
  const { data: chapter, error } = useLoader(chapterLoader)
  const listLoader = useCallback(() => chapterApi.list(pid), [pid])
  const { data: siblings = [], reload: reloadSiblings } = useLoader(listLoader)

  if (error && !chapter) {
    return (
      <Result
        status="warning"
        title="无法打开章节"
        subTitle={error}
        extra={<Button onClick={() => navigate(`/projects/${pid}/chapters`)}>返回章节列表</Button>}
      />
    )
  }
  if (!chapter) {
    return (
      <div style={{ padding: 48, textAlign: 'center' }}>
        <Spin />
      </div>
    )
  }
  return <ChapterEditor pid={pid} initial={chapter} siblings={siblings} onChanged={reloadSiblings} />
}

interface EditorProps {
  pid: number
  initial: Chapter
  siblings: ChapterBrief[]
  onChanged: () => void
}

type AIMode = 'generate' | 'polish' | 'rewrite'

function ChapterEditor({ pid, initial, siblings, onChanged }: EditorProps) {
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const refreshProject = useProjectStore((s) => s.refresh)
  const hasConfig = useHasAIConfig()

  const [chapter, setChapter] = useState<Chapter>(initial)
  const [title, setTitle] = useState(initial.title)
  const [content, setContent] = useState(initial.content)
  const [saving, setSaving] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [aiMode, setAiMode] = useState<AIMode>('generate')
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  const [genOptions, setGenOptions] = useState<GenerateOptions>(() => ({
    targetWords: 3000,
    instruction: '',
    autoContinue: readAutoContinue(),
  }))
  const [polishInstruction, setPolishInstruction] = useState('')
  const [continueInfo, setContinueInfo] = useState<ContinueEvent | null>(null)
  const [selection, setSelection] = useState<TextSelection>({ start: 0, end: 0 })
  const [rewriteLocked, setRewriteLocked] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const task = useStreamTask()
  const scrollRef = useRef<HTMLDivElement>(null)

  const dirty = content !== chapter.content || title !== chapter.title
  const busy = task.running || saving
  // 整章生成中或局部改写预览中：正文只读，避免选区失效
  const editorLocked = task.running || rewriteLocked
  const displayContent = task.running ? task.text : content
  const words = useMemo(() => countWords(displayContent), [displayContent])

  const { prev, next } = useMemo(() => {
    const sorted = [...siblings].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id)
    const idx = sorted.findIndex((c) => c.id === chapter.id)
    return { prev: idx > 0 ? sorted[idx - 1] : undefined, next: idx >= 0 ? sorted[idx + 1] : undefined }
  }, [siblings, chapter.id])

  const applyServerChapter = useCallback((c: Chapter) => {
    setChapter(c)
    setTitle(c.title)
    setContent(c.content)
  }, [])

  /** 服务端改写了正文（生成 / 采纳改写 / 回滚）：同步并把光标记录移到末尾（与浏览器行为一致）。 */
  const replaceFromServer = useCallback(
    (c: Chapter) => {
      applyServerChapter(c)
      setSelection({ start: c.content.length, end: c.content.length })
    },
    [applyServerChapter],
  )

  const syncSelection = (el: HTMLTextAreaElement) => {
    const { selectionStart: start, selectionEnd: end } = el
    setSelection((prev) => (prev.start === start && prev.end === end ? prev : { start, end }))
  }

  const save = useCallback(async (): Promise<boolean> => {
    setSaving(true)
    try {
      const updated = await chapterApi.update(pid, chapter.id, { title, content })
      applyServerChapter(updated)
      onChanged()
      void refreshProject()
      return true
    } catch (err) {
      message.error(errorMessage(err))
      return false
    } finally {
      setSaving(false)
    }
  }, [pid, chapter.id, title, content, applyServerChapter, onChanged, refreshProject, message])

  const handleSave = useCallback(async () => {
    if (editorLocked || saving) return
    if (!dirty) {
      message.info('没有需要保存的修改')
      return
    }
    if (await save()) message.success('已保存')
  }, [editorLocked, saving, dirty, save, message])

  // Ctrl/Cmd + S 保存
  const saveShortcutRef = useRef(handleSave)
  useEffect(() => {
    saveShortcutRef.current = handleSave
  })
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        void saveShortcutRef.current()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // 未保存 / 生成中 / 局部改写预览中（流式或待采纳）离开提示
  const guard = dirty || task.running || rewriteLocked
  useEffect(() => {
    if (!guard) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [guard])
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) => guard && currentLocation.pathname !== nextLocation.pathname,
  )

  // 流式输出时自动滚动到底部
  useEffect(() => {
    const el = scrollRef.current
    if (task.running && el) el.scrollTop = el.scrollHeight
  }, [task.text, task.running])

  const reloadChapter = async () => {
    try {
      const fresh = await chapterApi.get(pid, chapter.id)
      // 只同步服务端状态，保留本地正文（生成失败/中止时正文未变）
      setChapter(fresh)
    } catch {
      // 忽略
    }
  }

  /** AI 操作 / 回滚前：有未保存修改先自动保存。 */
  const ensureSaved = async (): Promise<boolean> => {
    if (!dirty) return true
    if (!(await save())) return false
    message.info('已自动保存当前修改')
    return true
  }

  const runAI = async (mode: 'generate' | 'polish') => {
    if (!(await ensureSaved())) return
    setContinueInfo(null)
    const outcome = await task.start((onDelta, signal) =>
      mode === 'generate'
        ? generateChapter(
            chapter.id,
            {
              ai_config_id: aiConfigId,
              instruction: genOptions.instruction.trim(),
              target_words: genOptions.targetWords,
              // 后端默认 false，必须显式传
              auto_continue: genOptions.autoContinue,
            },
            onDelta,
            signal,
            setContinueInfo,
          )
        : polishChapter(chapter.id, { ai_config_id: aiConfigId, instruction: polishInstruction.trim() }, onDelta, signal).then(
            (c) => ({ chapter: c, warning: undefined }),
          ),
    )
    setContinueInfo(null)
    if (outcome.status === 'done') {
      replaceFromServer(outcome.value.chapter)
      if (outcome.value.warning) message.warning(`生成完成，已保存；${outcome.value.warning}`)
      else message.success(mode === 'generate' ? '生成完成，已保存' : '润色完成，已保存')
      onChanged()
      void refreshProject()
    } else {
      if (outcome.status === 'aborted') message.info('已停止，正文保持原样')
      await reloadChapter()
    }
  }

  const confirmGenerate = () => {
    if (content.trim()) {
      modal.confirm({
        title: '重新生成正文？',
        content: '当前正文会自动存为历史版本，可在「版本历史」中对比或恢复。生成完成后覆盖正文，中途停止则保持原样。',
        okText: '生成',
        cancelText: '取消',
        onOk: () => {
          void runAI('generate')
        },
      })
    } else {
      void runAI('generate')
    }
  }

  const handleRestore = async (v: ChapterVersionBrief): Promise<boolean> => {
    if (!(await ensureSaved())) return false
    try {
      const updated = await versionApi.restore(pid, chapter.id, v.id)
      replaceFromServer(updated)
      message.success(`已恢复到 v${v.seq}`)
      onChanged()
      void refreshProject()
      return true
    } catch (err) {
      message.error(errorMessage(err))
      return false
    }
  }

  const handleRewriteApplied = (c: Chapter) => {
    replaceFromServer(c)
    onChanged()
    void refreshProject()
  }

  const handleResetStatus = async () => {
    setResetting(true)
    try {
      const updated = await chapterApi.update(pid, chapter.id, { status: 'draft' })
      setChapter(updated)
      message.success('已复位为草稿')
      onChanged()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setResetting(false)
    }
  }

  const handleToggleDone = async () => {
    if (dirty && !(await save())) return
    try {
      const updated = await chapterApi.update(pid, chapter.id, {
        status: chapter.status === 'done' ? 'draft' : 'done',
      })
      setChapter(updated)
      onChanged()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const stuck = chapter.status === 'generating' && !task.running
  const status = task.running ? CHAPTER_STATUS.generating : CHAPTER_STATUS[chapter.status]

  return (
    <>
      <Row gutter={16}>
        <Col xs={24} xl={17}>
          <Card
            size="small"
            title={
              <Space wrap>
                <Button
                  type="text"
                  size="small"
                  icon={<ArrowLeftOutlined />}
                  onClick={() => navigate(`/projects/${pid}/chapters`)}
                />
                <span>第 {chapter.chapter_no} 章</span>
                <Tag color={status.color}>{status.label}</Tag>
                <Typography.Text type="secondary">{words} 字</Typography.Text>
                {dirty && !task.running && <Tag color="orange">未保存</Tag>}
              </Space>
            }
            extra={
              <Space wrap>
                <Button
                  size="small"
                  icon={<LeftOutlined />}
                  disabled={!prev}
                  onClick={() => prev && navigate(`/projects/${pid}/chapters/${prev.id}`)}
                >
                  上一章
                </Button>
                <Button
                  size="small"
                  disabled={!next}
                  onClick={() => next && navigate(`/projects/${pid}/chapters/${next.id}`)}
                >
                  下一章 <RightOutlined />
                </Button>
                <Button size="small" icon={<HistoryOutlined />} onClick={() => setVersionsOpen(true)}>
                  版本历史
                </Button>
                <Button size="small" disabled={busy || stuck || rewriteLocked} onClick={() => void handleToggleDone()}>
                  {chapter.status === 'done' ? '标记为草稿' : '标记为完成'}
                </Button>
                <Button
                  type="primary"
                  size="small"
                  icon={<SaveOutlined />}
                  loading={saving}
                  disabled={editorLocked || stuck || !dirty}
                  onClick={() => void handleSave()}
                >
                  保存
                </Button>
              </Space>
            }
          >
            {stuck && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 12 }}
                message="该章节处于「生成中」状态"
                description="可能另一窗口正在生成；如果确认生成已中断（例如服务重启），可复位为草稿后继续编辑。"
                action={
                  <Button size="small" loading={resetting} onClick={() => void handleResetStatus()}>
                    复位
                  </Button>
                }
              />
            )}
            <Input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="章节标题"
              maxLength={200}
              disabled={editorLocked}
              variant="borderless"
              style={{ fontSize: 20, fontWeight: 600, paddingInline: 0, marginBottom: 8 }}
            />
            <div ref={scrollRef} style={{ height: 'calc(100vh - 300px)', minHeight: 360, overflow: 'auto' }}>
              <Input.TextArea
                value={displayContent}
                onChange={(e) => {
                  setContent(e.target.value)
                  syncSelection(e.target)
                }}
                onSelect={(e) => syncSelection(e.currentTarget)}
                onKeyUp={(e) => syncSelection(e.currentTarget)}
                onMouseUp={(e) => syncSelection(e.currentTarget)}
                readOnly={editorLocked}
                autoSize={{ minRows: 20 }}
                placeholder="在此书写正文，或使用右侧「AI 生成」"
                style={{ fontSize: 16, lineHeight: 1.9, resize: 'none' }}
              />
            </div>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              最后更新：{formatTime(chapter.updated_at)} · Ctrl/⌘ + S 保存
            </Typography.Text>
          </Card>
        </Col>
        <Col xs={24} xl={7}>
          <Card size="small" title="AI 助手">
            <Space direction="vertical" style={{ width: '100%' }}>
              <div>
                <Typography.Text type="secondary">AI 配置</Typography.Text>
                <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} disabled={editorLocked} />
              </div>
              <Tabs
                activeKey={aiMode}
                onChange={(k) => setAiMode(k as AIMode)}
                items={[
                  {
                    key: 'generate',
                    label: 'AI 生成',
                    disabled: editorLocked,
                    children: <GenerateForm value={genOptions} onChange={setGenOptions} disabled={task.running} />,
                  },
                  {
                    key: 'polish',
                    label: 'AI 润色',
                    disabled: editorLocked,
                    children: (
                      <PolishForm value={polishInstruction} onChange={setPolishInstruction} disabled={task.running} />
                    ),
                  },
                  {
                    key: 'rewrite',
                    label: '局部改写',
                    disabled: task.running,
                    children: (
                      <RewritePanel
                        pid={pid}
                        chapterId={chapter.id}
                        content={content}
                        selection={selection}
                        aiConfigId={aiConfigId}
                        disabled={task.running || saving || stuck || hasConfig === false}
                        ensureSaved={ensureSaved}
                        onLockChange={setRewriteLocked}
                        onApplied={handleRewriteApplied}
                      />
                    ),
                  },
                ]}
              />
              {task.running && continueInfo && (
                <Alert
                  type="info"
                  showIcon
                  message={`字数不足，自动续写第 ${continueInfo.round} 轮`}
                  description={`首轮 ${continueInfo.words} 字 / 目标 ${continueInfo.target} 字`}
                />
              )}
              {aiMode === 'rewrite' ? null : task.running ? (
                <Button danger block icon={<StopOutlined />} onClick={task.stop}>
                  停止（已输出 {countWords(task.text)} 字）
                </Button>
              ) : aiMode === 'generate' ? (
                <Button
                  type="primary"
                  block
                  icon={<ThunderboltOutlined />}
                  disabled={hasConfig === false || stuck || saving}
                  onClick={confirmGenerate}
                >
                  {content.trim() ? '重新生成本章' : '生成本章正文'}
                </Button>
              ) : (
                <Button
                  type="primary"
                  block
                  icon={<ThunderboltOutlined />}
                  disabled={hasConfig === false || stuck || saving || !content.trim()}
                  onClick={() => void runAI('polish')}
                >
                  润色本章
                </Button>
              )}
              {task.error && aiMode !== 'rewrite' && (
                <Alert type="error" showIcon message="生成失败" description={task.error} />
              )}
              {aiMode !== 'rewrite' && (
                <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                  生成会自动带入世界观、角色、本卷大纲与前文；完成后自动保存，被覆盖的旧稿自动存为历史版本。生成中不可编辑，可随时停止，停止后正文保持原样。
                </Typography.Paragraph>
              )}
            </Space>
          </Card>
        </Col>
      </Row>
      <VersionDrawer
        pid={pid}
        chapterId={chapter.id}
        open={versionsOpen}
        onClose={() => setVersionsOpen(false)}
        currentContent={content}
        currentDirty={dirty}
        restoreDisabled={editorLocked || saving || stuck}
        onRestore={handleRestore}
      />
      <Modal
        open={blocker.state === 'blocked'}
        title={task.running ? '正在生成' : dirty ? '有未保存的修改' : '局部改写结果未采纳'}
        okText="离开"
        cancelText="留下"
        okButtonProps={{ danger: true }}
        onOk={() => {
          task.stop()
          blocker.proceed?.()
        }}
        onCancel={() => blocker.reset?.()}
      >
        {task.running
          ? '离开将中止本次生成，正文保持原样。确定离开吗？'
          : dirty
            ? '离开后未保存的修改将丢失，确定离开吗？'
            : '离开将丢弃局部改写的预览结果（正文不受影响）。确定离开吗？'}
      </Modal>
    </>
  )
}
