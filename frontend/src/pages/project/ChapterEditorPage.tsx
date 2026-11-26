import ChapterDatesButton from '../../components/storyTimeline/ChapterDatesButton'
import { useSettingsConfirmation } from '../../components/systemSettings/useSettingsConfirmation'
import { readMentionAnchor } from '../../utils/mentions'
import {
  ArrowLeftOutlined,
  BookOutlined,
  FileTextOutlined,
  FormOutlined,
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
  ConfigProvider,
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
import { createPortal } from 'react-dom'
import { Link, useBlocker, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
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
import { isTaskKey, queueLabel, SCENE_RESTRUCTURE_TASK_QUERY } from '../../api/tasks'
import type { Chapter, ChapterBrief, ChapterVersionBrief, Plan, ReviseChapterRequest, SceneMoveChapter, StyleChoice, Task } from '../../api/types'
import AIConfigSelect from '../../components/AIConfigSelect'
import AnalysisDrawer, { type ReviseCompare } from '../../components/chapter/AnalysisDrawer'
import { GenerateForm, type GenerateOptions, PolishForm } from '../../components/chapter/ChapterAIForms'
import CastSelect from '../../components/chapter/CastSelect'
import ChapterStyleSetting from '../../components/chapter/ChapterStyleSetting'
import ChapterPlanCard from '../../components/chapter/ChapterPlanCard'
import ContextPreviewPanel from '../../components/chapter/ContextPreviewPanel'
import RewritePanel, { type TextSelection } from '../../components/chapter/RewritePanel'
import VersionDrawer from '../../components/chapter/VersionDrawer'
import NoteSidePanel from '../../components/notes/NoteSidePanel'
import { chapterLeaveBlocked } from '../../components/notes/noteDraft'
import SkillChoiceField from '../../components/skills/SkillChoiceField'
import { useSkillChoice } from '../../hooks/useSkillChoice'
import { skillChoiceProblemText } from '../../utils/skillInputs'
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
import { readAutoContinue } from '../../utils/preferences'
import { splitPane, splitRow } from '../../utils/layout'
import { formatTime } from '../../utils/time'
import { useProjectId } from '../../hooks/useProjectId'
import { useChapterImpactHint } from '../../hooks/useChapterImpactHint'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { impactHref } from '../../utils/consistencyImpact'
import DraftRecoveryPanel from '../../components/editorDrafts/DraftRecoveryPanel'
import EditorSaveStatus from '../../components/editorDrafts/EditorSaveStatus'
import TitleDraftConflict from '../../components/editorDrafts/TitleDraftConflict'
import { useChapterAutosave } from '../../components/editorDrafts/useChapterAutosave'
import { downloadJson } from '../../api/cards'
import type { EditorDraftTarget } from '../../components/editorDrafts/editorDraftRecord'
import './ChapterEditorPage.css'

const SUCCESS_MESSAGES: Partial<Record<Task['kind'], string>> = {
  'chapter.generate': '生成完成，已保存',
  'chapter.polish': '润色完成，已保存',
  'chapter.revise': '按建议重写完成，已保存',
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
type ChapterTaskRequest = { generation: number; scope: () => boolean }

/** 正文卡片宽度达到它时工具栏放进卡片标题栏（≈ 1600px 视口、停靠面板关闭时正文栏的宽度，含竖向滚动条时约 935）。 */
const TOOLBAR_IN_HEADER_MIN_WIDTH = 920

type InspectorTab = 'ai' | 'scene' | 'chapter' | 'notes'
const INSPECTOR_TITLES: Record<InspectorTab, string> = { ai: '🤖 AI 助手', scene: '📋 场景规划', chapter: '⚙️ 章节设置', notes: '📝 笔记' }

function ChapterEditor({ pid, initial, siblings, onChanged }: EditorProps) {
  const { canUseAI, managingOther, canEdit, canEditNow, canReadNow, role, isCollaborator } = useProjectAccess(pid)
  const isMobile = useIsMobile()
  // 工具栏按钮较多：只在正文卡片足够宽（约等于原 xxl 视口、停靠面板关闭时的宽度）时放进卡片标题栏，
  // 其余宽度放在正文上方，避免标题被挤压；按卡片实际宽度判断，右侧停靠面板打开时同样生效
  const [editorPaneRef, editorPaneWidth] = useElementWidth<HTMLDivElement>()
  const toolbarInHeader = editorPaneWidth >= TOOLBAR_IN_HEADER_MIN_WIDTH
  const { token } = theme.useToken()
  const [inspectorOpen, setInspectorOpen] = useState(true)
  const [selectedInspectorTab, setInspectorTab] = useState<InspectorTab>('ai')
  // 无 AI 权限时 AI 标签不可用，回退到场景规划（派生而非在 effect 中改写状态）
  const inspectorTab: InspectorTab = !canUseAI && selectedInspectorTab === 'ai' ? 'scene' : selectedInspectorTab
  // 未保存状态并入本页离开拦截。
  const [noteDirty, setNoteDirty] = useState(false)
  // 同一容器在桌面侧栏和移动抽屉之间移动，保留笔记组件及其草稿。
  const [notePanelHost] = useState(() => typeof document === 'undefined' ? null : document.createElement('div'))
  const attachNotePanel = useCallback((slot: HTMLDivElement | null) => {
    if (slot && notePanelHost) slot.appendChild(notePanelHost)
  }, [notePanelHost])
  // 搜索下拉等浮层随笔记栏移动和隐藏。
  const getNotePopupContainer = useCallback(() => notePanelHost ?? document.body, [notePanelHost])
  // 移动端辅助面板收进底部抽屉（forceRender：关闭抽屉不丢局部改写等面板内状态）
  const [aiPanelOpen, setAiPanelOpen] = useState(false)
  const { message, modal } = App.useApp()
  const navigate = useNavigate()
  const location = useLocation()
  const refreshProject = useProjectStore((s) => s.refresh)
  const projectScopeVersion = useProjectStore((s) => s.scopeVersion)
  const trackTask = useTasksStore((s) => s.track)
  const hasConfig = useHasAIConfig()
  const autoAnalyze = useProjectStore((s) => s.project?.auto_analyze === true)
  const projectDefaultStyle = useProjectStore((s) => s.project?.default_style ?? null)
  const [searchParams, setSearchParams] = useSearchParams()
  const restructureTask = searchParams.get(SCENE_RESTRUCTURE_TASK_QUERY)
  const restructureTaskKey = isTaskKey(restructureTask) ? restructureTask : undefined
  const [lastSceneMove, setLastSceneMove] = useState<{ target: SceneMoveChapter; sceneId: number; scope: number }>()
  const markerJump = useMemo(() => readMarkerLocation(location.state, initial.id), [location.state, initial.id])
  const mentionAnchor = useMemo(() => readMentionAnchor(searchParams), [searchParams])

  const [chapter, setChapter] = useState<Chapter>(initial)
  const [compositionTitle, setCompositionTitle] = useState<string | null>(null)
  const titleComposing = useRef(false)
  const requestedSceneId = Number(searchParams.get('scene_id')) || undefined
  const requestedDraft = searchParams.get('draft') ?? undefined
  const [draftsOpen, setDraftsOpen] = useState(false)
  const sceneEditor = useSceneEditor(pid, initial.id, requestedSceneId)
  const scene = sceneEditor.scene
  const selectionEditable = useMemo(() => {
    if (!sceneEditor.documentDraft) return false
    try { return validSectionDocument(sceneEditor.documentDraft) && !!editorFromSectionDocument(sceneEditor.documentDraft) } catch { return false }
  }, [sceneEditor.documentDraft])
  const currentSceneId = scene?.id
  const confirm = useSettingsConfirmation(modal, !canEdit, `${pid}:${initial.id}:${currentSceneId ?? ''}`)
  const content = sceneEditor.draft.content
  const [chosenAIScope, setAiScope] = useState<'scene' | 'chapter'>()
  const aiScope = chosenAIScope ?? (autoAnalyze && sceneEditor.page?.total === 1 ? 'chapter' : 'scene')
  const [aiMode, setAiMode] = useState<AIMode>('generate')
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  // 本次生成 / 润色 / 重写 / 局部改写临时使用的风格（inherit = 本章风格 > 项目默认）
  const [styleChoice, setStyleChoice] = useState<StyleChoice>('inherit')
  // 本次生成 / 润色 / 重写 / 局部改写选用的 Skill 及其输入项（补充工作流，任务创建时快照）
  const skillChoice = useSkillChoice()
  const skillProblem = skillChoiceProblemText(skillChoice)
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
  const [rewriteLocked, setRewriteLocked] = useState(false)
  const [sectionLocked, setSectionLocked] = useState(false)
  const [structureDirty, setStructureDirty] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [analysisOpen, setAnalysisOpen] = useState(false)
  // 按建议重写成功后的评分对比基准（由任务的 analysis_id 得出，任务面板重试 / 其他标签页发起的重写同样适用）
  const [reviseCompare, setReviseCompare] = useState<ReviseCompare | null>(null)
  const analysis = useChapterAnalysis(initial.id, canUseAI)
  const refreshAnalysis = analysis.refresh
  // 正文保存成功（内容版本变化）或重新分析完成后重新读取；只读取、失败静默，不参与保存与离页拦截
  const impactHint = useChapterImpactHint(pid, initial.id, `${chapter.content_revision}:${analysis.state?.latest_analysis_id ?? ''}`)
  // 最新已知分析 id：followTask 可能经由旧闭包调用，用 ref 读取最新值
  const latestAnalysisIdRef = useRef<number | null>(null)
  useEffect(() => {
    latestAnalysisIdRef.current = analysis.state?.latest_analysis_id ?? null
  })
  // 整章生成 / 润色走后台任务；局部改写仍是 RewritePanel 内的同步流
  const aiTask = useTaskStream()
  // 正在创建任务（请求未返回）
  const [startingRequest, setStartingRequest] = useState<ChapterTaskRequest | null>(null)
  const requestRef = useRef({ generation: 0, mounted: true, pending: null as ChapterTaskRequest | null })
  // 授权代次变化后，旧请求不再占用当前页面；迟到响应仍须通过原作用域校验。
  const starting = startingRequest !== null && startingRequest.scope === canEditNow
  useEffect(() => {
    const state = requestRef.current
    state.mounted = true
    return () => {
      state.mounted = false
      state.generation += 1
      state.pending = null
    }
  }, [])
  const beginTaskRequest = (): ChapterTaskRequest | null => {
    if (!canEditNow() || requestRef.current.pending?.scope === canEditNow) return null
    const request = { generation: ++requestRef.current.generation, scope: canEditNow }
    requestRef.current.pending = request
    setStartingRequest(request)
    return request
  }
  const isCurrentRequest = (request: ChapterTaskRequest) => requestRef.current.mounted
    && requestRef.current.generation === request.generation && request.scope()
  const finishTaskRequest = (request: ChapterTaskRequest) => {
    if (!requestRef.current.mounted || requestRef.current.pending !== request) return
    requestRef.current.pending = null
    setStartingRequest(null)
  }
  // 旧任务的章节 / 场景整数 ID 无实例证明；只跟随本页面明确创建的任务。
  const attachChecked = true
  const detachWriteTask = aiTask.detach
  const followedWriteTask = aiTask.task
  const writeTaskRunning = aiTask.running
  useEffect(() => {
    const task = followedWriteTask
    if (task?.scene_id && task.scene_id !== currentSceneId && writeTaskRunning) {
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

  const aiRunning = aiTask.running || starting
  const aiStatus = aiTask.task?.status
  // AI 任务进行中（含排队）或局部改写预览中：正文只读，避免选区失效
  const managedGenerating = !canUseAI && chapter.status === 'generating'
  const editorLocked = aiRunning || rewriteLocked || sectionLocked || managedGenerating
  const saved = useCallback(() => {
    onChanged()
    void refreshProject()
    void refreshAnalysis()
  }, [onChanged, refreshProject, refreshAnalysis])
  const autosave = useChapterAutosave(initial, sceneEditor.controller, editorLocked || chapter.status === 'generating', setChapter, saved)
  const titleState = autosave.title.getSnapshot()
  const title = canEdit ? compositionTitle ?? titleState.value : chapter.title
  const saving = autosave.saving || sceneEditor.saving
  const actionLocked = autosave.actionLocked
  const dirty = canEdit && autosave.dirty
  const busy = aiRunning || saving || actionLocked
  const localDraft = autosave.journal.getSnapshot()
  const titleStamp = autosave.titleStamp()
  // 「继承」解析为本章风格 > 项目默认（下拉里显示实际会用哪个风格）
  const styleContext = useMemo(
    () => ({ chapterStyleId: chapter.style_id, projectDefault: projectDefaultStyle }),
    [chapter.style_id, projectDefaultStyle],
  )
  const styleSelect = (disabled: boolean) => (
    <StyleChoiceSelect value={styleChoice} onChange={setStyleChoice} context={styleContext} disabled={disabled} />
  )
  const skillSelect = (disabled: boolean) => <SkillChoiceField choice={skillChoice} disabled={disabled} />
  const words = useMemo(() => countWords(content), [content])
  const outputWords = useMemo(() => countWords(aiTask.text), [aiTask.text])

  const { prev, next } = useMemo(() => {
    const sorted = [...siblings].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id)
    const idx = sorted.findIndex((c) => c.id === chapter.id)
    return { prev: idx > 0 ? sorted[idx - 1] : undefined, next: idx >= 0 ? sorted[idx + 1] : undefined }
  }, [siblings, chapter.id])

  /** 服务端改写了正文（生成 / 采纳改写 / 回滚）：同步并把光标记录移到末尾（与浏览器行为一致）。 */
  const replaceFromServer = useCallback(
    (c: Chapter) => {
      autosave.receiveChapter(c)
      void sceneEditor.controller.load()
      setSelection({ start: 0, end: 0 })
    },
    [autosave, sceneEditor.controller, setSelection],
  )

  useProjectRefresh(pid, ['chapters', 'outlines', 'characters', 'memories'], () => {
    onChanged()
    void refreshAnalysis()
    void autosave.refreshedAuthority()
  })
  useProjectRefresh(pid, ['planning', 'setting-ai-visibility'], () => { void autosave.refreshedAuthority() })

  const handleSave = useCallback(async () => {
    if (!canEditNow()) return
    if (editorLocked || autosave.actionLocked) return
    if (autosave.composing) { message.info('输入法组合尚未结束，请确认文字后再保存。'); return }
    if (autosave.reviewNeeded) { message.info('请先核对保存结果与服务端版本。'); return }
    if (!autosave.dirty) {
      message.info('没有需要保存的修改')
      return
    }
    if (await autosave.saveNow() && canEditNow()) message.success('已保存')
  }, [editorLocked, autosave, message, canEditNow])

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
  const pageGuard = dirty || autosave.unknown || rewriteLocked || sectionLocked || structureDirty
  const guard = pageGuard || noteDirty
  useEffect(() => {
    if (!guard) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault()
      e.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [guard])
  // 笔记草稿只拦截离开本页；切场景不卸载笔记栏，因此不因笔记草稿拦截
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) => chapterLeaveBlocked(
      (canEditNow() && autosave.hasLocalChanges()) || rewriteLocked || sectionLocked || structureDirty, noteDirty, currentLocation, nextLocation,
    ),
  )

  const reloadChapter = async (currentRequest: () => boolean = canReadNow) => {
    if (!currentRequest()) return
    try {
      const fresh = await chapterApi.get(pid, chapter.id, currentRequest)
      if (!canReadNow() || !currentRequest()) return
      // 只同步服务端状态，保留本地正文（生成失败/中止时正文未变）
      autosave.receiveChapter(fresh)
    } catch {
      // 忽略
    }
  }

  /** AI 操作 / 回滚前：有未保存修改先自动保存。 */
  const ensureSaved = async (): Promise<boolean> => {
    if (autosave.composing) { message.warning('请先确认输入法组合文字。'); return false }
    if (autosave.reviewNeeded || autosave.restored) { message.warning('请先核对或保存本地草稿。'); return false }
    return (await autosave.runSavedAction(async () => true)) === true
  }

  const openSerialization = async () => {
    if (!canReadNow()) return
    if (canEditNow() && (autosave.hasLocalChanges() || autosave.reviewNeeded || autosave.restored || saving)) {
      if (!await ensureSaved()) return
    }
    if (canReadNow()) navigate(`/projects/${pid}/serialization?chapter=${chapter.id}`)
  }

  const selectScene = useCallback(async (sid: number, updateUrl = true) => {
    if (!canReadNow()) return false
    const controller = sceneEditor.controller
    if (controller.getSnapshot().scene?.id === sid) return true
    const selected = canEditNow() ? await autosave.runSavedAction(() => controller.load(sid, false))
      : await controller.load(sid)
    if (!selected || !canReadNow()) return false
    setSelection({ start: 0, end: 0 })
    if (updateUrl) setSearchParams((current) => {
      const next = new URLSearchParams(current)
      next.set('scene_id', String(sid))
      return next
    }, { replace: true })
    return true
  }, [canReadNow, canEditNow, sceneEditor.controller, autosave, setSearchParams, setSelection])

  const closeRestructureTask = () => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      next.delete(SCENE_RESTRUCTURE_TASK_QUERY)
      return next
    }, { replace: true })
  }

  const refreshSceneStructure = async (preferredId?: number, closeTask = false) => {
    const loaded = await sceneEditor.controller.load(preferredId)
    if (!canReadNow()) return
    if (loaded || closeTask) {
      const selected = loaded ? sceneEditor.controller.getSnapshot().scene?.id : undefined
      setSelection({ start: 0, end: 0 })
      setSearchParams((current) => {
        const next = new URLSearchParams(current)
        if (selected !== undefined) next.set('scene_id', String(selected))
        if (closeTask) next.delete(SCENE_RESTRUCTURE_TASK_QUERY)
        return next
      }, { replace: true })
    }
    void reloadChapter()
    saved()
  }

  const lastRequested = useRef(requestedSceneId)
  useEffect(() => {
    if (!requestedSceneId || sceneEditor.loading || actionLocked || (canEdit && !autosave.readyToSave)) return
    if (lastRequested.current === requestedSceneId) return
    lastRequested.current = requestedSceneId
    void selectScene(requestedSceneId, false)
  }, [requestedSceneId, sceneEditor.loading, actionLocked, canEdit, autosave.readyToSave, selectScene])

  /** 订阅章节任务直到结束：成功覆盖正文，失败 / 取消 / 中断只同步服务端状态并保留本地正文。 */
  const followTask = async (t: Task) => {
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
        replaceFromServer(done.chapter)
      } else {
        try {
          const fresh = await chapterApi.get(pid, chapter.id)
          if (!canReadNow()) return
          replaceFromServer(fresh)
        } catch {
          // 忽略
        }
      }
      // 结果未发布（收据受限收尾）：已按上面重新读取已保存的正文，提示沿用服务端说明。
      if (outcome.unpublished) message.info(outcome.detail)
      else if (done?.warning) message.warning(`生成完成，已保存；${done.warning}`)
      else message.success(SUCCESS_MESSAGES[t.kind] ?? '生成完成，已保存')
      const finishedTask = outcome.task ?? t
      if (finishedTask.kind === 'chapter.revise') {
        const cmp = reviseCompareFor({ ...finishedTask, scene_id: t.scene_id ?? finishedTask.scene_id }, latestAnalysisIdRef.current)
        // Scene 重写只令章分析过期；也清掉此前整章任务留下的比较，不等待不存在的自动分析。
        setReviseCompare(cmp)
      }
      onChanged()
      void refreshProject()
      // 整章保存后读取自动分析的排队状态。
      void refreshAnalysis()
      return
    }
    if (outcome.status === 'cancelled') message.info('已停止，正文保持原样')
    await reloadChapter()
  }
  // 当前数据没有章节 / 场景实例证明，不按后台任务里的旧整数关联自动恢复订阅。
  const batchWriting = false

  const sceneForAI = () => {
    const snapshot = sceneEditor.controller.getSnapshot()
    if (!chosenAIScope && aiScope === 'chapter' && (snapshot.page?.total !== 1 || snapshot.scene?.id !== currentSceneId)) {
      message.warning('场景目录已变化，请重新选择 AI 处理范围。')
      return null
    }
    return snapshot.scene
  }

  const runAI = async (mode: 'generate' | 'polish') => {
    if (skillProblem) { message.warning(skillProblem); return }
    if (!canUseAI || !canEditNow()) return
    await autosave.runSavedAction(async () => {
      const current = sceneForAI()
      if (!current) return
      const versions = { expected_content_revision: current.content_revision, expected_planning_revision: current.planning_revision }
      const request = beginTaskRequest()
      if (!request) return
      let created: Task
      try {
        if (mode === 'generate') {
          const body = { ai_config_id: aiConfigId, instruction: genOptions.instruction.trim(), target_words: genOptions.targetWords, auto_continue: genOptions.autoContinue, style: styleChoice, ...skillChoice.payload, ...versions }
          created = aiScope === 'scene' ? await sceneApi.generate(pid, chapter.id, current.id, body) : await generateChapter(chapter.id, body)
        } else {
          const body = { ai_config_id: aiConfigId, instruction: polishInstruction.trim(), style: styleChoice, ...skillChoice.payload, ...versions }
          created = aiScope === 'scene' ? await sceneApi.polish(pid, chapter.id, current.id, body) : await polishChapter(chapter.id, body)
        }
        if (!isCurrentRequest(request)) return
        if (sceneEditor.controller.getSnapshot().scene?.id !== current.id) { trackTask(created); return }
      } catch (err) {
        // 恢复读取也可能等待网络；先结束本次创建状态。
        finishTaskRequest(request)
        if (isCurrentRequest(request)) {
          if (sceneEditor.controller.getSnapshot().scene?.id === current.id) message.error(errorMessage(err))
          void reloadChapter(() => isCurrentRequest(request))
        }
        return
      } finally { finishTaskRequest(request) }
      trackTask(created)
      setChapter((prev) => ({ ...prev, status: 'generating' }))
      void followTask(created)
    })
  }

  /** 按建议改写的任务创建与保存共用屏障。 */
  const runRevise = async (body: ReviseChapterRequest): Promise<boolean> => {
    if (!canUseAI) return false
    if (skillProblem) { message.warning(skillProblem); return false }
    return (await autosave.runSavedAction(async () => {
      const current = sceneForAI()
      if (!current) return false
      const pending = beginTaskRequest()
      if (!pending) return false
      let created: Task
      try {
        const request = { ...body, ai_config_id: aiConfigId, style: styleChoice, ...skillChoice.payload, expected_content_revision: current.content_revision, expected_planning_revision: current.planning_revision }
        created = aiScope === 'scene' ? await sceneApi.revise(pid, chapter.id, current.id, request) : await reviseChapter(chapter.id, request)
        if (!isCurrentRequest(pending)) return false
        if (sceneEditor.controller.getSnapshot().scene?.id !== current.id) { trackTask(created); return true }
      } catch (err) {
        finishTaskRequest(pending)
        if (isCurrentRequest(pending)) {
          if (sceneEditor.controller.getSnapshot().scene?.id === current.id) message.error(errorMessage(err))
          void reloadChapter(() => isCurrentRequest(pending))
        }
        return false
      } finally { finishTaskRequest(pending) }
      trackTask(created)
      setReviseCompare(null)
      if (aiMode === 'rewrite') setAiMode('generate')
      setChapter((prev) => ({ ...prev, status: 'generating' }))
      void followTask(created)
      return true
    })) ?? false
  }

  /** 手动分析从保存后的正文取资料。 */
  const runAnalyze = async () => {
    if (!canUseAI) return
    await autosave.runSavedAction(async () => {
      try {
        await analysis.start(aiConfigId)
      } catch (err) {
        if (canEditNow()) message.error(errorMessage(err))
      }
    })
  }

  const handleCancel = async () => {
    try {
      await aiTask.cancel()
    } catch (err) {
      message.error(errorMessage(err))
    }
  }

  const confirmGenerate = () => {
    if (skillProblem) { message.warning(skillProblem); return }
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
    return (await autosave.runSavedAction(async () => {
      if (!canEditNow()) return false
      try {
        const current = sceneEditor.controller.getSnapshot().scene
        if (!current || current.id !== currentSceneId) return false
        const updated = await versionApi.restore(pid, chapter.id, v.id, expectedRevision ?? current.content_revision, confirmRichDocumentLoss)
        if (!canEditNow()) return false
        autosave.receiveChapter(updated)
        await sceneEditor.controller.load()
        if (!canEditNow()) return false
        setSelection({ start: 0, end: 0 })
        message.success(`已恢复到 v${v.seq}`)
        saved()
        return true
      } catch (err) {
        if (canEditNow()) message.error(errorMessage(err))
        return false
      }
    })) ?? false
  }

  const handleRewriteApplied = (c: Chapter) => {
    replaceFromServer(c)
    onChanged()
    void refreshProject()
    void refreshAnalysis()
  }

  const handleToggleDone = async () => {
    await autosave.runSavedAction(async () => {
      try {
        const updated = await chapterApi.update(pid, chapter.id, { status: chapter.status === 'done' ? 'draft' : 'done' })
        if (!canEditNow()) return
        autosave.receiveChapter(updated)
        onChanged()
      } catch (err) {
        if (canEditNow()) message.error(errorMessage(err))
      }
    })
  }

  const otherSceneWriting = false
  const stuck = canUseAI && !otherSceneWriting && chapter.status === 'generating' && !aiRunning && attachChecked && !batchWriting
  // 批量正在写本章：正文 / 标题只读、不能保存（后端对生成中章节的正文 PATCH 返回 409，写完后会用服务端正文覆盖本地）
  const batchLocked = otherSceneWriting || batchWriting && chapter.status === 'generating'
  const status = starting ? { color: 'processing', label: '正在创建任务' }
    : aiTask.running ? CHAPTER_STATUS.generating : CHAPTER_STATUS[chapter.status]
  const revising = aiTask.running && aiTask.task?.kind === 'chapter.revise'
  const reviseRunningCopy = reviseRunningPresentation(aiTask.task, autoAnalyze)
  const analysisStatus = ANALYSIS_STATUS[analysis.status]
  const needsConnectionCheck = canEdit && dirty && !autosave.readyToSave && !autosave.verifying && !autosave.disconnected
  const syncState = !canEdit ? 'restricted' : saving ? 'saving' : autosave.reviewNeeded ? 'review'
    : autosave.disconnected && dirty ? 'offline' : autosave.error || needsConnectionCheck ? 'error' : autosave.restored ? 'recovered'
      : dirty ? 'pending' : 'saved'
  const syncNotice = autosave.error ?? sceneEditor.error
    ?? (needsConnectionCheck ? '请核对当前连接与作品权限。' : undefined)
    ?? (autosave.manualDirty ? '场景标题或摘要等待手动保存。' : undefined)
  const retryText = autosave.retryableUnknown ? '重试原保存' : autosave.reviewNeeded ? '核对最新版本'
    : autosave.restored ? '保存恢复稿' : '重试保存'
  const closeDrafts = () => {
    setDraftsOpen(false)
    if (!requestedDraft) return
    setSearchParams((current) => {
      const next = new URLSearchParams(current)
      next.delete('draft')
      return next
    }, { replace: true })
  }
  const exportConflict = async (kind: EditorDraftTarget['kind']) => {
    const record = await autosave.exportCurrent(kind)
    if (!canReadNow()) return
    if (record) downloadJson(record, ['本地草稿', chapter.id, record.target.anchor_scene_id].join('-') + '.json')
    else message.warning('草稿或权限已变化，请核对后再导出。')
  }
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
      <ChapterDatesButton key={`${chapter.id}:${currentSceneId}:${projectScopeVersion}`} pid={pid} chapterId={chapter.id} sceneId={currentSceneId} />
      <ChapterForeshadowButton projectId={pid} chapterId={chapter.id} />
      <Button size="small" disabled={actionLocked} onClick={() => void openSerialization()}>连载与存稿</Button>
      <Button size="small" icon={<HistoryOutlined />} onClick={() => setVersionsOpen(true)}>
        版本历史
      </Button>
      {(role === 'owner' || isCollaborator) && <Button size="small" onClick={() => navigate(`/projects/${pid}/ai-usage?chapter_id=${chapter.id}`)}>本章 AI 用量</Button>}
      <Button size="small" onClick={() => setDraftsOpen(true)}>本地草稿</Button>
      <Button size="small" disabled={!canEdit || (busy || editorLocked || stuck || batchLocked)} onClick={() => void handleToggleDone()}>
        {chapter.status === 'done' ? '标记为草稿' : '标记为完成'}
      </Button>
      <Button
        type="primary"
        size="small"
        icon={<SaveOutlined />}
        loading={saving}
        disabled={!canEdit || (editorLocked || actionLocked || stuck || batchLocked || autosave.composing || !dirty || autosave.reviewNeeded)}
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
      <Typography.Text type="secondary">
        {aiScope === 'scene'
          ? `只更新场景 ${(scene?.sort ?? 0) + 1} 的正文，保留摘要与规划。`
          : '按场景顺序生成，整章统一保存。目标字数是整章总量。'}
        {autoAnalyze && (aiScope === 'chapter' ? '保存后按设置自动分析本章。' : '如需自动分析，请选择「整章保结构」。')}
      </Typography.Text>
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
                onSceneApplied={() => { void sceneEditor.controller.load(); void reloadChapter(); saved() }}
                pid={pid}
                chapterId={chapter.id}
                content={content}
                selection={selection}
                aiConfigId={aiConfigId}
                styleChoice={styleChoice}
                skill={skillChoice}
                // 章节 generating（含挂载后尚未查完活动任务）时后端会 409，一律禁用
                disabled={!scene || !selectionEditable || aiRunning || sectionLocked || saving || actionLocked || chapter.status === 'generating' || hasConfig === false || autosave.reviewNeeded || autosave.restored}
                ensureSaved={ensureSaved}
                runSavedAction={autosave.runSavedAction}
                onLockChange={setRewriteLocked}
                onApplied={handleRewriteApplied}
              />
            ),
          },
        ]}
      />
      {revising && (
        <Alert type="info" showIcon message={reviseRunningCopy.title} description={reviseRunningCopy.description} />
      )}
      {aiTask.running && aiStatus === 'queued' && aiTask.task && (
        <Alert type="info" showIcon message={queueLabel(aiTask.task)} description="任务在后台排队，可离开本页，在任务面板查看进度。" />
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
      {skillProblem && !aiRunning && aiMode !== 'rewrite' && <Typography.Text type="warning" style={{ fontSize: 12 }}>{skillProblem}</Typography.Text>}
      {aiMode === 'rewrite' ? null : starting ? (
        <Button block loading disabled>正在创建任务</Button>
      ) : aiTask.running ? (
        <Button danger block icon={<StopOutlined />} onClick={() => void handleCancel()}>
          {aiStatus === 'queued' ? '取消排队' : outputWords > 0 ? `停止（已输出 ${outputWords} 字）` : '停止生成'}
        </Button>
      ) : aiMode === 'generate' ? (
        <Button
          type="primary"
          block
          icon={<ThunderboltOutlined />}
          disabled={!scene || sceneEditor.loading || autosave.reviewNeeded || autosave.restored || hasConfig === false || stuck || otherSceneWriting || saving || actionLocked || !attachChecked || (aiScope === 'chapter' && !sceneAllocations) || !!skillProblem}
          onClick={confirmGenerate}
        >
          {aiScope === 'scene' ? content.trim() ? '重新生成当前场景' : '生成当前场景正文' : '按现有场景生成整章'}
        </Button>
      ) : (
        <Button
          type="primary"
          block
          icon={<ThunderboltOutlined />}
          disabled={!scene || hasConfig === false || stuck || otherSceneWriting || saving || actionLocked || autosave.reviewNeeded || autosave.restored || !attachChecked || !(aiScope === 'scene' ? content : chapter.content).trim() || !!skillProblem}
          onClick={() => void runAI('polish')}
        >
          {aiScope === 'scene' ? '润色当前场景' : '逐场景润色整章'}
        </Button>
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
              disabled={!canEdit || editorLocked || actionLocked || batchLocked || stuck}
            />
          </div>

          <ScenePlanningPanel
            key={`planning:${scene.id}`}
            projectId={pid}
            scene={scene}
            disabled={editorLocked || saving || actionLocked}
            ensureSaved={ensureSaved}
            runSavedAction={autosave.runSavedAction}
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
          disabled={editorLocked || saving || actionLocked || batchLocked || stuck}
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
      { key: 'notes', label: '笔记', icon: <FormOutlined /> },
    )
    return items
  }, [canUseAI])

  const notesSlot = <div ref={attachNotePanel} style={inspectorTab === 'notes' ? undefined : { display: 'none' }} />

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
          onChange={(k) => setInspectorTab(k as InspectorTab)}
          size="small"
          style={{ marginBottom: -8 }}
          items={inspectorTabItems}
        />
      }
    >
      {inspectorOpen && inspectorTab === 'ai' && aiPanelContent}
      {inspectorOpen && inspectorTab === 'scene' && scenePlanningContent}
      {inspectorOpen && inspectorTab === 'chapter' && chapterSettingsContent}
      {notesSlot}
    </Card>
  )

  return (
    <>
      <div style={splitRow}>
        <div ref={editorPaneRef} style={inspectorOpen && !isMobile ? splitPane(640) : { flex: '1 1 100%', minWidth: 0 }}>
          <Card
            size="small"
            className="chapter-editor-card"
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
                {canUseAI && <Tag color={analysisStatus.color} style={{ cursor: 'pointer' }} onClick={() => setAnalysisOpen(true)}>
                  {analysisStatus.label}
                </Tag>}
                {impactHint && <Link to={impactHref(pid, chapter.id)} title="在一致性检查的「修改影响」页签查看并处理">
                  <Tag color="gold" style={{ cursor: 'pointer', whiteSpace: 'normal', marginInlineEnd: 0 }}>{impactHint}</Tag>
                </Link>}
              </Space>
            }
            extra={toolbarInHeader ? toolbar : undefined}
          >
            {!toolbarInHeader && <div style={{ marginBottom: 12 }}>{toolbar}</div>}
            <EditorSaveStatus
              local={canEdit ? localDraft.local : undefined}
              sync={syncState}
              notice={syncNotice ?? undefined}
              disabled={editorLocked || actionLocked || autosave.verifying || autosave.composing || autosave.disconnected}
              onRetry={canEdit && ['error', 'review', 'recovered'].includes(syncState) ? () => { void autosave.retry() } : undefined}
              retryText={retryText}
            />
            {canEdit && titleState.conflict && (
              <TitleDraftConflict
                local={titleState.value}
                remote={titleState.conflict.title}
                stamp={titleStamp}
                disabled={autosave.unknown || actionLocked || saving || editorLocked}
                onKeepLocal={async () => (await autosave.resolveTitle('keep-local', titleStamp)) === true}
                onUseRemote={async () => (await autosave.resolveTitle('use-remote', titleStamp)) === true}
                onExport={() => { void exportConflict('chapter_title') }}
              />
            )}
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
                description="暂无法安全关联先前的任务。请在任务面板核对执行状态，完成后刷新章节。"
              />
            )}
            <Input
              value={title}
              onChange={(event) => {
                if (titleComposing.current) setCompositionTitle(event.target.value)
                else autosave.changeTitle(event.target.value)
              }}
              onCompositionStart={(event) => {
                titleComposing.current = true
                setCompositionTitle(event.currentTarget.value)
                autosave.setTitleComposing(true)
              }}
              onCompositionEnd={(event) => {
                autosave.changeTitle(event.currentTarget.value)
                titleComposing.current = false
                setCompositionTitle(null)
                autosave.setTitleComposing(false)
              }}
              aria-label="章节标题"
              placeholder="章节标题"
              maxLength={200}
              disabled={!canEdit || !scene || sceneEditor.loading || editorLocked || batchLocked || actionLocked || stuck}
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
            {aiTask.error && !aiRunning && aiMode !== 'rewrite' && (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 16 }}
                message={aiStatus === 'interrupted' ? '任务已中断' : '生成失败'}
                description={aiTask.error}
              />
            )}
            {(starting || (aiTask.running && (!aiTask.task?.scene_id || aiTask.task.scene_id === currentSceneId))) && (
              <section aria-label="AI 实时预览" style={{ marginBottom: 16 }}>
                <Card
                  size="small"
                  title={starting ? '正在创建任务' : aiTask.task?.scene_id ? `场景 ${scene ? scene.sort + 1 : ''}实时预览` : '整章实时预览'}
                  extra={!starting && <Button danger size="small" icon={<StopOutlined />} onClick={() => void handleCancel()}>{aiStatus === 'queued' ? '取消排队' : '停止生成'}</Button>}
                >
                  <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
                    {starting ? '正在确认生成任务，请稍候。' : '只读提案 · 原稿保留在下方，全部生成成功后保存。'}
                  </Typography.Paragraph>
                  {!starting && outputWords > 0 ? (
                    <>
                      <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
                        {aiTask.task?.stage || `已输出 ${outputWords} 字`}
                      </Typography.Paragraph>
                      <StreamOutput text={aiTask.text} running maxHeight={420} />
                    </>
                  ) : (
                    <Space role="status" style={{ padding: '16px 0' }}>
                      <Spin size="small" />
                      <Typography.Text type="secondary">
                        {starting ? '正在创建任务…' : aiTask.task?.stage || (aiStatus === 'queued' && aiTask.task ? queueLabel(aiTask.task) : '等待模型输出…')}
                      </Typography.Text>
                    </Space>
                  )}
                </Card>
              </section>
            )}
            {lastSceneMove && lastSceneMove.scope === projectScopeVersion && <Alert
              type="success"
              showIcon
              closable
              onClose={() => setLastSceneMove(undefined)}
              message={`场景当前位于第 ${lastSceneMove.target.chapter_no} 章 · ${lastSceneMove.target.title || '未命名'}`}
              action={<Button size="small" onClick={() => {
                if (canReadNow()) navigate(`/projects/${pid}/chapters/${lastSceneMove.target.chapter_id}?scene_id=${lastSceneMove.sceneId}`)
              }}>打开目标场景</Button>}
              style={{ marginBottom: 12, overflowWrap: 'anywhere' }}
            />}
            <SceneWritingPanel
              aiConfigId={aiConfigId}
              styleChoice={styleChoice}
              skill={skillChoice}
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
              disabled={editorLocked || batchLocked || saving || actionLocked || stuck}
              editorDisabled={editorLocked || batchLocked || actionLocked || stuck}
              selection={selection}
              onSelection={setSelection}
              ensureSaved={ensureSaved}
              runSavedAction={autosave.runSavedAction}
              conflictActions={{
                pending: autosave.verifying || actionLocked,
                canKeepLocal: !autosave.unknown && sceneEditor.controller.canAcknowledgeLatest(),
                canUseRemote: !autosave.unknown,
                onReview: async () => { await autosave.review() },
                onResolve: async (choice, stamp) => (await autosave.resolveScene(choice, stamp)) === true,
                onExport: () => { void exportConflict('scene_document') },
              }}
              onChanged={() => { void reloadChapter(); saved() }}
              onSelect={selectScene}
              hasModel={hasConfig}
              restructureTaskKey={restructureTaskKey}
              onCloseRestructureTask={closeRestructureTask}
              onSceneMoved={async (target, sceneId) => {
                if (!canReadNow()) return
                setLastSceneMove({ target, sceneId, scope: projectScopeVersion })
                await refreshSceneStructure()
              }}
              onRestructured={(sceneId) => refreshSceneStructure(sceneId, true)}
              mentionAnchor={mentionAnchor}
              markerJump={markerJump}
              onNavigateMarker={async (target: MarkerLocation) => {
                if (rewriteLocked || sectionLocked || structureDirty || aiRunning || saving || actionLocked) {
                  message.warning('请先完成当前场景排序或改写预览。')
                  return false
                }
                const navigateToMarker = () => navigate(`/projects/${pid}/chapters/${target.chapterId}?scene_id=${target.sceneId}`, { state: { markerLocation: target } })
                if (!canEditNow()) { navigateToMarker(); return true }
                return (await autosave.runSavedAction(async () => { navigateToMarker(); return true })) === true
              }}
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
        {/* 收起时隐藏而非卸载，仅笔记栏保持挂载，其余标签内容仍随收起卸载 */}
        {!isMobile && <div style={inspectorOpen ? { flex: '0 0 360px', width: 360, minWidth: 320 } : { display: 'none' }}>{inspectorPanel}</div>}
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
            title={INSPECTOR_TITLES[inspectorTab]}
            extra={
              <Segmented
                size="small"
                value={inspectorTab}
                onChange={(val) => setInspectorTab(val as InspectorTab)}
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
            {notesSlot}
          </Drawer>
        </>
      )}
      {notePanelHost && createPortal(
        <ConfigProvider getPopupContainer={getNotePopupContainer}>
          <NoteSidePanel projectId={pid} onDirtyChange={setNoteDirty} />
        </ConfigProvider>,
        notePanelHost,
      )}
      <DraftRecoveryPanel
        projectId={pid}
        chapterId={chapter.id}
        currentSceneId={currentSceneId}
        initialDraftId={requestedDraft}
        currentDraftIds={localDraft.ids}
        open={draftsOpen || !!requestedDraft}
        disabled={editorLocked || actionLocked || saving || autosave.unknown}
        onClose={closeDrafts}
        onRestore={async (record) => (await autosave.restore(record)) === true}
      />
      <VersionDrawer
        key={`versions:${currentSceneId}`}
        pid={pid}
        chapterId={chapter.id}
        open={versionsOpen}
        onClose={() => setVersionsOpen(false)}
        currentContent={chapter.content}
        currentDirty={false}
        sceneContext={{
          scenes: sceneEditor.page?.items ?? [],
          currentSceneId: scene?.id ?? null,
          prepare: async () => await ensureSaved() ? sceneEditor.controller.getSnapshot().scene?.content_revision ?? null : null,
          onExtract: async (version, preview, confirmRichDocumentLoss) => (await autosave.runSavedAction(async () => {
            try {
              await versionApi.extract(pid, chapter.id, version.id, {
                scene_id: preview.scene_id, expected_content_revision: preview.content_revision,
                signature: preview.signature, confirm_rich_document_loss: confirmRichDocumentLoss,
              })
              if (!canEditNow()) return false
              await sceneEditor.controller.load()
              await reloadChapter()
              if (!canEditNow()) return false
              saved()
              message.success('完整旧稿已提取到指定场景，其他场景不变')
              return true
            } catch (err) {
              if (canEditNow()) message.error(errorMessage(err))
              return false
            }
          })) === true,
        }}
        restoreDisabled={editorLocked || saving || actionLocked || stuck || autosave.reviewNeeded || autosave.restored}
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
        analyzeDisabled={hasConfig === false || aiRunning || stuck || saving || actionLocked || autosave.reviewNeeded || autosave.restored || !chapter.content.trim()}
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
        title={dirty ? '有未保存的修改' : structureDirty ? '场景顺序尚未保存' : (rewriteLocked || sectionLocked) ? '局部改写结果未采纳' : '笔记有未保存修改'}
        okText="离开"
        cancelText="留下"
        okButtonProps={{ danger: true }}
        onOk={async () => {
          await autosave.journal.protectCurrent()
          blocker.proceed?.()
        }}
        onCancel={() => blocker.reset?.()}
      >
        {dirty || autosave.unknown
          ? localDraft.local === 'saved' && !autosave.composing
            ? '本地草稿已保留，联网重新打开后可恢复。'
            : '尚未确认最近的输入已保留。现在离开可能丢失这些修改。'
          : structureDirty ? '离开将丢弃尚未提交的拟排序（服务端顺序不受影响）。确定离开吗？'
            : (rewriteLocked || sectionLocked) ? '离开将丢弃局部改写的预览结果（正文不受影响）。确定离开吗？'
              : '离开将丢弃笔记栏中当前笔记的未保存修改（正文不受影响）。确定离开吗？'}
        {pageGuard && noteDirty && ' 笔记栏中的笔记也有未保存修改，离开后同样会丢失。'}
        {dirty && structureDirty && ' 尚未提交的拟排序会丢失。'}
        {dirty && (rewriteLocked || sectionLocked) && ' 尚未采纳的局部改写预览会丢失。'}
      </Modal>
    </>
  )
}
