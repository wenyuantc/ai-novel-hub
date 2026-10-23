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
  Progress,
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
import {
  chapterApi,
  CHAPTER_STATUS,
  countWords,
  generateChapter,
  parseChapterDone,
  polishChapter,
} from '../../api/chapters'
import { errorMessage } from '../../api/client'
import { queueLabel, taskApi } from '../../api/tasks'
import type { Chapter, ChapterBrief, ChapterVersionBrief, Task } from '../../api/types'
import AIConfigSelect from '../../components/AIConfigSelect'
import { GenerateForm, type GenerateOptions, PolishForm } from '../../components/chapter/ChapterAIForms'
import CastSelect from '../../components/chapter/CastSelect'
import RewritePanel, { type TextSelection } from '../../components/chapter/RewritePanel'
import VersionDrawer from '../../components/chapter/VersionDrawer'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useLoader } from '../../hooks/useLoader'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
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
  const trackTask = useTasksStore((s) => s.track)
  const activeTasks = useTasksStore((s) => s.active)
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
  const [selection, setSelection] = useState<TextSelection>({ start: 0, end: 0 })
  const [rewriteLocked, setRewriteLocked] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  // 整章生成 / 润色走后台任务；局部改写仍是 RewritePanel 内的同步流
  const aiTask = useTaskStream()
  // 正在创建任务（请求未返回）
  const [starting, setStarting] = useState(false)
  // 章节处于 generating 时，挂载后要先查一次活动任务再决定是否显示「卡住」提示
  const [attachChecked, setAttachChecked] = useState(initial.status !== 'generating')
  const scrollRef = useRef<HTMLDivElement>(null)
  // 已经订阅过的任务 id：避免任务结束后轮询列表尚未更新时被重复接上
  const followedIdsRef = useRef(new Set<number>())

  const dirty = content !== chapter.content || title !== chapter.title
  const aiRunning = aiTask.running || starting
  const aiStatus = aiTask.task?.status
  const busy = aiRunning || saving
  // AI 任务进行中（含排队）或局部改写预览中：正文只读，避免选区失效
  const editorLocked = aiRunning || rewriteLocked
  // 排队中 / 尚无输出时仍显示原正文；开始输出后显示实时文本
  const aiStreaming = aiTask.running && (aiTask.text !== '' || aiStatus === 'running')
  const displayContent = aiStreaming ? aiTask.text : content
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

  // 未保存 / 局部改写预览中（流式或待采纳）离开提示；AI 任务在后台运行，可以直接离开
  const guard = dirty || rewriteLocked
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
    if (aiStreaming && el) el.scrollTop = el.scrollHeight
  }, [aiTask.text, aiStreaming])

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

  /** 订阅章节任务直到结束：成功覆盖正文，失败 / 取消 / 中断只同步服务端状态并保留本地正文。 */
  const followTask = async (t: Task) => {
    followedIdsRef.current.add(t.id)
    const outcome = await aiTask.follow(t)
    if (outcome.status === 'detached') return
    if (outcome.status === 'succeeded') {
      const done = parseChapterDone(outcome.result)
      if (done) {
        replaceFromServer(done.chapter)
      } else {
        try {
          replaceFromServer(await chapterApi.get(pid, chapter.id))
        } catch {
          // 忽略
        }
      }
      if (done?.warning) message.warning(`生成完成，已保存；${done.warning}`)
      else message.success(t.kind === 'chapter.polish' ? '润色完成，已保存' : '生成完成，已保存')
      onChanged()
      void refreshProject()
      return
    }
    if (outcome.status === 'cancelled') message.info('已停止，正文保持原样')
    await reloadChapter()
  }
  // 挂载时的自动接续用 ref 调用最新的 followTask
  const followRef = useRef(followTask)
  useEffect(() => {
    followRef.current = followTask
  })

  /** 接上由其他入口（任务面板重试、另一个标签页）为本章创建的任务 */
  const attachExternal = (t: Task) => {
    if (dirty) message.warning('本章已有后台任务开始执行，当前未保存的修改将在任务完成后被覆盖')
    setAiMode(t.kind === 'chapter.polish' ? 'polish' : 'generate')
    setChapter((prev) => ({ ...prev, status: 'generating' }))
    void followTask(t)
  }
  const attachExternalRef = useRef(attachExternal)
  useEffect(() => {
    attachExternalRef.current = attachExternal
  })
  useEffect(() => {
    if (!attachChecked || aiRunning) return
    const t = activeTasks.find(
      (x) =>
        x.chapter_id === chapter.id &&
        (x.kind === 'chapter.generate' || x.kind === 'chapter.polish') &&
        !followedIdsRef.current.has(x.id),
    )
    if (t) attachExternalRef.current(t)
  }, [activeTasks, attachChecked, aiRunning, chapter.id])

  // 章节处于 generating：查找它的活动任务（排队或运行中）并自动接上
  useEffect(() => {
    if (initial.status !== 'generating') return
    let alive = true
    taskApi
      .list({ chapter_id: initial.id, active: true, limit: 5 })
      .then((list) => {
        if (!alive) return
        const t = list.find((x) => x.kind === 'chapter.generate' || x.kind === 'chapter.polish')
        if (t) {
          setAiMode(t.kind === 'chapter.polish' ? 'polish' : 'generate')
          void followRef.current(t)
        }
      })
      .catch(() => {
        // 查询失败时按「卡住」处理，用户仍可复位
      })
      .finally(() => {
        if (alive) setAttachChecked(true)
      })
    return () => {
      alive = false
    }
  }, [initial.id, initial.status])

  const runAI = async (mode: 'generate' | 'polish') => {
    if (!(await ensureSaved())) return
    setStarting(true)
    let created: Task
    try {
      created =
        mode === 'generate'
          ? await generateChapter(chapter.id, {
              ai_config_id: aiConfigId,
              instruction: genOptions.instruction.trim(),
              target_words: genOptions.targetWords,
              // 后端默认 false，必须显式传
              auto_continue: genOptions.autoContinue,
            })
          : await polishChapter(chapter.id, { ai_config_id: aiConfigId, instruction: polishInstruction.trim() })
    } catch (err) {
      message.error(errorMessage(err))
      setStarting(false)
      await reloadChapter()
      return
    }
    setStarting(false)
    trackTask(created)
    // 任务创建即抢占章节
    setChapter((prev) => ({ ...prev, status: 'generating' }))
    await followTask(created)
  }

  const handleCancel = async () => {
    try {
      await aiTask.cancel()
    } catch (err) {
      message.error(errorMessage(err))
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

  const stuck = chapter.status === 'generating' && !aiRunning && attachChecked
  const status = aiRunning ? CHAPTER_STATUS.generating : CHAPTER_STATUS[chapter.status]

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
                {dirty && !aiRunning && <Tag color="orange">未保存</Tag>}
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
                description="未找到进行中的 AI 任务；如果确认生成已中断，可复位为草稿后继续编辑。"
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
            <CastSelect pid={pid} chapterId={chapter.id} disabled={aiRunning} />
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
                    children: <GenerateForm value={genOptions} onChange={setGenOptions} disabled={aiRunning} />,
                  },
                  {
                    key: 'polish',
                    label: 'AI 润色',
                    disabled: editorLocked,
                    children: (
                      <PolishForm value={polishInstruction} onChange={setPolishInstruction} disabled={aiRunning} />
                    ),
                  },
                  {
                    key: 'rewrite',
                    label: '局部改写',
                    disabled: aiRunning,
                    children: (
                      <RewritePanel
                        pid={pid}
                        chapterId={chapter.id}
                        content={content}
                        selection={selection}
                        aiConfigId={aiConfigId}
                        // 章节 generating（含挂载后尚未查完活动任务）时后端会 409，一律禁用
                        disabled={aiRunning || saving || chapter.status === 'generating' || hasConfig === false}
                        ensureSaved={ensureSaved}
                        onLockChange={setRewriteLocked}
                        onApplied={handleRewriteApplied}
                      />
                    ),
                  },
                ]}
              />
              {aiTask.running && aiStatus === 'queued' && aiTask.task && (
                <Alert type="info" showIcon message={queueLabel(aiTask.task)} description="任务在后台排队，可离开本页，稍后返回会自动接上。" />
              )}
              {aiTask.running && aiStatus === 'running' && aiTask.task && (
                <div>
                  <Progress percent={aiTask.task.progress} size="small" status="active" style={{ marginBottom: 0 }} />
                  {aiTask.task.stage && (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {aiTask.task.stage}
                    </Typography.Text>
                  )}
                </div>
              )}
              {aiTask.reconnecting && <Alert type="warning" showIcon message="连接中断，正在重连…（任务仍在后台运行）" />}
              {aiTask.running && aiTask.continueInfo && (
                <Alert
                  type="info"
                  showIcon
                  message={`字数不足，自动续写第 ${aiTask.continueInfo.round} 轮`}
                  description={`首轮 ${aiTask.continueInfo.words} 字 / 目标 ${aiTask.continueInfo.target} 字`}
                />
              )}
              {aiMode === 'rewrite' ? null : aiRunning ? (
                <Button danger block icon={<StopOutlined />} disabled={!aiTask.running} onClick={() => void handleCancel()}>
                  {aiStatus === 'queued' ? '取消排队' : `停止（已输出 ${countWords(aiTask.text)} 字）`}
                </Button>
              ) : aiMode === 'generate' ? (
                <Button
                  type="primary"
                  block
                  icon={<ThunderboltOutlined />}
                  disabled={hasConfig === false || stuck || saving || !attachChecked}
                  onClick={confirmGenerate}
                >
                  {content.trim() ? '重新生成本章' : '生成本章正文'}
                </Button>
              ) : (
                <Button
                  type="primary"
                  block
                  icon={<ThunderboltOutlined />}
                  disabled={hasConfig === false || stuck || saving || !attachChecked || !content.trim()}
                  onClick={() => void runAI('polish')}
                >
                  润色本章
                </Button>
              )}
              {aiTask.error && !aiRunning && aiMode !== 'rewrite' && (
                <Alert
                  type="error"
                  showIcon
                  message={aiStatus === 'interrupted' ? '任务已中断' : '生成失败'}
                  description={aiTask.error}
                />
              )}
              {aiMode !== 'rewrite' && (
                <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
                  生成会自动带入世界观、角色、本卷大纲与前文；完成后自动保存，被覆盖的旧稿自动存为历史版本。任务在后台运行，可离开本页，返回后自动接上；生成中不可编辑，可随时停止，停止后正文保持原样。
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
        title={dirty ? '有未保存的修改' : '局部改写结果未采纳'}
        okText="离开"
        cancelText="留下"
        okButtonProps={{ danger: true }}
        onOk={() => blocker.proceed?.()}
        onCancel={() => blocker.reset?.()}
      >
        {dirty
          ? '离开后未保存的修改将丢失，确定离开吗？'
          : '离开将丢弃局部改写的预览结果（正文不受影响）。确定离开吗？'}
      </Modal>
    </>
  )
}
