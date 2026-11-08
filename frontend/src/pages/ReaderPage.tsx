import {
  ArrowLeftOutlined,
  EditOutlined,
  LeftOutlined,
  ProfileOutlined,
  RightOutlined,
  SettingOutlined,
  UnorderedListOutlined,
} from '@ant-design/icons'
import { Button, ConfigProvider, Drawer, Empty, Result, Segmented, Slider, Space, Spin, theme, Tooltip, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type MutableRefObject, type ReactNode } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { projectApi } from '../api/projects'
import { readerApi, saveProgressKeepalive } from '../api/reader'
import type { Project, ReaderTocItem, ReadingPosition } from '../api/types'
import ReaderAnalysisPanel from '../components/reader/ReaderAnalysisPanel'
import RichReaderContent from '../components/reader/RichReaderContent'
import MentionPreview from '../components/mentions/MentionPreview'
import { readMentionAnchor } from '../utils/mentions'
import ProjectManagementNotice from '../components/project/ProjectManagementNotice'
import { useAuthStore } from '../stores/auth'
import { projectAccess } from '../utils/projectAccess'
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
  splitParagraphs,
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

/**
 * 阅读器（/projects/:pid/read/:cid，全屏、在 ProjectLayout 之外）：章内滚动、章末上一章 / 下一章、←/→ 切章、按卷分组的目录、
 * 阅读设置（localStorage `ainovel.reader`）、阅读进度上报服务端、本章分析侧栏、跳到编辑器。
 * 不带 :cid 时按阅读进度（没有则第一个已写章节）定位。
 */
export default function ReaderPage() {
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
  const loader = useCallback(async (): Promise<ReaderData> => {
    const [project, toc] = await Promise.all([projectApi.get(pid), readerApi.toc(pid)])
    readingRef.current = projectAccess(project, user, pid).managingOther ? null : project.reading
    return { project, toc }
  }, [pid, user])
  const { data, loading, error } = useLoader(loader)

  const currentData = data?.project.id === pid ? data : undefined
  // 管理员阅读他人项目不使用所有者的阅读进度。
  const start = currentData ? startChapterId(currentData.toc, projectAccess(currentData.project, user, pid).managingOther ? null : currentData.project.reading) : null
  useEffect(() => {
    if (cid === null && start !== null) navigate(`/projects/${pid}/read/${start}`, { replace: true, state: location.state })
  }, [cid, start, pid, navigate, location.state])

  const wrap = (node: ReactNode) => (
    <ConfigProvider theme={{ algorithm: colors.dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}>
      <div style={{ minHeight: '100vh', background: colors.bg, color: colors.text }}>{node}</div>
    </ConfigProvider>
  )

  if (!Number.isFinite(pid) || (cid !== null && !Number.isFinite(cid))) {
    return wrap(<Result status="404" title="页面不存在" extra={<Link to="/projects">返回书架</Link>} />)
  }
  if (error) {
    return wrap(<Result status="warning" title="无法打开阅读器" subTitle={error} extra={<Link to="/projects">返回书架</Link>} />)
  }
  if (!currentData || loading) {
    return wrap(
      <div style={{ padding: 64, textAlign: 'center' }}>
        <Spin />
      </div>,
    )
  }
  if (!projectAccess(currentData.project, user, pid).canRead) {
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
            <Link to={`/projects/${pid}/chapters`}>去写作</Link>
          </Space>
        }
      />,
    )
  }
  return wrap(
    <ReaderView
      key={cid}
      project={currentData.project}
      toc={currentData.toc}
      chapterId={cid}
      settings={settings}
      onSettings={setSettings}
      tone={tone}
      readingRef={readingRef}
    />,
  )
}

interface ViewProps {
  project: Project
  toc: ReaderTocItem[]
  chapterId: number
  settings: ReaderSettings
  onSettings: (s: ReaderSettings) => void
  tone: ReaderTone
  readingRef: MutableRefObject<ReadingPosition | null>
}

function ReaderView({ project, toc, chapterId, settings, onSettings, tone, readingRef }: ViewProps) {
  const pid = project.id
  const user = useAuthStore((s) => s.user)
  const { managingOther, canUseAI } = projectAccess(project, user, pid)
  const navigate = useNavigate()
  const location = useLocation()
  const mentionAnchor = useMemo(() => readMentionAnchor(new URLSearchParams(location.search)), [location.search])
  const [showMentions, setShowMentions] = useState(false)
  const [dismissedAnchor, setDismissedAnchor] = useState('')
  const mentionsVisible = showMentions || (!!mentionAnchor && dismissedAnchor !== location.search)
  const isMobile = useIsMobile()
  const { token } = theme.useToken()
  const colors = READER_TONES[tone]
  const [tocOpen, setTocOpen] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const loader = useCallback(() => readerApi.chapter(pid, chapterId), [pid, chapterId])
  const { data: readerChapter, loading, error } = useLoader(loader)
  const chapter = readerChapter?.chapter
  const written = !!chapter?.content.trim()
  const paragraphs = useMemo(() => readerChapter?.blocks === null ? splitParagraphs(chapter?.content ?? '') : [], [readerChapter, chapter])
  const { prev, next } = useMemo(
    () => (chapter ? tocNeighbors(toc, chapter.chapter_no) : { prev: null, next: null }),
    [toc, chapter],
  )

  const from = (location.state as { from?: string } | null)?.from
  const goChapter = useCallback(
    (id: number) => navigate(`/projects/${pid}/read/${id}`, { replace: true, state: location.state }),
    [navigate, pid, location.state],
  )

  // ---------------- 进度：恢复、节流计算、停 2 秒上报、隐藏 / 卸载时 keepalive 上报 ----------------
  const pendingRef = useRef<{ ratio: number; dirty: boolean }>({ ratio: 0, dirty: false })
  const timerRef = useRef<number | null>(null)
  const report = useCallback(
    (keepalive: boolean) => {
      if (!canUseAI) return
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current)
        timerRef.current = null
      }
      const p = pendingRef.current
      if (!p.dirty || !chapter) return
      p.dirty = false
      const body = { chapter_id: chapter.id, ratio: p.ratio }
      readingRef.current = { chapter_id: chapter.id, chapter_no: chapter.chapter_no, ratio: p.ratio }
      if (keepalive) saveProgressKeepalive(pid, body)
      else readerApi.saveProgress(pid, body).catch(() => {
        // 进度上报失败不打扰阅读；下次滚动会再报
      })
    },
    [chapter, pid, readingRef, canUseAI],
  )
  const schedule = useCallback(
    (ratio: number) => {
      if (!canUseAI) return
      pendingRef.current = { ratio, dirty: true }
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = window.setTimeout(() => report(false), REPORT_DELAY_MS)
    },
    [report, canUseAI],
  )

  // 正文渲染后：只有这一章就是上次读到的章节才恢复位置，否则回到顶部；空章不记进度
  useEffect(() => {
    if (!chapter || mentionAnchor) return
    const ratio = canUseAI && written ? restoreRatio(chapter.id, readingRef.current) : 0
    const raf = window.requestAnimationFrame(() => {
      const el = document.documentElement
      window.scrollTo(0, scrollTopForRatio(ratio, el.scrollHeight, window.innerHeight))
      if (written) schedule(ratio)
    })
    return () => window.cancelAnimationFrame(raf)
  }, [chapter, written, schedule, readingRef, canUseAI, mentionAnchor])

  useEffect(() => {
    if (!written || !canUseAI) return
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
  }, [written, schedule, report, canUseAI])

  // ←/→ 切章（抽屉打开、焦点在输入控件时不响应）
  const anyDrawer = tocOpen || panelOpen || settingsOpen
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
          <Button type="text" icon={<ArrowLeftOutlined />} aria-label="返回" onClick={() => navigate(from ?? (managingOther ? '/admin/projects' : '/projects'))} />
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
        <Tooltip title={isMobile ? '编辑本章' : undefined}>
          <Button
            type="text"
            icon={<EditOutlined />}
            aria-label="编辑本章"
            onClick={() => navigate(`/projects/${pid}/chapters/${chapterId}`)}
          >
            {!isMobile && '编辑本章'}
          </Button>
        </Tooltip>
      </div>

      {managingOther && project.owner && <div style={{ padding: '12px 16px 0' }}><ProjectManagementNotice owner={project.owner} /></div>}
      <article style={articleStyle}>
        {loading && !chapter ? (
          <div style={{ padding: 48, textAlign: 'center' }}>
            <Spin />
          </div>
        ) : error || !chapter ? (
          <Result status="warning" title="章节不存在或无法读取" subTitle={error ?? undefined} />
        ) : (
          <>
            <h1 style={{ fontSize: Math.round(settings.fontSize * 1.4), lineHeight: 1.4, textAlign: 'center', margin: '0 0 1.2em' }}>
              {chapterTitle}
            </h1>
            {written && mentionsVisible ? <MentionPreview projectId={pid} text={chapter.content} anchor={mentionAnchor}
              renderText={readerChapter?.blocks ? (spans, focus) => <RichReaderContent projectId={pid} text={chapter.content} blocks={readerChapter.blocks ?? []} mentions={{ spans, focus }} /> : undefined} /> : written ? (
              readerChapter?.blocks ? <RichReaderContent projectId={pid} text={chapter.content} blocks={readerChapter.blocks} /> : paragraphs.map((p, i) => (
                <p key={i} style={{ textIndent: '2em', margin: '0 0 0.6em', wordBreak: 'break-word' }}>{p}</p>
              ))
            ) : (
              <Empty description="本章还没有正文">
                <Link to={`/projects/${pid}/chapters/${chapterId}`}>去写作</Link>
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
