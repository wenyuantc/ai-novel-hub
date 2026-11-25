import {
  ArrowLeftOutlined,
  EditOutlined,
  CustomerServiceOutlined,
  LeftOutlined,
  ProfileOutlined,
  SoundOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  StopOutlined,
  RightOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons'
import { Button, ConfigProvider, Drawer, Empty, Result, Segmented, Slider, Space, Spin, theme, Tooltip, Typography } from 'antd'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties, type MutableRefObject, type ReactNode } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { projectApi } from '../api/projects'
import { ApiError, currentRequestGeneration, watchRequestGeneration } from '../api/client'
import { readerApi, saveProgressKeepalive } from '../api/reader'
import type { Project, ReaderTocItem, ReadingPosition } from '../api/types'
import ReaderAnalysisPanel from '../components/reader/ReaderAnalysisPanel'
import PlainReaderContent from '../components/reader/PlainReaderContent'
import ReaderCastingPanel from '../components/reader/ReaderCastingPanel'
import ReaderAICasting from '../components/reader/ReaderAICasting'
import { applyReaderAiCues, applyReaderCastOverrides, useReaderCasting } from '../hooks/useReaderCasting'
import { readerRoleLabel } from '../utils/readerVoices'
import { buildReaderScript, READER_EMOTION_LABELS } from '../utils/readerDialogue'
import { useReadAloud } from '../hooks/useReadAloud'
import './ReaderPage.css'
import RichReaderContent from '../components/reader/RichReaderContent'
import SearchHitContent from '../components/reader/SearchHitContent'
import MentionPreview from '../components/mentions/MentionPreview'
import { readMentionAnchor } from '../utils/mentions'
import ProjectManagementNotice from '../components/project/ProjectManagementNotice'
import { useAuthStore } from '../stores/auth'
import { isProjectAccess, projectAccess } from '../utils/projectAccess'
import { authorizationKey } from '../utils/permissions'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'
import { READER_TONES, type ReaderTone } from '../theme/palette'
import { useResolvedTheme } from '../theme/themeStore'
import {
  arrowDirection,
  groupToc,
  READER_BACKGROUNDS,
  READER_FONT_SIZE,
  READER_FONTS,
  READER_LINE_HEIGHTS,
  READER_WIDTHS,
  readerTone,
  readReaderSettings,
  restoreRatio,
  scrollRatio,
  scrollTopForRatio,
  readerParagraphs,
  READER_SPEECH_RATES,
  startChapterId,
  tocNeighbors,
  writeReaderSettings,
  type ReaderBackground,
  type ReaderFont,
  type ReaderSettings,
} from '../utils/reader'

/** 滚动停止多久后上报进度 */
const REPORT_DELAY_MS = 2000

interface ReaderData {
  project: Project
  toc: ReaderTocItem[]
}

interface ReaderSessionScope {
  actorId: number
  generation: number
  authorization: string
}

const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

function isCurrentSession(scope: ReaderSessionScope): boolean {
  const auth = useAuthStore.getState()
  return auth.status === 'authenticated' && auth.user?.id === scope.actorId
    && authorizationKey(auth.user) === scope.authorization && currentRequestGeneration() === scope.generation
}

class ReaderAccessChanged extends Error {
  constructor() { super('作品访问权限已变化，请重新打开阅读器') }
}

function sameReaderAccess(before: Project, current: Project, user: ReturnType<typeof useAuthStore.getState>['user']): boolean {
  const first = before.access
  const next = current.access
  return current.id === before.id && current.owner?.id === before.owner?.id && isProjectAccess(first)
    && isProjectAccess(next) && projectAccess(current, user, before.id).canRead
    && first.role === next.role && first.is_collaborator === next.is_collaborator
    && first.authorization_version === next.authorization_version && first.can_edit_content === next.can_edit_content
    && first.can_use_ai === next.can_use_ai && first.can_write_reading_progress === next.can_write_reading_progress
}

/** 子资源失败只能提示复查；只有权威作品明确失权或授权轮次变化才关闭阅读会话。 */
async function readerAccessLostAfterFailure(pid: number, expected: Project | null,
  user: ReturnType<typeof useAuthStore.getState>['user'], current: () => boolean): Promise<boolean> {
  try {
    const project = await projectApi.get(pid)
    return current() && (!projectAccess(project, user, pid).canRead || (expected !== null && !sameReaderAccess(expected, project, user)))
  } catch (error) {
    return current() && error instanceof ApiError && (error.status === 403 || error.status === 404)
  }
}

/** 阅读器不使用项目 store；正文落地前独立核对服务器的当前协作轮次。 */
async function verifyReaderAccess(project: Project, user: ReturnType<typeof useAuthStore.getState>['user']): Promise<Project> {
  const before = project.access
  if (!isProjectAccess(before) || !projectAccess(project, user, project.id).canRead) throw new ReaderAccessChanged()
  // 作者的作品归属不会通过邀请更换；其账号和会话仍由调用方即时校验。
  if (before.role === 'owner') return project
  const current = await projectApi.get(project.id)
  if (!sameReaderAccess(project, current, user)) throw new ReaderAccessChanged()
  return current
}

/**
 * 阅读器（/projects/:pid/read/:cid，全屏、在 ProjectLayout 之外）：章内滚动、章末上一章 / 下一章、←/→ 切章、按卷分组的目录、
 * 阅读设置（localStorage `ainovel.reader`）、阅读进度上报服务端、本章分析侧栏、跳到编辑器。
 * 不带 :cid 时按阅读进度（没有则第一个已写章节）定位。
 */
export default function ReaderPage() {
  const { pid } = useParams()
  const status = useAuthStore((s) => s.status)
  const user = useAuthStore((s) => s.user)
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  if (status !== 'authenticated' || !user) return <Result status="403" title="当前账号无法继续读取该作品" />
  const authorization = authorizationKey(user)
  const scope = { actorId: user.id, generation, authorization }
  // 每次会话、账户、权限或作品变更时销毁旧目录、正文、进度与所有阅读器 effect。
  return <ReaderSession key={`${pid}:${user.id}:${authorization}:${generation}`} scope={scope} />
}

function ReaderSession({ scope }: { scope: ReaderSessionScope }) {
  const params = useParams()
  const pid = Number(params.pid)
  const cid = params.cid === undefined ? null : Number(params.cid)
  const navigate = useNavigate()
  const location = useLocation()
  const user = useAuthStore((s) => s.user)
  const resolved = useResolvedTheme()
  const [settings, setSettings] = useState<ReaderSettings>(readReaderSettings)
  const tone = readerTone(settings.background, resolved)
  const colors = READER_TONES[tone]

  useEffect(() => writeReaderSettings(settings), [settings])

  // 最近一次读到的位置：初始取服务端，之后随本页上报更新（切回读过的章节时据此恢复）
  const readingRef = useRef<ReadingPosition | null>(null)
  const readerProjectRef = useRef<Project | null>(null)
  const loadingSeq = useRef(0)
  const sessionActive = useRef(true)
  const sessionCurrent = useCallback(() => sessionActive.current && isCurrentSession(scope), [scope])
  const [accessLost, setAccessLost] = useState(false)
  const accessLostNow = useCallback(() => {
    if (!sessionCurrent()) return
    sessionActive.current = false
    readingRef.current = null
    readerProjectRef.current = null
    setAccessLost(true)
  }, [sessionCurrent])
  const loader = useCallback(async (): Promise<ReaderData> => {
    const seq = ++loadingSeq.current
    try {
      const [project, toc] = await Promise.all([projectApi.get(pid), readerApi.toc(pid)])
      if (seq !== loadingSeq.current || !sessionCurrent()) throw new ReaderAccessChanged()
      const verified = await verifyReaderAccess(project, user)
      if (seq !== loadingSeq.current || !sessionCurrent()) throw new ReaderAccessChanged()
      readerProjectRef.current = verified
      readingRef.current = projectAccess(verified, user, pid).canWriteReadingProgress ? verified.reading : null
      return { project: verified, toc }
    } catch (err) {
      const current = () => seq === loadingSeq.current && sessionCurrent()
      if (current() && (err instanceof ReaderAccessChanged || (err instanceof ApiError && [403, 404].includes(err.status)
        && await readerAccessLostAfterFailure(pid, readerProjectRef.current, user, current)))) accessLostNow()
      throw err
    }
  }, [pid, user, sessionCurrent, accessLostNow])
  const { data, loading, error, reload } = useLoader(loader)

  const currentData = data?.project.id === pid ? data : undefined
  // 仅作者使用自己的阅读进度；协作者与管理员均从首个已写章节开始。
  const access = projectAccess(currentData?.project, user, pid)
  const start = currentData && access.canRead && !accessLost && isCurrentSession(scope)
    ? startChapterId(currentData.toc, access.canWriteReadingProgress ? currentData.project.reading : null) : null
  useEffect(() => {
    if (cid === null && start !== null && sessionCurrent()) navigate(`/projects/${pid}/read/${start}`, { replace: true, state: location.state })
  }, [cid, start, pid, navigate, location.state, sessionCurrent])

  const wrap = (node: ReactNode) => (
    <ConfigProvider theme={{ algorithm: colors.dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
      <div style={{ minHeight: '100vh', background: colors.bg, color: colors.text }}>{node}</div>
    </ConfigProvider>
  )

  if (!Number.isFinite(pid) || (cid !== null && !Number.isFinite(cid))) {
    return wrap(<Result status="404" title="页面不存在" extra={<Link to="/projects">返回书架</Link>} />)
  }
  if (accessLost) return wrap(<Result status="warning" title="无法继续读取此作品" extra={<Link to="/projects">返回书架</Link>} />)
  if (error) {
    return wrap(<Result status="warning" title="无法打开阅读器" subTitle={error}
      extra={<Space><Button onClick={reload}>重试读取</Button><Link to="/projects">返回书架</Link></Space>} />)
  }
  if (!currentData || loading) {
    return wrap(
      <div style={{ padding: 64, textAlign: 'center' }}>
        <Spin />
      </div>,
    )
  }
  if (!access.canRead || !isCurrentSession(scope)) {
    return wrap(<Result status="403" title="当前账号无法继续读取该作品" extra={<Link to="/projects">返回书架</Link>} />)
  }
  if (cid === null) {
    if (start !== null) return wrap(null)
    return wrap(
      <Result
        status="info"
        title="还没有写好正文的章节"
        extra={
          <Space>
            <Link to="/projects">返回书架</Link>
            {access.canEdit && <Link to={`/projects/${pid}/chapters`}>去写作</Link>}
          </Space>
        }
      />,
    )
  }
  return wrap(
    <ReaderView
      key={cid}
      project={currentData.project}
      actorId={scope.actorId}
      toc={currentData.toc}
      chapterId={cid}
      settings={settings}
      onSettings={setSettings}
      tone={tone}
      readingRef={readingRef}
      sessionCurrent={sessionCurrent}
      onAccessLost={accessLostNow}
    />,
  )
}

interface ViewProps {
  actorId: number
  project: Project
  toc: ReaderTocItem[]
  chapterId: number
  settings: ReaderSettings
  onSettings: (s: ReaderSettings) => void
  tone: ReaderTone
  readingRef: MutableRefObject<ReadingPosition | null>
  sessionCurrent: () => boolean
  onAccessLost: () => void
}

function ReaderView({ project, actorId, toc, chapterId, settings, onSettings, tone, readingRef, sessionCurrent, onAccessLost }: ViewProps) {
  const pid = project.id
  const user = useAuthStore((s) => s.user)
  const { managingOther, isCollaborator, canUseAI, canEdit, canWriteReadingProgress } = projectAccess(project, user, pid)
  const administeringOther = managingOther && !isCollaborator
  const navigate = useNavigate()
  const location = useLocation()
  const mentionAnchor = useMemo(() => readMentionAnchor(new URLSearchParams(location.search)), [location.search])
  const searchAnchor = mentionAnchor?.origin === 'book-search' ? mentionAnchor : null
  const [showMentions, setShowMentions] = useState(false)
  const [dismissedAnchor, setDismissedAnchor] = useState('')
  const mentionsVisible = showMentions || (!!mentionAnchor && !searchAnchor && dismissedAnchor !== location.search)
  const isMobile = useIsMobile()
  const { token } = theme.useToken()
  const colors = READER_TONES[tone]
  const [tocOpen, setTocOpen] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [castingOpen, setCastingOpen] = useState(false)
  const alive = useRef(true)
  const chapterLoadSeq = useRef(0)
  useLayoutEffect(() => () => { chapterLoadSeq.current += 1 }, [chapterId, project.id, project.owner?.id,
    project.access.role, project.access.authorization_version, project.access.is_collaborator,
    project.access.can_read, project.access.can_edit_content, project.access.can_use_ai, project.access.can_write_reading_progress])
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  const loader = useCallback(async () => {
    const seq = ++chapterLoadSeq.current
    const current = () => seq === chapterLoadSeq.current && alive.current && sessionCurrent()
    try {
      const result = await readerApi.chapter(pid, chapterId)
      if (!current()) throw new ReaderAccessChanged()
      await verifyReaderAccess(project, user)
      if (!current()) throw new ReaderAccessChanged()
      return result
    } catch (err) {
      if (current() && (err instanceof ReaderAccessChanged || (err instanceof ApiError && [403, 404].includes(err.status)
        && await readerAccessLostAfterFailure(pid, project, user, current)))) onAccessLost()
      throw err
    }
  }, [pid, chapterId, onAccessLost, project, user, sessionCurrent])
  const { data: readerChapter, loading, error, reload } = useLoader(loader)
  const chapter = readerChapter?.chapter.id === chapterId ? readerChapter.chapter : undefined
  const written = !!chapter?.content.trim()
  const blocks = readerChapter?.blocks ?? null
  const paragraphs = useMemo(() => readerParagraphs(error ? '' : chapter?.content ?? '', blocks), [error, chapter?.content, blocks])
  const snapshot = chapter && !error ? `${chapter.id}:${readerChapter?.content_hash}` : ''
  const casting = useReaderCasting(actorId, pid, chapterId, chapter && !error ? readerChapter?.content_hash ?? '' : '')
  const localScript = useMemo(() => buildReaderScript(paragraphs), [paragraphs])
  const dialogueCues = useMemo(() => localScript.filter((cue) => cue.kind === 'dialogue'), [localScript])
  const detected = useMemo(() => applyReaderAiCues(localScript, casting.aiResult), [localScript, casting.aiResult])
  const script = useMemo(() => applyReaderCastOverrides(detected, casting.overrides), [detected, casting.overrides])
  const speech = useReadAloud(paragraphs, snapshot, settings.speechRate, sessionCurrent, { script, roleVoices: casting.effectiveVoices, expressive: true })
  const speaking = speech.status !== 'idle'
  const paused = speech.status === 'paused'
  const speechLabel = !speaking ? '朗读' : paused ? '继续' : '暂停'
  const { prev, next } = useMemo(
    () => (chapter ? tocNeighbors(toc, chapter.chapter_no) : { prev: null, next: null }),
    [toc, chapter],
  )

  const from = (location.state as { from?: string } | null)?.from ?? (searchAnchor ? `/projects/${pid}/book-search` : undefined)
  const goChapter = useCallback(
    (id: number) => navigate(`/projects/${pid}/read/${id}`, { replace: true, state: location.state }),
    [navigate, pid, location.state],
  )

  // ---------------- 进度：恢复、节流计算、停 2 秒上报、隐藏 / 卸载时 keepalive 上报 ----------------
  const pendingRef = useRef<{ ratio: number; dirty: boolean }>({ ratio: 0, dirty: false })
  const timerRef = useRef<number | null>(null)
  const report = useCallback(
    (keepalive: boolean) => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current)
        timerRef.current = null
      }
      const p = pendingRef.current
      if (!canWriteReadingProgress || !sessionCurrent()) {
        p.dirty = false
        readingRef.current = null
        return
      }
      if (!p.dirty || !chapter) return
      p.dirty = false
      const body = { chapter_id: chapter.id, ratio: p.ratio }
      readingRef.current = { chapter_id: chapter.id, chapter_no: chapter.chapter_no, ratio: p.ratio }
      if (keepalive) saveProgressKeepalive(pid, body)
      else readerApi.saveProgress(pid, body).catch(() => {
        // 进度上报失败不打扰阅读；下次滚动会再报
      })
    },
    [chapter, pid, readingRef, canWriteReadingProgress, sessionCurrent],
  )
  const schedule = useCallback(
    (ratio: number) => {
      if (!canWriteReadingProgress || !sessionCurrent()) return
      pendingRef.current = { ratio, dirty: true }
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = window.setTimeout(() => report(false), REPORT_DELAY_MS)
    },
    [report, canWriteReadingProgress, sessionCurrent],
  )

  // 正文渲染后：只有这一章就是上次读到的章节才恢复位置，否则回到顶部；空章不记进度
  useEffect(() => {
    if (!chapter || mentionAnchor) return
    const ratio = canWriteReadingProgress && sessionCurrent() && written ? restoreRatio(chapter.id, readingRef.current) : 0
    const raf = window.requestAnimationFrame(() => {
      const el = document.documentElement
      window.scrollTo(0, scrollTopForRatio(ratio, el.scrollHeight, window.innerHeight))
      if (written) schedule(ratio)
    })
    return () => window.cancelAnimationFrame(raf)
  }, [chapter, written, schedule, readingRef, canWriteReadingProgress, sessionCurrent, mentionAnchor])

  useEffect(() => {
    if (!written || !canWriteReadingProgress || !sessionCurrent()) return
    let frame = 0
    const onScroll = () => {
      if (frame) return
      frame = window.requestAnimationFrame(() => {
        frame = 0
        const el = document.documentElement
        schedule(scrollRatio(window.scrollY, el.scrollHeight, window.innerHeight))
      })
    }
    const onHide = () => {
      if (document.visibilityState === 'hidden') report(true)
    }
    const onPageHide = () => report(true)
    window.addEventListener('scroll', onScroll, { passive: true })
    document.addEventListener('visibilitychange', onHide)
    window.addEventListener('pagehide', onPageHide)
    return () => {
      if (frame) window.cancelAnimationFrame(frame)
      window.removeEventListener('scroll', onScroll)
      document.removeEventListener('visibilitychange', onHide)
      window.removeEventListener('pagehide', onPageHide)
      report(true) // 切章 / 离开阅读器
    }
  }, [written, schedule, report, canWriteReadingProgress, sessionCurrent])

  // ←/→ 切章（抽屉打开、焦点在输入控件时不响应）
  const anyDrawer = tocOpen || panelOpen || settingsOpen || castingOpen
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (anyDrawer) return
      const dir = arrowDirection({
        key: e.key,
        altKey: e.altKey,
        ctrlKey: e.ctrlKey,
        metaKey: e.metaKey,
        shiftKey: e.shiftKey,
        target: e.target as HTMLElement | null,
      })
      if (dir === 'prev' && prev) goChapter(prev.id)
      else if (dir === 'next' && next) goChapter(next.id)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [anyDrawer, prev, next, goChapter])

  const barStyle: CSSProperties = {
    position: 'sticky',
    top: 0,
    zIndex: 10,
    display: 'flex',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: 4,
    padding: isMobile ? '6px 8px' : '8px 16px',
    background: colors.bg,
    borderBottom: `1px solid ${token.colorBorderSecondary}`,
  }
  const articleStyle: CSSProperties = {
    maxWidth: settings.width,
    margin: '0 auto',
    padding: isMobile ? '20px 16px 48px' : '32px 24px 64px',
    fontFamily: READER_FONTS[settings.font].stack,
    fontSize: settings.fontSize,
    lineHeight: settings.lineHeight,
    color: colors.text,
  }
  const sideDrawer = (placement: 'left' | 'right') =>
    isMobile ? { placement: 'bottom' as const, height: '72vh' } : { placement, width: placement === 'left' ? 320 : 380 }

  const chapterTitle = chapter ? `第 ${chapter.chapter_no} 章${chapter.title ? ` ${chapter.title}` : ''}` : ''

  return (
    <>
      <div style={barStyle}>
        <Tooltip title="返回">
          <Button type="text" icon={<ArrowLeftOutlined />} aria-label="返回" onClick={() => navigate(from ?? (administeringOther ? '/admin/projects' : '/projects'))} />
        </Tooltip>
        <div style={{ flex: 1, minWidth: 0, lineHeight: 1.3 }}>
          <Typography.Text strong ellipsis style={{ display: 'block', color: colors.text }}>
            {project.title}
          </Typography.Text>
          {!isMobile && (
            <Typography.Text ellipsis style={{ display: 'block', fontSize: 12, color: colors.secondary }}>
              {chapterTitle}
            </Typography.Text>
          )}
        </div>
        <div className="reader-speech-controls" data-open={speaking || undefined} style={{ colorScheme: colors.dark ? 'dark' : 'light' }}>
          <Tooltip title={isMobile && !speaking ? speechLabel : undefined}>
            <Button type="text" icon={!speaking ? <SoundOutlined /> : paused ? <PlayCircleOutlined /> : <PauseCircleOutlined />}
              aria-label={speechLabel} onClick={!speaking ? speech.start : paused ? speech.resume : speech.pause} disabled={!paragraphs.length}>
              {(!isMobile || speaking) && speechLabel}
            </Button>
          </Tooltip>
          {speaking && <>
            {speech.activeRole && <span className="reader-speech-current" title={readerRoleLabel(speech.activeRole)}>
              {readerRoleLabel(speech.activeRole)}{speech.activeEmotion && speech.activeEmotion !== 'neutral' ? ` · ${READER_EMOTION_LABELS[speech.activeEmotion]}` : ''}
            </span>}
            <Button type="text" icon={<StopOutlined />} onClick={speech.stop}>停止</Button>
            <label htmlFor="reader-speech-rate">语速</label>
            <select className="reader-speech-select" id="reader-speech-rate" value={settings.speechRate}
              onChange={(e) => onSettings({ ...settings, speechRate: Number(e.target.value) })}>
              {READER_SPEECH_RATES.map((rate) => <option key={rate} value={rate}>{rate} 倍</option>)}
            </select>
          </>}
        </div>
        <Tooltip title="角色配音">
          <Button type="text" icon={<CustomerServiceOutlined />} aria-label="角色配音" disabled={!paragraphs.length}
            onClick={() => setCastingOpen(true)}>{!isMobile && '配音'}</Button>
        </Tooltip>
        <Tooltip title="目录">
          <Button type="text" icon={<UnorderedListOutlined />} aria-label="目录" onClick={() => setTocOpen(true)} />
        </Tooltip>
        <Button type={mentionsVisible ? 'primary' : 'text'} aria-pressed={mentionsVisible} onClick={() => { setShowMentions(!mentionsVisible); setDismissedAnchor(mentionsVisible ? location.search : '') }}>提及</Button>
        {canUseAI && <Tooltip title="本章分析">
          <Button type="text" icon={<ProfileOutlined />} aria-label="本章分析" onClick={() => setPanelOpen(true)} />
        </Tooltip>}
        <Tooltip title="阅读设置">
          <Button type="text" icon={<SettingOutlined />} aria-label="阅读设置" onClick={() => setSettingsOpen(true)} />
        </Tooltip>
        <Tooltip title={isMobile ? (canEdit ? '编辑本章' : '查看本章') : undefined}>
          <Button
            type="text"
            icon={<EditOutlined />}
            aria-label={canEdit ? '编辑本章' : '查看本章'}
            onClick={() => navigate(`/projects/${pid}/chapters/${chapterId}`)}
          >
            {!isMobile && (canEdit ? '编辑本章' : '查看本章')}
          </Button>
        </Tooltip>
        <div role="status" className="reader-speech-message">{speech.message ?? (speech.status === 'waiting' && speech.activeStart === null ? '正在等待本机语音…' : '')}</div>
      </div>

      {administeringOther && project.owner && <div style={{ padding: '12px 16px 0' }}><ProjectManagementNotice owner={project.owner} /></div>}
      <article style={articleStyle}>
        {loading && !chapter ? (
          <div style={{ padding: 48, textAlign: 'center' }}>
            <Spin />
          </div>
        ) : error || !chapter ? (
          <Result status="warning" title="章节不存在或无法读取" subTitle={error ?? undefined}
            extra={searchAnchor && <Space><Button onClick={reload}>重试读取</Button><Link to={`/projects/${pid}/book-search`}>返回全书搜索</Link></Space>} />
        ) : (
          <>
            <h1 style={{ fontSize: Math.round(settings.fontSize * 1.4), lineHeight: 1.4, textAlign: 'center', margin: '0 0 1.2em' }}>
              {chapterTitle}
            </h1>
            {written && mentionsVisible ? <MentionPreview projectId={pid} text={chapter.content} anchor={searchAnchor ? null : mentionAnchor}
              renderText={(spans, focus) => blocks
                ? <RichReaderContent projectId={pid} text={chapter.content} blocks={blocks} mentions={{ spans, focus }} activeStart={speech.activeStart} />
                : <PlainReaderContent projectId={pid} text={chapter.content} paragraphs={paragraphs} mentions={{ spans, focus }} activeStart={speech.activeStart} />} />
              : searchAnchor && readerChapter ? <SearchHitContent projectId={pid} text={chapter.content} blocks={blocks}
                contentHash={readerChapter.content_hash} anchor={searchAnchor} paragraphs={paragraphs} activeStart={speech.activeStart} /> : written ? (
              blocks ? <RichReaderContent projectId={pid} text={chapter.content} blocks={blocks} activeStart={speech.activeStart} />
                : <PlainReaderContent projectId={pid} text={chapter.content} paragraphs={paragraphs} activeStart={speech.activeStart} />
            ) : (
              <Empty description="本章还没有正文">
                {canEdit && <Link to={`/projects/${pid}/chapters/${chapterId}`}>去写作</Link>}
              </Empty>
            )}
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                gap: 12,
                marginTop: 48,
                fontFamily: 'inherit',
                fontSize: 14,
                lineHeight: 1.5,
              }}
            >
              <Button icon={<LeftOutlined />} disabled={!prev} onClick={() => prev && goChapter(prev.id)}>
                上一章
              </Button>
              <Button type="text" onClick={() => setTocOpen(true)}>
                目录
              </Button>
              <Button disabled={!next} onClick={() => next && goChapter(next.id)}>
                {next ? '下一章' : '已是最后一章'} {next && <RightOutlined />}
              </Button>
            </div>
          </>
        )}
      </article>

      <Drawer title="目录" open={tocOpen} onClose={() => setTocOpen(false)} {...sideDrawer('left')}>
        <TocList toc={toc} currentId={chapterId} onSelect={(id) => {
          setTocOpen(false)
          if (id !== chapterId) goChapter(id)
        }} />
      </Drawer>
      {canUseAI && <Drawer title="本章分析" open={panelOpen} onClose={() => setPanelOpen(false)} mask={isMobile} destroyOnHidden {...sideDrawer('right')}>
        <ReaderAnalysisPanel projectId={pid} chapterId={chapterId} />
      </Drawer>}
      <Drawer title="角色配音" open={castingOpen} onClose={() => {
        if (speech.previewing) speech.stop()
        setCastingOpen(false)
      }} {...sideDrawer('right')}
        width={isMobile ? undefined : 460} destroyOnHidden>
        {castingOpen && chapter && !error && <ReaderCastingPanel key={snapshot} casting={casting} detected={detected} script={script} player={speech}>
          {canUseAI && readerChapter && <ReaderAICasting project={project} chapterId={chapterId} contentHash={readerChapter.content_hash}
            hasText={written} cues={dialogueCues} voices={speech.voices} sessionCurrent={sessionCurrent} saveResult={casting.saveAiResult} reload={reload}
            recheckAccess={async (current) => {
              if (await readerAccessLostAfterFailure(pid, project, user, current)) onAccessLost()
            }} />}
        </ReaderCastingPanel>}
      </Drawer>
      <Drawer title="阅读设置" open={settingsOpen} onClose={() => setSettingsOpen(false)} {...sideDrawer('right')} height={isMobile ? 'auto' : undefined}>
        <SettingsForm value={settings} onChange={onSettings} />
      </Drawer>
    </>
  )
}

/** 目录：按卷分组，当前章高亮并在打开时滚动到可见处。 */
function TocList({ toc, currentId, onSelect }: { toc: ReaderTocItem[]; currentId: number; onSelect: (id: number) => void }) {
  const { token } = theme.useToken()
  const groups = useMemo(() => groupToc(toc), [toc])
  const currentRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    currentRef.current?.scrollIntoView({ block: 'center' })
  }, [])
  if (toc.length === 0) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有写好正文的章节" />
  return (
    <div>
      {groups.map((g, gi) => (
        <div key={gi} style={{ marginBottom: 12 }}>
          {g.label && (
            <Typography.Text type="secondary" strong style={{ display: 'block', padding: '4px 8px' }}>
              {g.label}
            </Typography.Text>
          )}
          {g.items.map((t) => {
            const current = t.id === currentId
            return (
              <div
                key={t.id}
                ref={current ? currentRef : undefined}
                role="button"
                tabIndex={0}
                onClick={() => onSelect(t.id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') onSelect(t.id)
                }}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 8,
                  padding: '6px 8px',
                  borderRadius: token.borderRadius,
                  cursor: 'pointer',
                  background: current ? token.colorPrimaryBg : undefined,
                  color: current ? token.colorPrimary : undefined,
                  fontWeight: current ? 600 : undefined,
                }}
              >
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  第 {t.chapter_no} 章{t.title ? ` ${t.title}` : ''}
                </span>
                <Typography.Text type="secondary" style={{ fontSize: 12, flex: 'none' }}>
                  {t.word_count} 字
                </Typography.Text>
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}

/** 阅读设置：字号、行距、版心宽度、背景、字体（全局生效）。 */
function SettingsForm({ value, onChange }: { value: ReaderSettings; onChange: (s: ReaderSettings) => void }) {
  const set = <K extends keyof ReaderSettings>(key: K, v: ReaderSettings[K]) => onChange({ ...value, [key]: v })
  const swatch = (bg: ReaderBackground) =>
    bg === 'theme' ? null : (
      <span
        style={{
          display: 'inline-block',
          width: 10,
          height: 10,
          borderRadius: '50%',
          marginRight: 4,
          verticalAlign: 'middle',
          background: READER_TONES[bg].bg,
          border: `1px solid ${READER_TONES[bg].secondary}`,
        }}
      />
    )
  const row = (label: string, control: ReactNode) => (
    <div style={{ marginBottom: 20 }}>
      <Typography.Text type="secondary" style={{ display: 'block', marginBottom: 6 }}>
        {label}
      </Typography.Text>
      {control}
    </div>
  )
  return (
    <div>
      {row(
        `字号 ${value.fontSize}px`,
        <Space.Compact block>
          <Button disabled={value.fontSize <= READER_FONT_SIZE.min} onClick={() => set('fontSize', value.fontSize - 1)}>
            A-
          </Button>
          <div style={{ flex: 1, padding: '0 12px' }}>
            <Slider min={READER_FONT_SIZE.min} max={READER_FONT_SIZE.max} step={1} value={value.fontSize} onChange={(v) => set('fontSize', v)} />
          </div>
          <Button disabled={value.fontSize >= READER_FONT_SIZE.max} onClick={() => set('fontSize', value.fontSize + 1)}>
            A+
          </Button>
        </Space.Compact>,
      )}
      {row(
        '朗读语速',
        <Segmented<number> block aria-label="朗读语速" value={value.speechRate}
          onChange={(v) => set('speechRate', v)} options={READER_SPEECH_RATES.map((v) => ({ value: v, label: `${v}×` }))} />,
      )}
      {row(
        '行距',
        <Segmented<number>
          block
          value={value.lineHeight}
          onChange={(v) => set('lineHeight', v)}
          options={READER_LINE_HEIGHTS.map((v) => ({ value: v, label: v.toFixed(1) }))}
        />,
      )}
      {row(
        '版心宽度',
        <Segmented<number>
          block
          value={value.width}
          onChange={(v) => set('width', v)}
          options={READER_WIDTHS.map((v, i) => ({ value: v, label: ['窄', '中', '宽'][i] ?? String(v) }))}
        />,
      )}
      {row(
        '背景',
        <Segmented<ReaderBackground>
          block
          value={value.background}
          onChange={(v) => set('background', v)}
          options={READER_BACKGROUNDS.map((b) => ({
            value: b.value,
            label: (
              <span>
                {swatch(b.value)}
                {b.label}
              </span>
            ),
          }))}
        />,
      )}
      {row(
        '字体',
        <Segmented<ReaderFont>
          block
          value={value.font}
          onChange={(v) => set('font', v)}
          options={(Object.keys(READER_FONTS) as ReaderFont[]).map((f) => ({
            value: f,
            label: <span style={{ fontFamily: READER_FONTS[f].stack }}>{READER_FONTS[f].label}</span>,
          }))}
        />,
      )}
    </div>
  )
}
