import {
  ArrowLeftOutlined,
  ExperimentOutlined,
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
  Badge,
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
import { useBlocker, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ANALYSIS_STATUS, countPending, reviseChapter, reviseCompareFor } from '../../api/analysis'
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
import { outlineProApi } from '../../api/outlines'
import { isActiveTask, isChapterWriteKind, queueLabel, taskApi } from '../../api/tasks'
import type { Chapter, ChapterBrief, ChapterVersionBrief, Plan, ReviseChapterRequest, StyleChoice, Task } from '../../api/types'
import AIConfigSelect from '../../components/AIConfigSelect'
import AnalysisDrawer, { type ReviseCompare } from '../../components/chapter/AnalysisDrawer'
import { GenerateForm, type GenerateOptions, PolishForm } from '../../components/chapter/ChapterAIForms'
import CastSelect from '../../components/chapter/CastSelect'
import ChapterStyleSetting from '../../components/chapter/ChapterStyleSetting'
import ChapterPlanCard from '../../components/chapter/ChapterPlanCard'
import RewritePanel, { type TextSelection } from '../../components/chapter/RewritePanel'
import VersionDrawer from '../../components/chapter/VersionDrawer'
import StyleChoiceSelect from '../../components/styles/StyleChoiceSelect'
import ChapterForeshadowButton from '../../components/foreshadows/ChapterForeshadowButton'
import { useChapterAnalysis } from '../../hooks/useChapterAnalysis'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useLoader } from '../../hooks/useLoader'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { readAutoContinue } from '../../utils/preferences'
import { formatTime } from '../../utils/time'
import { useProjectId } from '../../hooks/useProjectId'

const SUCCESS_MESSAGES: Partial<Record<Task['kind'], string>> = {
  'chapter.generate': '生成完成，已保存',
  'chapter.polish': '润色完成，已保存',
  'chapter.revise': '按建议重写完成，已保存',
}

/** 接上章节任务时切换的 AI 助手标签：重写没有独立标签，放在「AI 生成」下显示进度与停止按钮 */
function aiModeForTask(t: Task): 'generate' | 'polish' {
  return t.kind === 'chapter.polish' ? 'polish' : 'generate'
}

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
  const autoAnalyze = useProjectStore((s) => s.project?.auto_analyze ?? true)
  const projectDefaultStyle = useProjectStore((s) => s.project?.default_style ?? null)
  const [searchParams, setSearchParams] = useSearchParams()

  const [chapter, setChapter] = useState<Chapter>(initial)
  const [title, setTitle] = useState(initial.title)
  const [content, setContent] = useState(initial.content)
  const [saving, setSaving] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [aiMode, setAiMode] = useState<AIMode>('generate')
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  // 本次生成 / 润色 / 重写 / 局部改写临时使用的风格（inherit = 本章风格 > 项目默认）
  const [styleChoice, setStyleChoice] = useState<StyleChoice>('inherit')
  const [genOptions, setGenOptions] = useState<GenerateOptions>(() => ({
    targetWords: 3000,
    instruction: '',
    autoContinue: readAutoContinue(),
  }))
  const [polishInstruction, setPolishInstruction] = useState('')
  // 本章细纲（按细纲建章的章节）：展示要点，目标字数预填 est_words
  const [plan, setPlan] = useState<Plan | null>(null)
  // 用户是否已手动改过目标字数：细纲加载完成前改过的不被 est_words 覆盖
  const targetWordsTouchedRef = useRef(false)
  const changeGenOptions = useCallback((next: GenerateOptions) => {
    setGenOptions((prev) => {
      if (next.targetWords !== prev.targetWords) targetWordsTouchedRef.current = true
      return next
    })
  }, [])
  const [selection, setSelection] = useState<TextSelection>({ start: 0, end: 0 })
  const [rewriteLocked, setRewriteLocked] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [analysisOpen, setAnalysisOpen] = useState(false)
  // 按建议重写成功后的评分对比基准（由任务的 analysis_id 得出，任务面板重试 / 其他标签页发起的重写同样适用）
  const [reviseCompare, setReviseCompare] = useState<ReviseCompare | null>(null)
  const analysis = useChapterAnalysis(initial.id)
  const refreshAnalysis = analysis.refresh
  // 最新已知分析 id：followTask 可能经由旧闭包调用，用 ref 读取最新值
  const latestAnalysisIdRef = useRef<number | null>(null)
  useEffect(() => {
    latestAnalysisIdRef.current = analysis.state?.latest_analysis_id ?? null
  })
  // 整章生成 / 润色走后台任务；局部改写仍是 RewritePanel 内的同步流
  const aiTask = useTaskStream()
  // 正在创建任务（请求未返回）
  const [starting, setStarting] = useState(false)
  // 章节处于 generating 时，挂载后要先查一次活动任务再决定是否显示「卡住」提示
  const [attachChecked, setAttachChecked] = useState(initial.status !== 'generating')
  const scrollRef = useRef<HTMLDivElement>(null)
  // 已经订阅过的任务 id：避免任务结束后轮询列表尚未更新时被重复接上
  const followedIdsRef = useRef(new Set<number>())

  useEffect(() => {
    if (initial.plan_id === null) return
    let alive = true
    outlineProApi
      .chapterPlan(pid, initial.id)
      .then((p) => {
        if (!alive || !p) return
        setPlan(p)
        if (!targetWordsTouchedRef.current) setGenOptions((o) => ({ ...o, targetWords: p.est_words }))
      })
      .catch(() => {
        // 细纲只是辅助信息，读取失败不影响编辑
      })
    return () => {
      alive = false
    }
  }, [pid, initial.id, initial.plan_id])

  const dirty = content !== chapter.content || title !== chapter.title
  const aiRunning = aiTask.running || starting
  const aiStatus = aiTask.task?.status
  const busy = aiRunning || saving
  // AI 任务进行中（含排队）或局部改写预览中：正文只读，避免选区失效
  const editorLocked = aiRunning || rewriteLocked
  // 「继承」解析为本章风格 > 项目默认（下拉里显示实际会用哪个风格）
  const styleContext = useMemo(
    () => ({ chapterStyleId: chapter.style_id, projectDefault: projectDefaultStyle }),
    [chapter.style_id, projectDefaultStyle],
  )
  const styleSelect = (disabled: boolean) => (
    <StyleChoiceSelect value={styleChoice} onChange={setStyleChoice} context={styleContext} disabled={disabled} />
  )
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
      // 正文变化后分析可能变为「已过期」
      void refreshAnalysis()
      return true
    } catch (err) {
      message.error(errorMessage(err))
      return false
    } finally {
      setSaving(false)
    }
  }, [pid, chapter.id, title, content, applyServerChapter, onChanged, refreshProject, refreshAnalysis, message])

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
      else message.success(SUCCESS_MESSAGES[t.kind] ?? '生成完成，已保存')
      const cmp = reviseCompareFor(outcome.task ?? t, latestAnalysisIdRef.current)
      if (cmp) setReviseCompare(cmp)
      onChanged()
      void refreshProject()
      // 成功后后端可能已自动排队分析本章：刷新状态以接上分析任务
      void refreshAnalysis()
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
    setAiMode(aiModeForTask(t))
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
      (x) => x.chapter_id === chapter.id && isChapterWriteKind(x.kind) && !followedIdsRef.current.has(x.id),
    )
    if (t) attachExternalRef.current(t)
  }, [activeTasks, attachChecked, aiRunning, chapter.id])

  // 批量生成正在写本章（批量任务没有 chapter_id，按断点里的当前章识别）：只读等待、不提供复位；写完后从服务端刷新正文
  const batchWriting = activeTasks.some(
    (x) => x.kind === 'chapter.batch' && isActiveTask(x.status) && x.checkpoint?.current?.chapter_id === chapter.id,
  )
  const wasBatchWritingRef = useRef(false)
  useEffect(() => {
    if (batchWriting) {
      wasBatchWritingRef.current = true
      return
    }
    if (!wasBatchWritingRef.current) return
    wasBatchWritingRef.current = false
    chapterApi
      .get(pid, chapter.id)
      .then((fresh) => replaceFromServer(fresh))
      .catch(() => {
        // 忽略：用户可手动刷新
      })
  }, [batchWriting, pid, chapter.id, replaceFromServer])

  // 章节处于 generating：查找它的活动任务（排队或运行中）并自动接上
  useEffect(() => {
    if (initial.status !== 'generating') return
    let alive = true
    taskApi
      .list({ chapter_id: initial.id, active: true, limit: 5 })
      .then((list) => {
        if (!alive) return
        const t = list.find((x) => isChapterWriteKind(x.kind))
        if (t) {
          setAiMode(aiModeForTask(t))
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
              style: styleChoice,
            })
          : await polishChapter(chapter.id, {
              ai_config_id: aiConfigId,
              instruction: polishInstruction.trim(),
              style: styleChoice,
            })
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

  /** 按建议重写：与整章生成同样走后台任务（创建即抢占、正文只读、可停止）；任务创建成功即返回 true */
  const runRevise = async (body: ReviseChapterRequest): Promise<boolean> => {
    if (!(await ensureSaved())) return false
    setStarting(true)
    let created: Task
    try {
      created = await reviseChapter(chapter.id, { ...body, ai_config_id: aiConfigId, style: styleChoice })
    } catch (err) {
      message.error(errorMessage(err))
      setStarting(false)
      await reloadChapter()
      return false
    }
    setStarting(false)
    trackTask(created)
    setReviseCompare(null)
    if (aiMode === 'rewrite') setAiMode('generate')
    setChapter((prev) => ({ ...prev, status: 'generating' }))
    void followTask(created)
    return true
  }

  /** 手动分析：分析的是已保存的正文，先自动保存未保存的修改 */
  const runAnalyze = async () => {
    if (!(await ensureSaved())) return
    try {
      await analysis.start(aiConfigId)
    } catch (err) {
      message.error(errorMessage(err))
    }
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
      void refreshAnalysis()
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
    void refreshAnalysis()
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

  const stuck = chapter.status === 'generating' && !aiRunning && attachChecked && !batchWriting
  // 批量正在写本章：正文 / 标题只读、不能保存（后端对生成中章节的正文 PATCH 返回 409，写完后会用服务端正文覆盖本地）
  const batchLocked = batchWriting && chapter.status === 'generating'
  const status = aiRunning ? CHAPTER_STATUS.generating : CHAPTER_STATUS[chapter.status]
  const revising = aiRunning && aiTask.task?.kind === 'chapter.revise'
  const analysisStatus = ANALYSIS_STATUS[analysis.status]
  const pendingChanges = countPending(analysis.latest?.changes)
  // 任务面板「跳转」带 ?panel=analysis 时直接打开分析抽屉
  const panelRequested = searchParams.get('panel') === 'analysis'
  const closeAnalysis = () => {
    setAnalysisOpen(false)
    if (panelRequested) {
      const next = new URLSearchParams(searchParams)
      next.delete('panel')
      setSearchParams(next, { replace: true })
    }
  }
  const reviseBlockedReason =
    hasConfig === false
      ? '尚未配置 AI 模型'
      : !content.trim()
        ? '章节正文为空，无法重写'
        : aiRunning || stuck || chapter.status === 'generating' || !attachChecked
          ? '本章正在生成中，请稍候'
          : saving || rewriteLocked
            ? '请先完成保存或局部改写'
            : ''

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
                <Tag color={analysisStatus.color} style={{ cursor: 'pointer' }} onClick={() => setAnalysisOpen(true)}>
                  {analysisStatus.label}
                </Tag>
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
                <Badge count={pendingChanges} size="small" title={`${pendingChanges} 项设定变化待确认`}>
                  <Button size="small" icon={<ExperimentOutlined />} onClick={() => setAnalysisOpen(true)}>
                    分析
                  </Button>
                </Badge>
                <ChapterForeshadowButton projectId={pid} chapterId={chapter.id} />
                <Button size="small" icon={<HistoryOutlined />} onClick={() => setVersionsOpen(true)}>
                  版本历史
                </Button>
                <Button size="small" disabled={busy || stuck || batchLocked || rewriteLocked} onClick={() => void handleToggleDone()}>
                  {chapter.status === 'done' ? '标记为草稿' : '标记为完成'}
                </Button>
                <Button
                  type="primary"
                  size="small"
                  icon={<SaveOutlined />}
                  loading={saving}
                  disabled={editorLocked || stuck || batchLocked || !dirty}
                  onClick={() => void handleSave()}
                >
                  保存
                </Button>
              </Space>
            }
          >
            {batchWriting && chapter.status === 'generating' && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="该章节正在批量生成中"
                description="写完后自动刷新正文；进度见章节列表顶部或任务面板。"
              />
            )}
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
              disabled={editorLocked || batchLocked}
              variant="borderless"
              style={{ fontSize: 20, fontWeight: 600, paddingInline: 0, marginBottom: 8 }}
            />
            <CastSelect pid={pid} chapterId={chapter.id} disabled={aiRunning} />
            <ChapterStyleSetting
              pid={pid}
              chapterId={chapter.id}
              value={chapter.style_id}
              projectDefault={projectDefaultStyle}
              disabled={aiRunning}
              onChanged={(styleId) => setChapter((prev) => ({ ...prev, style_id: styleId }))}
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
                readOnly={editorLocked || batchLocked}
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
              <div>
                <Typography.Text type="secondary">本次写作风格</Typography.Text>
                {styleSelect(editorLocked)}
              </div>
              <Tabs
                activeKey={aiMode}
                onChange={(k) => setAiMode(k as AIMode)}
                items={[
                  {
                    key: 'generate',
                    label: 'AI 生成',
                    disabled: editorLocked,
                    children: (
                      <Space direction="vertical" style={{ width: '100%' }}>
                        {plan && <ChapterPlanCard plan={plan} />}
                        <GenerateForm value={genOptions} onChange={changeGenOptions} disabled={aiRunning} />
                      </Space>
                    ),
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
                        styleChoice={styleChoice}
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
              {revising && (
                <Alert type="info" showIcon message="正在按分析建议重写本章" description="完成后覆盖正文，旧稿自动存为历史版本。" />
              )}
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
      <AnalysisDrawer
        open={analysisOpen || panelRequested}
        onClose={closeAnalysis}
        chapterId={chapter.id}
        analysis={analysis}
        autoAnalyze={autoAnalyze}
        analyzeDisabled={hasConfig === false || aiRunning || stuck || saving || !content.trim()}
        onAnalyze={runAnalyze}
        reviseBlockedReason={reviseBlockedReason}
        reviseRunning={revising}
        onRevise={runRevise}
        compare={reviseCompare}
        reviseStyle={styleSelect(aiRunning)}
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
