import { ReloadOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Skeleton, Space, Tabs, Tooltip, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { chapterApi } from '../../api/chapters'
import { ApiError, errorMessage } from '../../api/client'
import { consistencyApi, type ConsistencyIssue } from '../../api/consistency'
import CheckModal from '../../components/consistency/CheckModal'
import CheckProgress from '../../components/consistency/CheckProgress'
import { parseUsageId } from '../../components/aiUsage/usagePresentation'
import UnavailableTarget from '../../components/notifications/UnavailableTarget'
import { useNotificationTarget } from '../../components/notifications/useNotificationTarget'
import { useProjectStore } from '../../stores/project'
import CheckSummary, { RunSummary } from '../../components/consistency/CheckSummary'
import ImpactPanel from '../../components/consistency/ImpactPanel'
import IssueDrawer from '../../components/consistency/IssueDrawer'
import IssueList, { IssueFilters } from '../../components/consistency/IssueList'
import { useConsistencyQuery } from '../../components/consistency/useConsistencyQuery'
import { useRunningTaskFollow } from '../../components/consistency/useRunningTaskFollow'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useProjectId } from '../../hooks/useProjectId'
import { useProjectRefresh } from '../../hooks/useProjectRefresh'
import { useAuthStore } from '../../stores/auth'
import {
  checkEndNotice, consistencyPermissions, createRequestGate, draftFromIssue, isCheckConflict, isDraftDirty,
  isStaleRevision, issueListQuery, issueUpdateBody, rebaseDraft, settleCurrent, type IssueDraft, type IssueFilter,
} from '../../utils/consistency'
import {
  readConsistencyTab, readImpactChapter, readImpactTask, withImpactLocation, type CheckRange, type ConsistencyTab,
} from '../../utils/consistencyImpact'
import { authorizationKey } from '../../utils/permissions'

/**
 * 一致性检查：手动发起的跨章 AI 检查与「潜在冲突」清单；「修改影响」页签列出改稿后可能过期的派生内容与受影响章节。
 * 作品、身份变化时整页重建，不沿用旧数据。
 */
export default function ConsistencyPage() {
  const pid = useProjectId()
  const { ready, canRead } = useProjectAccess(pid)
  const identity = useAuthStore((state) => `${state.status}:${state.user?.id ?? ''}:${authorizationKey(state.user)}`)
  const [params] = useSearchParams()
  const navigation = useLocation().key
  const version = useProjectStore((s) => s.scopeVersion)
  const linked = params.has('run_id')
  const notificationId = params.get('notification_id') ?? undefined
  const runId = parseUsageId(params.get('run_id'))
  if (!ready) return <Skeleton active />
  if (linked) {
    if (!canRead || !runId || notificationId === '') return <UnavailableTarget />
    return <HistoricRun key={`${pid}:${identity}:${version}:${navigation}`} projectId={pid} runId={runId} notificationId={notificationId} />
  }
  if (!canRead) return <Alert type="warning" showIcon message="当前无权查看此作品的一致性检查" />
  return <ConsistencyContent key={`${pid}:${identity}`} projectId={pid} />
}

function HistoricRun({ projectId, runId, notificationId }: { projectId: number; runId: number; notificationId?: string }) {
  const read = useCallback((signal: AbortSignal) => consistencyApi.run(projectId, runId, signal, notificationId), [projectId, runId, notificationId])
  const { result, retry } = useNotificationTarget(read)
  return <Space direction="vertical" style={{ width: '100%' }}>
    <Link to={`/projects/${projectId}/consistency`}>返回一致性检查</Link>
    {!result ? <Skeleton active /> : 'missing' in result ? <UnavailableTarget />
      : 'error' in result ? <Alert type="error" showIcon message={result.error} action={<Button onClick={retry}>重试</Button>} />
        : <RunSummary title={`检查概要 #${runId}`} run={result.data} chapters={[]} />}
  </Space>
}

function ConsistencyContent({ projectId }: { projectId: number }) {
  const { message, modal } = App.useApp()
  const access = useProjectAccess(projectId)
  const { canReadNow, canEditNow, canUseAINow } = access
  const hasConfig = useHasAIConfig()
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  // ---------------- 数据 ----------------
  const [filter, setFilter] = useState<IssueFilter>({})
  const [page, setPage] = useState(1)
  const query = useMemo(() => issueListQuery(filter, page), [filter, page])
  const summaryLoad = useCallback((signal: AbortSignal) => consistencyApi.summary(projectId, signal), [projectId])
  const issuesLoad = useCallback((signal: AbortSignal) => consistencyApi.issues(projectId, query, signal), [projectId, query])
  const chaptersLoad = useCallback((signal: AbortSignal) => chapterApi.list(projectId, signal), [projectId])
  const summary = useConsistencyQuery(projectId, 'summary', summaryLoad)
  const issues = useConsistencyQuery(projectId, `issues:${JSON.stringify(query)}`, issuesLoad)
  const chapterList = useConsistencyQuery(projectId, 'chapters', chaptersLoad)
  // 页签与所选章节记在地址栏（任务面板、章节编辑页的提示可直接深链到某一章）
  const [searchParams, setSearchParams] = useSearchParams()
  const tab = readConsistencyTab(searchParams)
  const impactChapterId = readImpactChapter(searchParams)
  const setImpactLocation = useCallback((nextTab: ConsistencyTab, chapterId: number | null = null) => {
    setSearchParams((previous) => withImpactLocation(previous, nextTab, chapterId), { replace: true })
  }, [setSearchParams])
  // 「修改影响」概览只在该页签下读取；它同时报告进行中的评估任务
  const impactsLoad = useCallback((signal: AbortSignal) => consistencyApi.impacts(projectId, signal), [projectId])
  const impacts = useConsistencyQuery(projectId, 'impacts', impactsLoad, tab === 'impact')
  const { reload: reloadImpacts } = impacts
  // 进行中的评估任务（轻量）：评估期间轮询它而不是概览——概览每次都要读全书正文计算指纹
  const impactRunningLoad = useCallback((signal: AbortSignal) => consistencyApi.impactRunning(projectId, signal), [projectId])
  const impactRunningQuery = useConsistencyQuery(projectId, 'impact-running', impactRunningLoad, tab === 'impact')
  const { reload: reloadImpactRunning } = impactRunningQuery
  const impactTaskId = readImpactTask(searchParams)
  // 页面级刷新的代次：检查结束或手动刷新后，「修改影响」页签重新读取打开中的章节详情
  const [refreshToken, setRefreshToken] = useState(0)
  const { reload: reloadSummary } = summary
  const { reload: reloadIssues } = issues
  const { reload: reloadChapters } = chapterList
  const chapters = useMemo(
    () => [...(chapterList.data ?? [])].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id),
    [chapterList.data],
  )

  // ---------------- 问题详情与状态草稿 ----------------
  const [selected, setSelected] = useState<ConsistencyIssue | null>(null)
  const [draft, setDraft] = useState<IssueDraft | null>(null)
  const [conflict, setConflict] = useState(false)
  const [saving, setSaving] = useState(false)
  const [refreshingIssue, setRefreshingIssue] = useState(false)
  // 详情请求的代次：打开另一个问题、关闭抽屉或再次请求后，迟到的响应不得回填
  const [gate] = useState(createRequestGate)

  const confirmDiscard = (onOk: () => void) => {
    modal.confirm({ title: '放弃未保存的修改？', content: '状态或备注的修改尚未保存。', okText: '放弃', cancelText: '继续编辑', onOk })
  }
  // 最近打开的问题：抽屉关闭后把焦点还给对应的列表条目（键盘用户不必从页面开头重新找）
  const lastOpenedRef = useRef<number | null>(null)
  const restoreIssueFocus = () => {
    const id = lastOpenedRef.current
    if (id !== null) document.querySelector<HTMLElement>(`[data-consistency-issue="${id}"]`)?.focus()
  }
  const openIssue = (issue: ConsistencyIssue) => {
    if (!canReadNow()) return
    const apply = () => { gate.invalidate(); lastOpenedRef.current = issue.id; setSelected(issue); setDraft(draftFromIssue(issue)); setConflict(false) }
    if (selected && selected.id !== issue.id && draft && isDraftDirty(draft)) confirmDiscard(apply)
    else apply()
  }
  const closeIssue = () => {
    const apply = () => { gate.invalidate(); setSelected(null); setDraft(null); setConflict(false) }
    if (draft && isDraftDirty(draft)) confirmDiscard(apply)
    else apply()
  }

  /** 重新读取当前问题：作者改过的字段保留本地输入，其余跟随服务端，修订号换成最新。 */
  const refreshIssue = useCallback(async (issueId: number) => {
    const current = gate.begin()
    setRefreshingIssue(true)
    const result = await settleCurrent(() => current() && alive.current, consistencyApi.issue(projectId, issueId))
    if (!alive.current) return
    setRefreshingIssue(false)
    if (result.status === 'stale') return
    if (result.status === 'error') {
      if (result.error instanceof ApiError && result.error.status === 404) {
        setSelected(null); setDraft(null); setConflict(false)
        message.warning('这个问题已不存在')
      } else message.error(errorMessage(result.error))
      return
    }
    const fresh = result.value
    setSelected(fresh)
    setDraft((prev) => (prev ? rebaseDraft(prev, fresh) : draftFromIssue(fresh)))
    setConflict(false)
  }, [gate, projectId, message])

  const refreshAll = useCallback(() => {
    reloadSummary(); reloadIssues(); reloadChapters(); reloadImpacts(); reloadImpactRunning()
    setRefreshToken((value) => value + 1)
  }, [reloadSummary, reloadIssues, reloadChapters, reloadImpacts, reloadImpactRunning])
  // 章节增删改、改章号都会影响台账覆盖、需重新核查与证据章号
  useProjectRefresh(projectId, ['chapters'], refreshAll)

  const saveIssue = async () => {
    if (!selected || !draft || saving || !canEditNow()) return
    const body = issueUpdateBody(draft)
    if (!body) return
    const current = gate.begin()
    setSaving(true)
    const result = await settleCurrent(() => current() && alive.current, consistencyApi.update(projectId, selected.id, body))
    if (!alive.current) return
    setSaving(false)
    // 请求已发出：无论抽屉是否已切走，列表与计数都可能已变化
    reloadIssues(); reloadSummary()
    if (result.status === 'stale') return
    if (result.status === 'error') {
      // 冲突：草稿原样保留，提示刷新后再保存
      if (isStaleRevision(result.error)) setConflict(true)
      else message.error(errorMessage(result.error))
      return
    }
    setSelected(result.value)
    setDraft(draftFromIssue(result.value))
    setConflict(false)
    message.success('已保存')
  }

  // ---------------- 检查任务 ----------------
  // 发起检查弹窗：range 为从「修改影响」页签快捷发起时预填的起止章节
  const [checkModal, setCheckModal] = useState<{ range: CheckRange | null } | null>(null)
  const [rechecking, setRechecking] = useState(false)
  // 进行中的检查由概要报告；接上进度流、轮询兜底、核对终态与取消都在 useRunningTaskFollow 里（与评估任务共用）
  const follow = useRunningTaskFollow({
    runningId: summary.data?.running_task_id ?? null, runningKey: summary.data?.running_task_key ?? null,
    loaded: summary.data !== undefined, canUseAI: access.canUseAI, isCurrent: canUseAINow,
    reload: reloadSummary, endNotice: checkEndNotice,
    onSettled: () => {
      reloadSummary(); reloadIssues()
      // 检查会刷新事实台账：台账状态、能否评估与过期判断都可能变化
      reloadImpacts(); setRefreshToken((value) => value + 1)
      if (selected) void refreshIssue(selected.id)
    },
  })
  const { busy, started } = follow
  const permissions = consistencyPermissions(access, follow.owns)

  const recheck = async () => {
    if (!selected || rechecking || !canUseAINow()) return
    setRechecking(true)
    try {
      const task = await consistencyApi.recheck(projectId, selected.id)
      if (!alive.current || !canUseAINow()) return
      message.success('已开始重新核查')
      started(task)
    } catch (err) {
      if (!alive.current) return
      message.error(errorMessage(err))
      if (isCheckConflict(err)) reloadSummary()
    } finally {
      if (alive.current) setRechecking(false)
    }
  }

  // 检查与修改影响评估互斥（后端同一 resource_key）；评估是否在进行只在「修改影响」页签下已知，其余情况由后端 409 兜底
  const impactRunning = ((impactRunningQuery.data ?? impacts.data)?.running_task_id ?? null) !== null
  const startBlocked = busy ? '已有进行中的一致性检查'
    : impactRunning ? '已有进行中的修改影响评估'
      : hasConfig === false ? '尚未配置可用的文本模型' : ''
  const openCheckModal = (range: CheckRange | null) => { if (canUseAINow()) setCheckModal({ range }) }
  const startButton = permissions.canStartCheck && (startBlocked
    ? <Tooltip title={startBlocked}><Button type="primary" disabled>发起检查</Button></Tooltip>
    : <Button type="primary" onClick={() => openCheckModal(null)}>发起检查</Button>)
  const applyFilter = (next: IssueFilter) => { setFilter(next); setPage(1) }
  const loadError = summary.error || (tab === 'issues' ? issues.error : undefined) || chapterList.error

  return <Space direction="vertical" size="large" style={{ width: '100%', minWidth: 0 }}>
    <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 12 }}>
      <div style={{ minWidth: 0 }}>
        <Typography.Title level={2} style={{ margin: 0 }}>一致性检查</Typography.Title>
        <Typography.Text type="secondary">逐章抽取事实后跨章比对，列出人物经历、状态、物品去向、时间顺序与伏笔前提上的潜在冲突。</Typography.Text>
      </div>
      <Space wrap>
        <Button icon={<ReloadOutlined />} onClick={refreshAll} loading={summary.refreshing || issues.refreshing || impacts.refreshing}>刷新</Button>
        {startButton}
      </Space>
    </div>
    {loadError && <Alert type="error" showIcon message="一致性检查数据加载失败" description={loadError} action={<Button onClick={refreshAll}>重试</Button>} />}
    {busy && <CheckProgress
      task={follow.task} reconnecting={follow.reconnecting} lostMessage={follow.lost}
      canCancel={permissions.canCancelCheck} cancelling={follow.cancelling} onCancel={() => void follow.cancel()}
      onRetrySubscribe={(summary.data?.running_task_key ?? null) !== null && access.canUseAI ? follow.retrySubscribe : undefined} />}
    <Tabs activeKey={tab} onChange={(key) => setImpactLocation(key === 'impact' ? 'impact' : 'issues')} items={[
      { key: 'issues', label: '潜在冲突' },
      { key: 'impact', label: '修改影响' },
    ]} />
    {tab === 'issues' && <>
      <Alert type="info" showIcon message="以下内容是 AI 判断的「潜在冲突」，仅供参考"
        description="结论由模型根据各章事实台账得出，可能误报或漏报，请对照原文自行判断。检查只读取正文，不会改动正文、设定、伏笔或剧情记忆。" />
      {summary.loading ? <Skeleton active paragraph={{ rows: 4 }} /> : summary.data && <CheckSummary summary={summary.data} chapters={chapters} />}
      <Card size="small" title="潜在冲突清单">
        <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
          <IssueFilters filter={filter} chapters={chapters} onChange={applyFilter} />
          {issues.loading ? <Skeleton active paragraph={{ rows: 5 }} /> : issues.data && <IssueList
            items={issues.data.items} total={issues.data.total} page={page} filter={filter}
            checked={!!summary.data?.last_run} emptyAction={startButton || undefined}
            onPage={setPage} onOpen={openIssue} />}
        </Space>
      </Card>
    </>}
    {tab === 'impact' && <ImpactPanel
      projectId={projectId} overview={impacts} running={impactRunningQuery} selectedChapterId={impactChapterId} taskId={impactTaskId}
      onSelect={(chapterId) => setImpactLocation('impact', chapterId)}
      checkBusy={busy} checkBlocked={startBlocked} refreshToken={refreshToken}
      onRequestCheck={openCheckModal} onTaskChanged={reloadSummary} />}
    <IssueDrawer
      projectId={projectId} issue={selected} draft={draft} conflict={conflict} saving={saving}
      refreshing={refreshingIssue} rechecking={rechecking}
      canUpdate={permissions.canUpdateIssue} canRecheck={permissions.canRecheck} recheckBlocked={startBlocked}
      onDraft={setDraft} onSave={() => void saveIssue()} onRefresh={() => { if (selected) void refreshIssue(selected.id) }}
      onRecheck={() => void recheck()} onClose={closeIssue} onClosed={restoreIssueFocus} />
    {checkModal && <CheckModal
      projectId={projectId} chapters={chapters} isCurrent={canUseAINow} initialRange={checkModal.range} onClose={() => setCheckModal(null)}
      onStarted={(task) => { setCheckModal(null); message.success('已开始检查'); started(task) }}
      onConflict={() => { reloadSummary(); reloadImpactRunning() }} />}
  </Space>
}
