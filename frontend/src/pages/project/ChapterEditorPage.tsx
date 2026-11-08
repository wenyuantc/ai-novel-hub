import { useSettingsConfirmation } from '../../components/systemSettings/useSettingsConfirmation'
import { readMentionAnchor } from '../../utils/mentions'
import {
  ArrowLeftOutlined,
  BookOutlined,
  FileTextOutlined,
  RobotOutlined,
  ExperimentOutlined,
  HistoryOutlined,
  LeftOutlined,
  RightOutlined,
  SaveOutlined,
  SettingOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  Alert,
  App,
  Badge,
  Button,
  Card,
  Drawer,
  FloatButton,
  Input,
  Modal,
  Progress,
  Segmented,
  Result,
  Space,
  Spin,
  Tabs,
  Tag,
  theme,
  Typography,
} from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useBlocker, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ANALYSIS_STATUS, countPending, reviseChapter, reviseCompareFor, reviseRunningPresentation } from '../../api/analysis'
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
import { sceneApi } from '../../api/scenes'
import ScenePlanningPanel from '../../components/scenes/ScenePlanningPanel'
import SceneSettingPreview from '../../components/scenes/SceneSettingPreview'
import SceneWritingPanel from '../../components/scenes/SceneWritingPanel'
import { readMarkerLocation, type MarkerLocation } from '../../components/scenes/markerLocation'
import { editorFromSectionDocument } from '../../components/scenes/richSectionProjection'
import { validSectionDocument } from '../../components/scenes/richSectionValidity'
import { useSceneEditor } from '../../components/scenes/useSceneEditor'
import { sceneTargetAllocation } from '../../components/scenes/scenePresentation'
import StreamOutput from '../../components/StreamOutput'
import { useAuthStore } from '../../stores/auth'
import { authorizationKey } from '../../utils/permissions'
import { outlineProApi } from '../../api/outlines'
import { isActiveTask, isChapterWriteKind, queueLabel, taskApi } from '../../api/tasks'
import type { Chapter, ChapterBrief, ChapterVersionBrief, Plan, ReviseChapterRequest, StyleChoice, Task } from '../../api/types'
import AIConfigSelect from '../../components/AIConfigSelect'
import AnalysisDrawer, { type ReviseCompare } from '../../components/chapter/AnalysisDrawer'
import { GenerateForm, type GenerateOptions, PolishForm } from '../../components/chapter/ChapterAIForms'
import CastSelect from '../../components/chapter/CastSelect'
import ChapterStyleSetting from '../../components/chapter/ChapterStyleSetting'
import ChapterPlanCard from '../../components/chapter/ChapterPlanCard'
import ContextPreviewPanel from '../../components/chapter/ContextPreviewPanel'
import RewritePanel, { type TextSelection } from '../../components/chapter/RewritePanel'
import VersionDrawer from '../../components/chapter/VersionDrawer'
import SkillSelect from '../../components/skills/SkillSelect'
import StyleChoiceSelect from '../../components/styles/StyleChoiceSelect'
import ChapterForeshadowButton from '../../components/foreshadows/ChapterForeshadowButton'
import { useChapterAnalysis } from '../../hooks/useChapterAnalysis'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useElementWidth } from '../../hooks/useElementWidth'
import { useIsMobile } from '../../hooks/useIsMobile'
import { usePublishAgentContext } from '../../hooks/useAgentPageContext'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useLoader } from '../../hooks/useLoader'
import { useTaskStream } from '../../hooks/useTaskStream'
import { useProjectStore } from '../../stores/project'
import { useTasksStore } from '../../stores/tasks'
import { batchCheckpoint } from '../../utils/batchState'
import { readAutoContinue } from '../../utils/preferences'
import { splitPane, splitRow } from '../../utils/layout'
import { formatTime } from '../../utils/time'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectAccess } from '../../hooks/useProjectAccess'

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
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  if (!Number.isFinite(chapterId)) return <Result status="404" title="章节不存在" />
  return <ChapterLoader key={`${pid}:${chapterId}:${identity}`} pid={pid} cid={chapterId} />
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

/** 正文卡片宽度达到它时工具栏放进卡片标题栏（≈ 1600px 视口、停靠面板关闭时正文栏的宽度，含竖向滚动条时约 935）。 */
const TOOLBAR_IN_HEADER_MIN_WIDTH = 920

function ChapterEditor({ pid, initial, siblings, onChanged }: EditorProps) {
  const { canUseAI, managingOther, canEdit, canEditNow, canReadNow } = useProjectAccess(pid)
  const isMobile = useIsMobile()
  // 工具栏按钮较多：只在正文卡片足够宽（约等于原 xxl 视口、停靠面板关闭时的宽度）时放进卡片标题栏，
  // 其余宽度放在正文上方，避免标题被挤压；按卡片实际宽度判断，右侧停靠面板打开时同样生效
  const [editorPaneRef, editorPaneWidth] = useElementWidth<HTMLDivElement>()
  const toolbarInHeader = editorPaneWidth >= TOOLBAR_IN_HEADER_MIN_WIDTH
  const { token } = theme.useToken()
  const [inspectorOpen, setInspectorOpen] = useState(true)
  const [inspectorTab, setInspectorTab] = useState<'ai' | 'scene' | 'chapter'>('ai')
  // 移动端辅助面板收进底部抽屉（forceRender：关闭抽屉不丢局部改写等面板内状态）
  const [aiPanelOpen, setAiPanelOpen] = useState(false)
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const location = useLocation()
  const refreshProject = useProjectStore((s) => s.refresh)
  const trackTask = useTasksStore((s) => s.track)
  const activeTasks = useTasksStore((s) => s.active)
  const hasConfig = useHasAIConfig()
  const autoAnalyze = useProjectStore((s) => s.project?.auto_analyze ?? true)
  const projectDefaultStyle = useProjectStore((s) => s.project?.default_style ?? null)
  const [searchParams, setSearchParams] = useSearchParams()
  const markerJump = useMemo(() => readMarkerLocation(location.state, initial.id), [location.state, initial.id])
  const mentionAnchor = useMemo(() => readMentionAnchor(searchParams), [searchParams])

  const [chapter, setChapter] = useState<Chapter>(initial)
  const [title, setTitle] = useState(initial.title)
  const requestedSceneId = Number(searchParams.get('scene_id')) || undefined
  const sceneEditor = useSceneEditor(pid, initial.id, requestedSceneId)
  const scene = sceneEditor.scene
  const selectionEditable = useMemo(() => {
    if (!sceneEditor.documentDraft) return false
    try { return validSectionDocument(sceneEditor.documentDraft) && !!editorFromSectionDocument(sceneEditor.documentDraft) } catch { return false }
  }, [sceneEditor.documentDraft])
  const currentSceneId = scene?.id
  const confirm = useSettingsConfirmation(modal, !canEdit, `${pid}:${initial.id}:${currentSceneId ?? ''}`)
  const content = sceneEditor.draft.content
  const [aiScope, setAiScope] = useState<'scene' | 'chapter'>('scene')
  const [savingChapter, setSaving] = useState(false)
  const saving = savingChapter || sceneEditor.saving
  const [resetting, setResetting] = useState(false)
  const [aiMode, setAiMode] = useState<AIMode>('generate')
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  // 本次生成 / 润色 / 重写 / 局部改写临时使用的风格（inherit = 本章风格 > 项目默认）
  const [styleChoice, setStyleChoice] = useState<StyleChoice>('inherit')
  // 本次生成 / 润色 / 重写 / 局部改写选用的 Skill（补充工作流，任务创建时快照）
  const [skillId, setSkillId] = useState<number | null>(null)
  const [genOptions, setGenOptions] = useState<GenerateOptions>(() => ({
    targetWords: 3000,
    instruction: '',
    autoContinue: readAutoContinue(),
  }))
  const [polishInstruction, setPolishInstruction] = useState('')
  const sceneAllocations = sceneTargetAllocation(genOptions.targetWords, sceneEditor.page?.items.map((item) => item.id) ?? [])
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
  usePublishAgentContext(pid, { chapter_id: initial.id, outline_id: initial.outline_id ?? undefined, selection: content.slice(selection.start, selection.end) })
  const [externalChapter, setExternalChapter] = useState<Chapter | null>(null)
  const [rewriteLocked, setRewriteLocked] = useState(false)
  const [sectionLocked, setSectionLocked] = useState(false)
  const [structureDirty, setStructureDirty] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [analysisOpen, setAnalysisOpen] = useState(false)
  // 按建议重写成功后的评分对比基准（由任务的 analysis_id 得出，任务面板重试 / 其他标签页发起的重写同样适用）
  const [reviseCompare, setReviseCompare] = useState<ReviseCompare | null>(null)
  const analysis = useChapterAnalysis(initial.id, canUseAI)
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
  // 已经订阅过的任务 id：避免任务结束后轮询列表尚未更新时被重复接上
  const followedIdsRef = useRef(new Set<number>())
  const detachWriteTask = aiTask.detach
  const followedWriteTask = aiTask.task
  const writeTaskRunning = aiTask.running
  useEffect(() => {
    const task = followedWriteTask
    if (task?.scene_id && task.scene_id !== currentSceneId && writeTaskRunning) {
      followedIdsRef.current.delete(task.id)
      detachWriteTask()
    }
  }, [currentSceneId, followedWriteTask, writeTaskRunning, detachWriteTask])

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

  const dirty = sceneEditor.dirty || title !== chapter.title
  const dirtyRef = useRef(dirty)
  useEffect(() => { dirtyRef.current = dirty }, [dirty])
  const aiRunning = aiTask.running || starting
  const aiStatus = aiTask.task?.status
  const busy = aiRunning || saving
  // AI 任务进行中（含排队）或局部改写预览中：正文只读，避免选区失效
  const managedGenerating = !canUseAI && chapter.status === 'generating'
  const editorLocked = aiRunning || rewriteLocked || sectionLocked || managedGenerating
  // 「继承」解析为本章风格 > 项目默认（下拉里显示实际会用哪个风格）
  const styleContext = useMemo(
    () => ({ chapterStyleId: chapter.style_id, projectDefault: projectDefaultStyle }),
    [chapter.style_id, projectDefaultStyle],
  )
  const styleSelect = (disabled: boolean) => (
    <StyleChoiceSelect value={styleChoice} onChange={setStyleChoice} context={styleContext} disabled={disabled} />
  )
  const skillSelect = (disabled: boolean) => <SkillSelect value={skillId} onChange={setSkillId} disabled={disabled} />
  const words = useMemo(() => countWords(content), [content])

  const { prev, next } = useMemo(() => {
    const sorted = [...siblings].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id)
    const idx = sorted.findIndex((c) => c.id === chapter.id)
    return { prev: idx > 0 ? sorted[idx - 1] : undefined, next: idx >= 0 ? sorted[idx + 1] : undefined }
  }, [siblings, chapter.id])

  const applyServerChapter = useCallback((c: Chapter) => {
    setChapter(c)
    setTitle(c.title)
  }, [])

  /** 服务端改写了正文（生成 / 采纳改写 / 回滚）：同步并把光标记录移到末尾（与浏览器行为一致）。 */
  const replaceFromServer = useCallback(
    (c: Chapter) => {
      applyServerChapter(c)
      void sceneEditor.controller.load()
      setSelection({ start: 0, end: 0 })
    },
    [applyServerChapter, sceneEditor.controller, setSelection],
  )

  useProjectRefresh(pid, ['chapters', 'outlines', 'characters', 'memories'], () => {
    onChanged()
    void refreshAnalysis()
    void chapterApi.get(pid, chapter.id).then((next) => {
      if (!canReadNow()) return
      if (next.content_revision === chapter.content_revision && next.title === chapter.title) return
      if (dirty || editorLocked || saving) setExternalChapter(next)
      else replaceFromServer(next)
    }).catch((err: unknown) => { if (canReadNow()) message.error(errorMessage(err)) })
  })
  useProjectRefresh(pid, ['planning', 'setting-ai-visibility'], () => { void sceneEditor.controller.load() })

  const save = useCallback(async (): Promise<boolean> => {
    if (!canEditNow() || !sceneEditor.controller.getSnapshot().scene) return false
    setSaving(true)
    try {
      if (!(await sceneEditor.controller.save()) || !canEditNow()) return false
      const updated = title !== chapter.title
        ? await chapterApi.update(pid, chapter.id, { title })
        : await chapterApi.get(pid, chapter.id)
      if (!canEditNow()) return false
      applyServerChapter(updated)
      onChanged()
      void refreshProject()
      void refreshAnalysis()
      return true
    } catch (err) {
      if (canEditNow()) message.error(errorMessage(err))
      return false
    } finally { setSaving(false) }
  }, [pid, chapter.id, chapter.title, title, sceneEditor.controller, applyServerChapter, onChanged, refreshProject, refreshAnalysis, message, canEditNow])

  const handleSave = useCallback(async () => {
    if (!canEditNow()) return
    if (editorLocked || saving) return
    if (sceneEditor.controller.getSnapshot().composing) { message.info('输入法组合尚未结束，请确认文字后再保存。'); return }
    if (!dirty) {
      message.info('没有需要保存的修改')
      return
    }
    if (!canEditNow()) return
    if (await save()) message.success('已保存')
    if (!canEditNow()) return
  }, [editorLocked, saving, dirty, save, message, canEditNow, sceneEditor.controller])

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
  const guard = dirty || rewriteLocked || sectionLocked || structureDirty
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
    ({ currentLocation, nextLocation }) => guard && (currentLocation.pathname !== nextLocation.pathname || new URLSearchParams(currentLocation.search).get('scene_id') !== new URLSearchParams(nextLocation.search).get('scene_id')),
  )

  const reloadChapter = async () => {
    try {
      const fresh = await chapterApi.get(pid, chapter.id)
      if (!canReadNow()) return
      // 只同步服务端状态，保留本地正文（生成失败/中止时正文未变）
      setChapter(fresh)
    } catch {
      // 忽略
    }
  }

  /** AI 操作 / 回滚前：有未保存修改先自动保存。 */
  const ensureSaved = async (): Promise<boolean> => {
    if (!sceneEditor.controller.getSnapshot().scene) return false
    if (sceneEditor.controller.getSnapshot().composing) { message.warning('请先确认输入法组合文字。'); return false }
    if (sceneEditor.conflict || sceneEditor.externalChange) { message.warning('请先核对最新场景并明确接受版本号，当前草稿未覆盖'); return false }
    if (!dirty) return true
    if (!(await save())) return false
    message.info('已自动保存当前修改')
    return true
  }

  /** 订阅章节任务直到结束：成功覆盖正文，失败 / 取消 / 中断只同步服务端状态并保留本地正文。 */
  const followTask = async (t: Task) => {
    followedIdsRef.current.add(t.id)
    const outcome = await aiTask.follow(t)
    if (outcome.status === 'detached' || !canReadNow()) return
    if (t.scene_id && t.scene_id !== sceneEditor.controller.getSnapshot().scene?.id) {
      await reloadChapter()
      if (!canReadNow()) return
      void sceneEditor.controller.load()
      if (outcome.status === 'succeeded') {
        // 另一景成功也使本章分析过期；只刷新章级派生资料，不把整章稿写入当前景草稿。
        setReviseCompare(null)
        onChanged()
        void refreshProject()
        void refreshAnalysis()
      }
      return
    }
    if (outcome.status === 'succeeded') {
      const done = parseChapterDone(outcome.result)
      if (done && done.chapter.id === chapter.id) {
        if (dirtyRef.current) { setChapter(done.chapter); setExternalChapter(done.chapter); void sceneEditor.controller.load() }
        else replaceFromServer(done.chapter)
      } else {
        try {
          const fresh = await chapterApi.get(pid, chapter.id)
          if (!canReadNow()) return
          if (dirtyRef.current) { setChapter(fresh); setExternalChapter(fresh); void sceneEditor.controller.load() }
          else replaceFromServer(fresh)
        } catch {
          // 忽略
        }
      }
      if (done?.warning) message.warning(`生成完成，已保存；${done.warning}`)
      else message.success(SUCCESS_MESSAGES[t.kind] ?? '生成完成，已保存')
      const finishedTask = outcome.task ?? t
      if (finishedTask.kind === 'chapter.revise') {
        const cmp = reviseCompareFor({ ...finishedTask, scene_id: t.scene_id ?? finishedTask.scene_id }, latestAnalysisIdRef.current)
        // Scene 重写只令章分析过期；也清掉此前整章任务留下的比较，不等待不存在的自动分析。
        setReviseCompare(cmp)
      }
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
    if (dirty) message.warning('本章已有后台任务开始执行；本地场景草稿会保留，完成后请核对最新版本')
    setAiMode(aiModeForTask(t))
    setChapter((prev) => ({ ...prev, status: 'generating' }))
    void followTask(t)
  }
  const attachExternalRef = useRef(attachExternal)
  useEffect(() => {
    attachExternalRef.current = attachExternal
  })
  useEffect(() => {
    if (!canUseAI || !attachChecked || aiRunning) return
    const t = activeTasks.find(
      (x) => x.chapter_id === chapter.id && isChapterWriteKind(x.kind) && (!x.scene_id || x.scene_id === scene?.id) && !followedIdsRef.current.has(x.id),
    )
    if (t) attachExternalRef.current(t)
  }, [activeTasks, attachChecked, aiRunning, chapter.id, canUseAI, scene?.id])

  // 批量生成正在写本章（批量任务没有 chapter_id，按断点里的当前章识别）：只读等待、不提供复位；写完后从服务端刷新正文
  const batchWriting = canUseAI && activeTasks.some(
    (x) => x.kind === 'chapter.batch' && isActiveTask(x.status) && batchCheckpoint(x.checkpoint)?.current?.chapter_id === chapter.id,
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
    if (!canUseAI || initial.status !== 'generating' || !currentSceneId) return
    let alive = true
    taskApi
      .list({ chapter_id: initial.id, active: true, limit: 5 })
      .then((list) => {
        if (!alive) return
        const t = list.find((x) => isChapterWriteKind(x.kind) && (!x.scene_id || x.scene_id === currentSceneId))
        if (t) {
          setAiMode(aiModeForTask(t))
          void followRef.current(t)
        } else { const other = list.find((x) => isChapterWriteKind(x.kind)); if (other) trackTask(other) }
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
  }, [initial.id, initial.status, canUseAI, currentSceneId, trackTask])

  const runAI = async (mode: 'generate' | 'polish') => {
    if (!canUseAI || !canEditNow() || !(await ensureSaved())) return
    const current = sceneEditor.controller.getSnapshot().scene
    if (!current) return
    const versions = { expected_content_revision: current.content_revision, expected_planning_revision: current.planning_revision }
    setStarting(true)
    let created: Task
    try {
      if (mode === 'generate') {
        const body = { ai_config_id: aiConfigId, instruction: genOptions.instruction.trim(), target_words: genOptions.targetWords, auto_continue: genOptions.autoContinue, style: styleChoice, skill_id: skillId, ...versions }
        created = aiScope === 'scene' ? await sceneApi.generate(pid, chapter.id, current.id, body) : await generateChapter(chapter.id, body)
      } else {
        const body = { ai_config_id: aiConfigId, instruction: polishInstruction.trim(), style: styleChoice, skill_id: skillId, ...versions }
        created = aiScope === 'scene' ? await sceneApi.polish(pid, chapter.id, current.id, body) : await polishChapter(chapter.id, body)
      }
      if (!canEditNow()) return
      if (sceneEditor.controller.getSnapshot().scene?.id !== current.id) { trackTask(created); return }
    } catch (err) {
      if (canEditNow() && sceneEditor.controller.getSnapshot().scene?.id === current.id) message.error(errorMessage(err))
      await reloadChapter()
      return
    } finally { if (canEditNow()) setStarting(false) }
    trackTask(created)
    setChapter((prev) => ({ ...prev, status: 'generating' }))
    await followTask(created)
  }

  /** 按建议重写：与整章生成同样走后台任务（创建即抢占、正文只读、可停止）；任务创建成功即返回 true */
  const runRevise = async (body: ReviseChapterRequest): Promise<boolean> => {
    if (!canUseAI) return false
    if (!(await ensureSaved())) return false
    setStarting(true)
    let created: Task
    try {
      const current = sceneEditor.controller.getSnapshot().scene
      if (!current) { setStarting(false); return false }
      const request = { ...body, ai_config_id: aiConfigId, style: styleChoice, skill_id: skillId, expected_content_revision: current.content_revision, expected_planning_revision: current.planning_revision }
      created = aiScope === 'scene' ? await sceneApi.revise(pid, chapter.id, current.id, request) : await reviseChapter(chapter.id, request)
      if (!canEditNow()) return false
      if (sceneEditor.controller.getSnapshot().scene?.id !== current.id) { trackTask(created); setStarting(false); return true }
    } catch (err) {
      if (canEditNow() && sceneEditor.controller.getSnapshot().scene?.id === currentSceneId) message.error(errorMessage(err))
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
    if (!canUseAI) return
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
      confirm({
        title: aiScope === 'scene' ? '重新生成当前场景？' : '按现有场景结构重新生成整章？',
        content: aiScope === 'scene' ? '只更新当前场景正文，摘要和规划不变；旧稿自动保留历史。中途停止保持原样。' : '按顺序逐场景处理，仅更新正文，身份、顺序、摘要与规划不变。全部成功后整章统一保存，中途停止不部分覆盖。目标字数是整章总量。',
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

  const handleRestore = async (v: ChapterVersionBrief, expectedRevision?: number, confirmRichDocumentLoss = false): Promise<boolean> => {
    if (!canEditNow()) return false
    if (!(await ensureSaved())) return false
    if (!canEditNow()) return false
    try {
      if (!canEditNow()) return false
      const updated = await versionApi.restore(pid, chapter.id, v.id, expectedRevision ?? sceneEditor.controller.getSnapshot().scene?.content_revision, confirmRichDocumentLoss)
      if (!canEditNow()) return false
      if (sceneEditor.controller.getSnapshot().scene?.id === currentSceneId) replaceFromServer(updated)
      message.success(`已恢复到 v${v.seq}`)
      onChanged()
      void refreshProject()
      void refreshAnalysis()
      return true
    } catch (err) {
      if (!canEditNow()) return false
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
    if (!canEditNow()) return
    if (!canUseAI) return
    setResetting(true)
    try {
      if (!canEditNow()) return
      const updated = await chapterApi.update(pid, chapter.id, { status: 'draft' })
      if (!canEditNow()) return
      setChapter(updated)
      message.success('已复位为草稿')
      onChanged()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    } finally {
      setResetting(false)
    }
  }

  const handleToggleDone = async () => {
    if (!canEditNow()) return
    if (editorLocked || saving) return
    if (!canEditNow()) return
    if (dirty && !(await save())) return
    if (!canEditNow()) return
    try {
      if (!canEditNow()) return
      const updated = await chapterApi.update(pid, chapter.id, {
        status: chapter.status === 'done' ? 'draft' : 'done',
      })
      if (!canEditNow()) return
      setChapter(updated)
      onChanged()
    } catch (err) {
      if (!canEditNow()) return
      message.error(errorMessage(err))
    }
  }

  const otherSceneWriting = canUseAI && activeTasks.some((task) => task.chapter_id === chapter.id && isChapterWriteKind(task.kind) && isActiveTask(task.status) && !!task.scene_id && task.scene_id !== scene?.id)
  const wasOtherSceneWriting = useRef(false)
  useEffect(() => {
    if (otherSceneWriting) { wasOtherSceneWriting.current = true; return }
    if (!wasOtherSceneWriting.current) return
    wasOtherSceneWriting.current = false
    if (!canReadNow()) return
    // 未订阅的异景任务退出也重新读取章级派生状态，不推测终态或自动发起分析。
    void sceneEditor.controller.load()
    void chapterApi.get(pid, chapter.id).then((fresh) => {
      if (!canReadNow()) return
      setChapter(fresh)
      setReviseCompare(null)
      onChanged()
      void refreshProject()
      void refreshAnalysis()
    }).catch((err: unknown) => { if (canReadNow()) message.error(errorMessage(err)) })
  }, [otherSceneWriting, sceneEditor.controller, pid, chapter.id, canReadNow, message, onChanged, refreshProject, refreshAnalysis])
  const stuck = canUseAI && !otherSceneWriting && chapter.status === 'generating' && !aiRunning && attachChecked && !batchWriting
  // 批量正在写本章：正文 / 标题只读、不能保存（后端对生成中章节的正文 PATCH 返回 409，写完后会用服务端正文覆盖本地）
  const batchLocked = otherSceneWriting || batchWriting && chapter.status === 'generating'
  const status = aiRunning ? CHAPTER_STATUS.generating : CHAPTER_STATUS[chapter.status]
  const revising = aiTask.running && aiTask.task?.kind === 'chapter.revise'
  const reviseRunningCopy = reviseRunningPresentation(aiTask.task, autoAnalyze)
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
      : !(aiScope === 'scene' ? content : chapter.content).trim()
        ? aiScope === 'scene' ? '当前场景正文为空，无法重写' : '章节正文为空，无法重写'
        : aiRunning || stuck || chapter.status === 'generating' || !attachChecked
          ? '本章正在生成中，请稍候'
          : saving || rewriteLocked || sectionLocked
            ? '请先完成保存或局部改写'
            : ''

  const toolbar = (
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
      <Button
        size="small"
        icon={<BookOutlined />}
        onClick={() =>
          navigate(`/projects/${pid}/read/${chapter.id}`, { state: { from: `/projects/${pid}/chapters/${chapter.id}` } })
        }
      >
        阅读模式
      </Button>
      {canUseAI && <Badge count={pendingChanges} size="small" title={`${pendingChanges} 项设定变化待确认`}>
        <Button size="small" icon={<ExperimentOutlined />} onClick={() => setAnalysisOpen(true)}>
          分析
        </Button>
      </Badge>}
      <ChapterForeshadowButton projectId={pid} chapterId={chapter.id} />
      <Button size="small" icon={<HistoryOutlined />} onClick={() => setVersionsOpen(true)}>
        版本历史
      </Button>
      <Button size="small" disabled={!canEdit || (busy || editorLocked || stuck || batchLocked)} onClick={() => void handleToggleDone()}>
        {chapter.status === 'done' ? '标记为草稿' : '标记为完成'}
      </Button>
      <Button
        type="primary"
        size="small"
        icon={<SaveOutlined />}
        loading={saving}
        disabled={!canEdit || (editorLocked || stuck || batchLocked || sceneEditor.composing || !dirty)}
        onClick={() => void handleSave()}
      >
        保存
      </Button>
      {!isMobile && (
        <Button
          size="small"
          icon={<SettingOutlined />}
          type={inspectorOpen ? 'default' : 'primary'}
          onClick={() => setInspectorOpen((prev) => !prev)}
          title={inspectorOpen ? '收起右侧辅助面板（进入宽屏沉浸码字模式）' : '展开右侧辅助面板（AI助手/场景规划/章节设置）'}
        >
          {inspectorOpen ? '收起侧栏' : '辅助面板'}
        </Button>
      )}
    </Space>
  )

  const aiPanelContent = (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Segmented aria-label="AI处理范围" value={aiScope} disabled={editorLocked || saving} options={[{ value: 'scene', label: '当前场景' }, { value: 'chapter', label: '整章保结构' }]} onChange={setAiScope} />
      <Typography.Text type="secondary">{aiScope === 'scene' ? `只更新场景 ${(scene?.sort ?? 0) + 1} 的正文；摘要与规划不变` : '逐场景生成提案，整章统一保存；目标字数为整章总量'}</Typography.Text>
      <div>
        <Typography.Text type="secondary">AI 配置</Typography.Text>
        <AIConfigSelect value={aiConfigId} onChange={setAiConfigId} disabled={editorLocked} />
      </div>
      <div>
        <Typography.Text type="secondary">本次写作风格</Typography.Text>
        {styleSelect(editorLocked)}
      </div>
      <div>
        <Typography.Text type="secondary">Skill（可选）</Typography.Text>
        {skillSelect(editorLocked)}
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
                {aiScope === 'chapter' && (sceneAllocations ? <Typography.Text type="secondary">已保存结构的总量分配：{sceneAllocations.slice(0, 12).map((item, index) => `场景 ${index + 1}：${item.words} 字`).join('；')}{sceneAllocations.length > 12 ? `；其余 ${sceneAllocations.length - 12} 个场景按同一均分规则分配` : ''}</Typography.Text> : <Alert type="warning" message="目标总字数不足以为每个场景分配正数字数，或场景目录尚未加载。" />)}
                {aiScope === 'chapter' && <ContextPreviewPanel chapterId={chapter.id} />}
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
            disabled: aiRunning || sectionLocked,
            children: (
              <RewritePanel
                key={scene?.id ?? 'loading'}
                sceneId={scene?.id}
                getSceneVersions={() => { const current = sceneEditor.controller.getSnapshot().scene; return current ? { content_revision: current.content_revision, planning_revision: current.planning_revision } : null }}
                onSceneApplied={() => { void sceneEditor.controller.load(undefined, false); void reloadChapter(); onChanged(); void refreshProject(); void refreshAnalysis() }}
                pid={pid}
                chapterId={chapter.id}
                content={content}
                selection={selection}
                aiConfigId={aiConfigId}
                styleChoice={styleChoice}
                skillId={skillId}
                // 章节 generating（含挂载后尚未查完活动任务）时后端会 409，一律禁用
                disabled={!scene || !selectionEditable || aiRunning || sectionLocked || saving || chapter.status === 'generating' || hasConfig === false}
                ensureSaved={ensureSaved}
                onLockChange={setRewriteLocked}
                onApplied={handleRewriteApplied}
              />
            ),
          },
        ]}
      />
      {aiTask.running && <><Typography.Text strong>{aiTask.task?.scene_id ? `场景任务 #${aiTask.task.scene_id} · 只读提案` : '整章保结构任务 · 只读提案'}</Typography.Text><StreamOutput text={aiTask.text} running maxHeight={320} /></>}
      {revising && (
        <Alert type="info" showIcon message={reviseRunningCopy.title} description={reviseRunningCopy.description} />
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
          disabled={!scene || sceneEditor.loading || !!sceneEditor.conflict || hasConfig === false || stuck || otherSceneWriting || saving || !attachChecked || (aiScope === 'chapter' && !sceneAllocations)}
          onClick={confirmGenerate}
        >
          {aiScope === 'scene' ? content.trim() ? '重新生成当前场景' : '生成当前场景正文' : '按现有场景生成整章'}
        </Button>
      ) : (
        <Button
          type="primary"
          block
          icon={<ThunderboltOutlined />}
          disabled={!scene || hasConfig === false || stuck || otherSceneWriting || saving || !attachChecked || !(aiScope === 'scene' ? content : chapter.content).trim()}
          onClick={() => void runAI('polish')}
        >
          {aiScope === 'scene' ? '润色当前场景' : '逐场景润色整章'}
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
          当前场景使用本场景有效规划和开始时的设定；整章按现有场景顺序生成，仅更新正文，全部成功后统一保存。旧稿自动保留历史，任务可离页继续；场景与整章提案始终只读，不覆盖本地草稿。
        </Typography.Paragraph>
      )}
    </Space>
  )

  const scenePlanningContent = (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <div style={{ paddingBottom: 10, borderBottom: `1px solid ${token.colorBorderSecondary}` }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
          <Typography.Text strong style={{ fontSize: 14 }}>
            {scene ? `场景 ${(scene.sort ?? 0) + 1}：${scene.title || '未命名场景'}` : '未选中场景'}
          </Typography.Text>
          {scene && (
            <Tag color={scene.word_count ? 'blue' : 'default'} style={{ margin: 0 }}>
              {scene.word_count ? `${scene.word_count} 字` : '未写'}
            </Tag>
          )}
        </div>
        {scene && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            内容版本 rev.{scene.content_revision} · 规划版本 rev.{scene.planning_revision}
          </Typography.Text>
        )}
      </div>

      {scene ? (
        <>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <Typography.Text strong style={{ fontSize: 13 }}>
                作者摘要
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                独立保存 · AI写作参考
              </Typography.Text>
            </div>
            <Input.TextArea
              aria-label="场景摘要"
              value={sceneEditor.draft.summary}
              maxLength={200000}
              placeholder="输入当前场景的情节推进、关键冲突或注意事项..."
              autoSize={{ minRows: 2, maxRows: 6 }}
              onChange={(event) => sceneEditor.controller.change({ summary: event.target.value })}
              disabled={!canEdit || editorLocked || saving}
            />
          </div>

          <ScenePlanningPanel
            key={`planning:${scene.id}`}
            projectId={pid}
            scene={scene}
            disabled={editorLocked || saving}
            ensureSaved={ensureSaved}
            onChanged={() => {
              void sceneEditor.controller.load()
              onChanged()
            }}
          />

          <SceneSettingPreview
            key={`setting:${scene.id}:${scene.content_revision}`}
            projectId={pid}
            chapterId={chapter.id}
            sceneId={scene.id}
          />
        </>
      ) : (
        <Alert type="info" message="请选择或新建一个场景进行规划。" />
      )}
    </Space>
  )

  const chapterSettingsContent = (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <div>
        <div style={{ marginBottom: 6 }}>
          <Typography.Text strong style={{ fontSize: 13 }}>
            本章出场角色
          </Typography.Text>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '2px 0 6px' }}>
            手动指定本章出场角色。主角始终注入，隐藏字段仍受保护。
          </Typography.Paragraph>
        </div>
        <CastSelect
          pid={pid}
          chapterId={chapter.id}
          disabled={editorLocked || saving || batchLocked || stuck}
          hideLabel
        />
      </div>

      <div>
        <div style={{ marginBottom: 6 }}>
          <Typography.Text strong style={{ fontSize: 13 }}>
            本章写作风格
          </Typography.Text>
          <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '2px 0 6px' }}>
            生成、润色与局部改写默认继承本章风格；未设置时继承项目默认风格。
          </Typography.Paragraph>
        </div>
        <ChapterStyleSetting
          pid={pid}
          chapterId={chapter.id}
          value={chapter.style_id}
          projectDefault={projectDefaultStyle}
          readOnly={managingOther}
          disabled={editorLocked}
          hideLabel
          onChanged={(styleId) => setChapter((prev) => ({ ...prev, style_id: styleId }))}
        />
      </div>

      <div
        style={{
          marginTop: 8,
          padding: '12px 14px',
          background: token.colorFillAlter,
          border: `1px solid ${token.colorBorderSecondary}`,
          borderRadius: 8,
        }}
      >
        <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 8 }}>
          章节概览
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px 12px', fontSize: 12 }}>
          <div>
            <span style={{ color: token.colorTextSecondary }}>当前场景字数：</span>
            <span style={{ fontWeight: 500 }}>{words} 字</span>
          </div>
          <div>
            <span style={{ color: token.colorTextSecondary }}>全章合计字数：</span>
            <span style={{ fontWeight: 500 }}>{chapter.word_count} 字</span>
          </div>
          <div>
            <span style={{ color: token.colorTextSecondary }}>场景总数：</span>
            <span style={{ fontWeight: 500 }}>{sceneEditor.page?.total ?? 0} 个</span>
          </div>
          <div>
            <span style={{ color: token.colorTextSecondary }}>章节状态：</span>
            <Tag color={status.color} style={{ margin: 0, fontSize: 11, lineHeight: '18px', padding: '0 4px' }}>
              {status.label}
            </Tag>
          </div>
        </div>
      </div>
    </Space>
  )

  const inspectorTabItems = useMemo(() => {
    const items = []
    if (canUseAI) {
      items.push({ key: 'ai', label: 'AI 助手', icon: <RobotOutlined /> })
    }
    items.push(
      { key: 'scene', label: '场景规划', icon: <FileTextOutlined /> },
      { key: 'chapter', label: '章节设置', icon: <SettingOutlined /> },
    )
    return items
  }, [canUseAI])

  useEffect(() => {
    if (!canUseAI && inspectorTab === 'ai') {
      setInspectorTab('scene')
    }
  }, [canUseAI, inspectorTab])

  const inspectorPanel = (
    <Card
      size="small"
      styles={{
        header: { padding: '4px 12px', minHeight: 46 },
        body: { padding: '12px 14px', maxHeight: 'calc(100vh - 120px)', overflowY: 'auto' },
      }}
      title={
        <Tabs
          activeKey={inspectorTab}
          onChange={(k) => setInspectorTab(k as 'ai' | 'scene' | 'chapter')}
          size="small"
          style={{ marginBottom: -8 }}
          items={inspectorTabItems}
        />
      }
    >
      {inspectorTab === 'ai' && aiPanelContent}
      {inspectorTab === 'scene' && scenePlanningContent}
      {inspectorTab === 'chapter' && chapterSettingsContent}
    </Card>
  )

  return (
    <>
      {externalChapter && <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="章节已被其他操作更新，本地场景草稿已保留" description="可加载最新章信息；场景草稿保留并单独核对，未保存的章节标题会在确认后替换。" action={<Button size="small" disabled={editorLocked || saving} onClick={() => confirm({ title: '加载最新章信息？', content: '未保存的章节标题会替换为服务端值；场景草稿仍保留，需单独核对。', onOk: async () => { if (!canEditNow()) return; const latest = await chapterApi.get(pid, chapter.id); if (!canEditNow()) return; replaceFromServer(latest); setExternalChapter(null) } })}>核对最新章信息</Button>} />}
      <div style={splitRow}>
        <div ref={editorPaneRef} style={inspectorOpen && !isMobile ? splitPane(640) : { flex: '1 1 100%', minWidth: 0 }}>
          <Card
            size="small"
            title={
              <Space wrap>
                <Button
                  type="text"
                  size="small"
                  icon={<ArrowLeftOutlined />}
                  aria-label="返回章节列表"
                  onClick={() => navigate(`/projects/${pid}/chapters`)}
                />
                <span>第 {chapter.chapter_no} 章</span>
                <Tag color={status.color}>{status.label}</Tag>
                <Typography.Text type="secondary">当前场景 {words} 字 · 整章 {chapter.word_count} 字</Typography.Text>
                {dirty && !aiRunning && <Tag color="orange">未保存</Tag>}
                {canUseAI && <Tag color={analysisStatus.color} style={{ cursor: 'pointer' }} onClick={() => setAnalysisOpen(true)}>
                  {analysisStatus.label}
                </Tag>}
              </Space>
            }
            extra={toolbarInHeader ? toolbar : undefined}
          >
            {!toolbarInHeader && <div style={{ marginBottom: 12 }}>{toolbar}</div>}
            {batchWriting && chapter.status === 'generating' && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 12 }}
                message="该章节正在批量生成中"
                description="写完后自动刷新正文；进度见章节列表顶部或任务面板。"
              />
            )}
            {managedGenerating && <Alert type="info" showIcon style={{ marginBottom: 12 }} message="项目所有者正在生成此章节，完成后可刷新并继续编辑。" />}
            {stuck && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 12 }}
                message="该章节处于「生成中」状态"
                description="未找到进行中的 AI 任务；如果确认生成已中断，可复位为草稿后继续编辑。"
                action={
                  <Button size="small" loading={resetting} onClick={() => void handleResetStatus()} disabled={!canEdit}>
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
              disabled={!canEdit || editorLocked || batchLocked || saving}
              variant="borderless"
              style={{ fontSize: 20, fontWeight: 600, paddingInline: 0, marginBottom: 8 }}
            />
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
              <Space size={8} wrap style={{ fontSize: 12 }}>
                <Tag color="default" style={{ margin: 0, borderRadius: 10 }}>
                  风格：{chapter.style_id ? '本章专属风格' : (projectDefaultStyle ? `默认 (${projectDefaultStyle.name})` : '继承项目')}
                </Tag>
                <Button
                  type="link"
                  size="small"
                  style={{ padding: 0, height: 'auto', fontSize: 12 }}
                  onClick={() => {
                    setInspectorTab('chapter')
                    setInspectorOpen(true)
                  }}
                >
                  ⚙️ 章节设置 (角色/风格) →
                </Button>
              </Space>
            </div>
            {otherSceneWriting && <Alert type="info" showIcon message="本章另一场景正在后台写作；当前草稿保留，完成后请核对最新版本。" style={{ marginBottom: 12 }} />}
            <SceneWritingPanel
              aiConfigId={aiConfigId}
              styleChoice={styleChoice}
              skillId={skillId}
              onSectionLockChange={setSectionLocked}
              onStructureDirtyChange={setStructureDirty}
              onOpenInspectorTab={(tab) => {
                setInspectorTab(tab)
                setInspectorOpen(true)
              }}
              projectId={pid}
              chapter={chapter}
              state={sceneEditor}
              controller={sceneEditor.controller}
              disabled={editorLocked || batchLocked || saving || stuck}
              selection={selection}
              onSelection={setSelection}
              ensureSaved={ensureSaved}
              onChanged={() => { onChanged(); void reloadChapter(); void refreshProject(); void refreshAnalysis() }}
              onSelect={async (sid) => { if (rewriteLocked || sectionLocked || aiRunning || saving) return false; if (!(await ensureSaved())) return false; const selected = await sceneEditor.controller.select(sid); if (selected) { setSelection({ start: 0, end: 0 }); const next = new URLSearchParams(searchParams); next.set('scene_id', String(sid)); setSearchParams(next, { replace: true }) } return selected }}
              mentionAnchor={mentionAnchor}
              markerJump={markerJump}
              onNavigateMarker={async (target: MarkerLocation) => { if (rewriteLocked || sectionLocked || structureDirty || aiRunning || saving) { message.warning('请先完成当前场景排序或改写预览。'); return false } if (!(await ensureSaved())) return false; navigate(`/projects/${pid}/chapters/${target.chapterId}?scene_id=${target.sceneId}`, { state: { markerLocation: target } }); return true }}
            />
            <div style={{ marginTop: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                最后更新：{formatTime(chapter.updated_at)} · Ctrl/⌘ + S 保存
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                场景 {scene ? scene.sort + 1 : 0} · {words} 字
              </Typography.Text>
            </div>
          </Card>
        </div>
        {!isMobile && inspectorOpen && <div style={{ flex: '0 0 360px', width: 360, minWidth: 320 }}>{inspectorPanel}</div>}
      </div>
      {isMobile && (
        <>
          <FloatButton
            type="primary"
            icon={<SettingOutlined />}
            tooltip="辅助面板"
            badge={aiRunning ? { dot: true } : undefined}
            style={{ insetInlineEnd: 24, bottom: 104 }}
            onClick={() => setAiPanelOpen(true)}
          />
          <Drawer
            title={inspectorTab === 'ai' ? '🤖 AI 助手' : inspectorTab === 'scene' ? '📋 场景规划' : '⚙️ 章节设置'}
            extra={
              <Segmented
                size="small"
                value={inspectorTab}
                onChange={(val) => setInspectorTab(val as 'ai' | 'scene' | 'chapter')}
                options={inspectorTabItems.map((item) => ({ value: item.key, label: item.label }))}
              />
            }
            placement="bottom"
            height="85vh"
            open={aiPanelOpen}
            forceRender
            onClose={() => setAiPanelOpen(false)}
            styles={{ body: { padding: 12 } }}
          >
            {inspectorTab === 'ai' && aiPanelContent}
            {inspectorTab === 'scene' && scenePlanningContent}
            {inspectorTab === 'chapter' && chapterSettingsContent}
          </Drawer>
        </>
      )}
      <VersionDrawer
        key={`versions:${currentSceneId}`}
        pid={pid}
        chapterId={chapter.id}
        open={versionsOpen}
        onClose={() => setVersionsOpen(false)}
        currentContent={chapter.content}
        currentDirty={false}
        sceneContext={{ scenes: sceneEditor.page?.items ?? [], currentSceneId: scene?.id ?? null, prepare: async () => await ensureSaved() ? sceneEditor.controller.getSnapshot().scene?.content_revision ?? null : null, onExtract: async (version, preview, confirmRichDocumentLoss) => { if (!canEditNow()) return false; try { await versionApi.extract(pid, chapter.id, version.id, { scene_id: preview.scene_id, expected_content_revision: preview.content_revision, signature: preview.signature, confirm_rich_document_loss: confirmRichDocumentLoss }); if (!canEditNow()) return false; await sceneEditor.controller.load(); await reloadChapter(); onChanged(); void refreshProject(); void refreshAnalysis(); message.success('完整旧稿已提取到指定场景，其他场景不变'); return true } catch (err) { if (canEditNow()) message.error(errorMessage(err)); return false } } }}
        restoreDisabled={editorLocked || saving || stuck}
        onRestore={handleRestore}
      />
      {canUseAI && <AnalysisDrawer
        key={`analysis:${currentSceneId}`}
        open={analysisOpen || panelRequested}
        onClose={closeAnalysis}
        chapterId={chapter.id}
        reviseScope={aiScope === 'scene'
          ? { kind: 'scene', sceneId: currentSceneId ?? null, sceneTitle: scene?.title, sceneOrder: scene?.sort }
          : { kind: 'chapter', sceneCount: sceneEditor.page?.total ?? 0 }}
        reviseTask={revising ? aiTask.task : null}
        analysis={analysis}
        autoAnalyze={autoAnalyze}
        analyzeDisabled={hasConfig === false || aiRunning || stuck || saving || !chapter.content.trim()}
        onAnalyze={runAnalyze}
        reviseBlockedReason={reviseBlockedReason}
        reviseRunning={revising}
        onRevise={runRevise}
        compare={reviseCompare}
        reviseStyle={styleSelect(aiRunning)}
        reviseSkill={skillSelect(aiRunning)}
      />}
      <Modal
        open={blocker.state === 'blocked'}
        title={dirty ? '有未保存的修改' : structureDirty ? '场景顺序尚未保存' : '局部改写结果未采纳'}
        okText="离开"
        cancelText="留下"
        okButtonProps={{ danger: true }}
        onOk={() => blocker.proceed?.()}
        onCancel={() => blocker.reset?.()}
      >
        {dirty
          ? '离开后未保存的修改将丢失，确定离开吗？'
          : structureDirty ? '离开将丢弃尚未提交的拟排序（服务端顺序不受影响）。确定离开吗？' : '离开将丢弃局部改写的预览结果（正文不受影响）。确定离开吗？'}
      </Modal>
    </>
  )
}
