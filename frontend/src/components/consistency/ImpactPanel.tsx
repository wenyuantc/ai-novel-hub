import { Alert, App, Button, Card, Empty, List, Skeleton, Space, Tag, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import { consistencyApi, type ImpactChapterSummary, type ImpactDetail, type ImpactOverview, type ImpactRunning } from '../../api/consistency'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { createRequestGate, isStaleRevision, settleCurrent } from '../../utils/consistency'
import {
  assessGate, chapterLabel, impactConclusion, impactEndNotice, impactPermissions, impactRowNotes, impactTaskChapter, impactUpdateBody,
  isImpactDirty, LEDGER_STATE, NO_IMPACT_EDITS, staleLabels, type CheckRange, type ImpactEdits,
} from '../../utils/consistencyImpact'
import { formatTime } from '../../utils/time'
import CheckProgress from './CheckProgress'
import ImpactDrawer from './ImpactDrawer'
import { useRunningTaskFollow } from './useRunningTaskFollow'

interface OverviewQuery {
  data: ImpactOverview | undefined
  error: string | undefined
  loading: boolean
  reload: () => void
}

interface RunningQuery {
  data: ImpactRunning | undefined
  reload: () => void
}

interface Props {
  projectId: number
  overview: OverviewQuery
  /** 进行中的评估任务（轻量接口）：评估期间轮询它，任务结束后才重新读取概览 */
  running: RunningQuery
  /** 所选章节（来自地址栏，便于深链）；null = 未打开详情 */
  selectedChapterId: number | null
  /** 任务面板跳转带来的评估任务 id：找到对应章节后打开它 */
  taskId: number | null
  onSelect: (chapterId: number | null) => void
  /** 本作品有一致性检查在进行（与评估互斥） */
  checkBusy: boolean
  /** 不能发起检查的原因；空串 = 可以 */
  checkBlocked: string
  /** 页面级刷新的代次（检查结束、手动刷新）：变化时重新读取打开中的详情 */
  refreshToken: number
  /** 打开发起检查弹窗并预填范围 */
  onRequestCheck: (range: CheckRange | null) => void
  /** 评估任务结束或互斥冲突：页面刷新概要（台账覆盖、进行中的检查） */
  onTaskChanged: () => void
}

interface DetailState { chapterId: number; detail: ImpactDetail | null; error: string }

/** 「修改影响」页签：有过期派生内容或评估记录的章节列表、单章详情抽屉、评估任务的进度与取消。 */
export default function ImpactPanel({
  projectId, overview, running, selectedChapterId, taskId, onSelect, checkBusy, checkBlocked, refreshToken, onRequestCheck, onTaskChanged,
}: Props) {
  const { message, modal } = App.useApp()
  const access = useProjectAccess(projectId)
  const { canReadNow, canEditNow, canUseAINow } = access
  const hasConfig = useHasAIConfig()
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const { reload: reloadOverview } = overview
  const { reload: reloadRunning } = running

  // ---------------- 单章详情 ----------------
  const [detailState, setDetailState] = useState<DetailState | null>(null)
  const [detailTick, setDetailTick] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  // 本地勾选按章节保存：切换章节后旧勾选自动失效
  const [editsState, setEditsState] = useState<{ chapterId: number; value: ImpactEdits } | null>(null)
  const [conflict, setConflict] = useState(false)
  const [saving, setSaving] = useState(false)
  // 详情请求的代次：切换章节、关闭抽屉、保存或再次请求后，迟到的响应不得回填
  const [gate] = useState(createRequestGate)
  const selectRef = useRef(onSelect)
  useEffect(() => { selectRef.current = onSelect })

  useEffect(() => {
    if (selectedChapterId === null) { gate.invalidate(); return }
    const current = gate.begin()
    let active = true
    void settleCurrent(() => active && current() && alive.current, consistencyApi.impact(projectId, selectedChapterId)).then((result) => {
      if (result.status === 'stale') return
      setRefreshing(false)
      if (result.status === 'ok') {
        setDetailState({ chapterId: selectedChapterId, detail: result.value, error: '' })
        setConflict(false)
      } else if (result.error instanceof ApiError && result.error.status === 404) {
        message.warning('章节不存在或已被删除')
        selectRef.current(null)
      } else {
        const text = errorMessage(result.error)
        // 刷新失败保留已显示的同一章详情，只附带错误
        setDetailState((prev) => ({ chapterId: selectedChapterId, detail: prev?.chapterId === selectedChapterId ? prev.detail : null, error: text }))
      }
    })
    return () => { active = false }
  }, [projectId, selectedChapterId, detailTick, refreshToken, gate, message])

  const shown = selectedChapterId !== null && detailState?.chapterId === selectedChapterId ? detailState : null
  const detail = shown?.detail ?? null
  const edits = selectedChapterId !== null && editsState?.chapterId === selectedChapterId ? editsState.value : NO_IMPACT_EDITS
  const dirty = isImpactDirty(detail, edits)

  const confirmDiscard = (onOk: () => void) => {
    modal.confirm({ title: '放弃未保存的确认？', content: '复核状态或「已确认无需更新」的勾选尚未保存。', okText: '放弃', cancelText: '继续编辑', onOk })
  }
  // 最近打开的章节：抽屉关闭后把焦点还给对应的列表条目
  const lastOpenedRef = useRef<number | null>(selectedChapterId)
  const restoreFocus = () => {
    const id = lastOpenedRef.current
    if (id !== null) document.querySelector<HTMLElement>(`[data-impact-chapter="${id}"]`)?.focus()
  }
  const openChapter = (chapterId: number) => {
    if (!canReadNow() || chapterId === selectedChapterId) return
    const apply = () => { lastOpenedRef.current = chapterId; setEditsState(null); setConflict(false); onSelect(chapterId) }
    if (dirty) confirmDiscard(apply)
    else apply()
  }
  const closeChapter = () => {
    const apply = () => { setEditsState(null); setConflict(false); onSelect(null) }
    if (dirty) confirmDiscard(apply)
    else apply()
  }
  const refreshDetail = () => { setRefreshing(true); setDetailTick((value) => value + 1) }

  const save = async () => {
    if (!detail || saving || !canEditNow()) return
    const body = impactUpdateBody(detail, edits)
    if (!body) return
    const chapterId = detail.chapter_id
    const current = gate.begin()
    setSaving(true)
    const result = await settleCurrent(() => current() && alive.current, consistencyApi.updateImpact(projectId, chapterId, body))
    if (!alive.current) return
    setSaving(false)
    setRefreshing(false)
    // 请求已发出：无论抽屉是否已切走，列表上的计数都可能已变化
    reloadOverview()
    if (result.status === 'stale') return
    if (result.status === 'error') {
      // 冲突：本地勾选原样保留，提示刷新后再保存
      if (isStaleRevision(result.error)) setConflict(true)
      else message.error(errorMessage(result.error))
      return
    }
    setDetailState({ chapterId, detail: result.value, error: '' })
    setEditsState(null)
    setConflict(false)
    message.success('已保存')
  }

  // ---------------- 评估任务 ----------------
  const [assessing, setAssessing] = useState(false)
  // 本页刚发起评估的章节（接口刷新前用它判断进度属于哪一章）
  const [startedChapterId, setStartedChapterId] = useState<number | null>(null)
  // 轻量接口读到之前先用概览里的同一组字段；之后以轻量接口为准（轮询只刷新它）
  const runningInfo = running.data ?? overview.data
  const runningId = runningInfo?.running_task_id ?? null
  const runningKey = runningInfo?.running_task_key ?? null
  const follow = useRunningTaskFollow({
    runningId, runningKey, loaded: running.data !== undefined, canUseAI: access.canUseAI, isCurrent: canUseAINow,
    reload: reloadRunning, endNotice: impactEndNotice,
    onSettled: () => {
      reloadRunning()
      reloadOverview()
      setDetailTick((value) => value + 1)
      onTaskChanged()
    },
  })
  const impactBusy = follow.busy
  const runningChapterId = impactBusy ? runningInfo?.running_chapter_id ?? startedChapterId : null

  // 任务面板跳转：按任务 id 找到它评估的章节并打开；找不到（或已选了章节）就去掉该参数，停在列表
  const overviewChapters = overview.data?.chapters
  useEffect(() => {
    if (taskId === null) return
    const target = selectedChapterId ?? impactTaskChapter(taskId, runningInfo, overviewChapters)
    if (target !== undefined) selectRef.current(target)
  }, [taskId, selectedChapterId, runningInfo, overviewChapters])
  const permissions = impactPermissions(access, follow.owns)

  const assess = async () => {
    if (!detail || assessing || !canUseAINow()) return
    const chapterId = detail.chapter_id
    setAssessing(true)
    try {
      const task = await consistencyApi.assess(projectId, chapterId)
      if (!alive.current || !canUseAINow()) return
      message.success('已开始评估')
      setStartedChapterId(chapterId)
      follow.started(task)
    } catch (err) {
      if (!alive.current) return
      message.error(errorMessage(err))
      // 409 = 与进行中的检查或评估互斥；400 = 评估前提已变化：都刷新以显示真实状态
      if (err instanceof ApiError && (err.status === 409 || err.status === 400)) {
        reloadRunning(); reloadOverview(); onTaskChanged(); setDetailTick((value) => value + 1)
      }
    } finally {
      if (alive.current) setAssessing(false)
    }
  }

  const progress = (inDrawer: boolean) => impactBusy && <div style={inDrawer ? { marginBottom: 12 } : undefined}>
    <CheckProgress kind="impact" task={follow.task} reconnecting={follow.reconnecting} lostMessage={follow.lost}
      canCancel={permissions.canCancelAssess} cancelling={follow.cancelling} onCancel={() => void follow.cancel()}
      onRetrySubscribe={runningKey !== null && access.canUseAI ? follow.retrySubscribe : undefined} />
  </div>
  const gateInfo = detail ? assessGate({ detail, checkBusy, impactBusy, hasConfig }) : { reason: '', needsCheck: false }
  const chapters = overview.data?.chapters ?? []
  const runningRow = runningChapterId === null ? undefined : chapters.find((row) => row.chapter_id === runningChapterId)

  return <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
    <Alert type="info" showIcon message="修改早期章节后，在这里查看哪些内容可能需要跟着更新"
      description="列出正文修改后可能过期的章摘要、剧情记忆与设定变化（按正文指纹即时判断，不调用模型）；对已改动的章节还可以手动「评估影响」，找出可能受影响的后续章节供复核。所有结果只是提示，不会自动修改任何内容。" />
    {overview.error && <Alert type="error" showIcon message="修改影响数据加载失败" description={overview.error} action={<Button onClick={reloadOverview}>重试</Button>} />}
    {impactBusy && <div>
      {progress(false)}
      {runningChapterId !== null && runningChapterId !== selectedChapterId && <Button type="link" style={{ paddingInline: 0 }} onClick={() => openChapter(runningChapterId)}>
        查看正在评估的章节{runningRow ? `（${chapterLabel(runningRow)}）` : ''}
      </Button>}
    </div>}
    <Card size="small" title="受修改影响的章节">
      {overview.loading ? <Skeleton active paragraph={{ rows: 5 }} /> : overview.data && (chapters.length === 0
        ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<Space direction="vertical" size={4}>
          <span>目前没有派生内容过期的章节，也没有评估记录</span>
          <Typography.Text type="secondary">修改并保存已分析过的章节后，该章会出现在这里。</Typography.Text>
        </Space>} />
        : <List<ImpactChapterSummary> aria-label="受修改影响的章节列表" itemLayout="vertical" dataSource={chapters} rowKey="chapter_id"
          renderItem={(row) => {
            const conclusion = impactConclusion(row)
            const notes = impactRowNotes(row)
            const stale = staleLabels(row)
            return <List.Item key={row.chapter_id} style={{ paddingInline: 0 }}>
              <Space direction="vertical" size={6} style={{ width: '100%', minWidth: 0 }}>
                <Button type="link" onClick={() => openChapter(row.chapter_id)} aria-label={`查看修改影响：${chapterLabel(row)}`} data-impact-chapter={row.chapter_id}
                  style={{ padding: 0, height: 'auto', whiteSpace: 'normal', textAlign: 'left', fontWeight: 600, overflowWrap: 'anywhere' }}>
                  {chapterLabel(row)}
                </Button>
                <Space size={[4, 4]} wrap>
                  {stale.length === 0 ? <Tag color="success">派生内容无过期</Tag> : stale.map((label) => <Tag key={label} color="warning" style={{ whiteSpace: 'normal' }}>{label}</Tag>)}
                  <Tag color={LEDGER_STATE[row.ledger_state].color}>{LEDGER_STATE[row.ledger_state].label}</Tag>
                  {row.chapter_id === runningChapterId && <Tag color="processing">评估中</Tag>}
                </Space>
                <Space size={[4, 4]} wrap>
                  <Typography.Text type="secondary">最近评估：</Typography.Text>
                  <Tag color={conclusion.color}>{conclusion.label}</Tag>
                  {row.assessed && row.assessed_at && <Typography.Text type="secondary">{formatTime(row.assessed_at)}</Typography.Text>}
                </Space>
                {notes.length > 0 && <Typography.Text type={row.outdated || row.pending_items > 0 ? 'warning' : 'secondary'} style={{ overflowWrap: 'anywhere' }}>
                  {notes.join('；')}
                </Typography.Text>}
              </Space>
            </List.Item>
          }} />)}
    </Card>
    <ImpactDrawer
      projectId={projectId} chapterId={selectedChapterId} detail={detail} error={shown?.error ?? ''} edits={edits} dirty={dirty}
      conflict={conflict} saving={saving} refreshing={refreshing} assessing={assessing}
      canConfirm={permissions.canConfirm} canAssess={permissions.canAssess} canStartCheck={permissions.canStartCheck} canEdit={access.canEdit}
      gate={gateInfo} checkBlocked={checkBlocked}
      progress={detail && runningChapterId === detail.chapter_id ? progress(true) : null}
      onEdits={(value) => { if (selectedChapterId !== null && canEditNow()) setEditsState({ chapterId: selectedChapterId, value }) }}
      onSave={() => void save()} onRefresh={refreshDetail} onAssess={() => void assess()}
      onRequestCheck={onRequestCheck} onClose={closeChapter} onClosed={restoreFocus} />
  </Space>
}
