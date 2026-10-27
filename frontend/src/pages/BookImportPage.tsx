import { ArrowLeftOutlined, ImportOutlined, RocketOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Descriptions, Spin, Steps, Typography } from 'antd'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { bookImportApi } from '../api/bookImport'
import { errorMessage } from '../api/client'
import type { BookEstimate, BookImport, BookOpRequest } from '../api/types'
import CorrectionStep from '../components/bookImport/CorrectionStep'
import OptionsStep from '../components/bookImport/OptionsStep'
import UploadStep from '../components/bookImport/UploadStep'
import { useIsMobile } from '../hooks/useIsMobile'
import { useLoader } from '../hooks/useLoader'
import { useTasksStore } from '../stores/tasks'
import {
  BOOK_STEPS,
  canEnterStep,
  defaultOptions,
  formatChars,
  isStaleRevision,
  optionsError,
  rangeSize,
  resolveStep,
  withStaleRefresh,
  type BookImportOptions,
  type BookStep,
} from '../utils/bookImport'

/** 预估的防抖间隔 */
const ESTIMATE_DEBOUNCE_MS = 500
const STALE_TIP = '解析结果已在别处被修改，已刷新为最新内容，请确认后重试'

/**
 * 拆书导入（/book-import，懒加载）：上传 → 校正 → 选项与预估 → 开始。当前解析 id 记在查询串 `?id=`，刷新后可继续。
 * 所有带修订号的写操作遇到 409（修订号过期）都会重新拉取解析记录并提示；开始导入成功后跳转到新项目的概览。
 */
export default function BookImportPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const isMobile = useIsMobile()
  const track = useTasksStore((s) => s.track)
  const [params, setParams] = useSearchParams()
  const idParam = Number(params.get('id')) || null

  const pendingLoader = useCallback(() => bookImportApi.list(), [])
  const { data: pending = [], loading: pendingLoading, reload: reloadPending } = useLoader(pendingLoader)

  // 最近一次拿到的解析记录；只有与查询串 id 一致时才算当前记录（换 id / 清空 id 时自然失效，不必在 effect 里复位）
  const [loaded, setLoaded] = useState<BookImport | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [step, setStep] = useState<BookStep>(0)
  const [busy, setBusy] = useState(false)
  const [options, setOptions] = useState<BookImportOptions | null>(null)
  const [deepTouched, setDeepTouched] = useState(false)
  const [estimate, setEstimate] = useState<BookEstimate | null>(null)
  const [estimating, setEstimating] = useState(false)
  const [estimateError, setEstimateError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  /** 换成另一条解析（或同一条的最新内容）；换记录、重切或章节数少于导入范围时选项按新记录重置 */
  const adopt = (next: BookImport, resetOptions: boolean) => {
    setLoaded(next)
    if (resetOptions || !options || next.chapter_count < options.importRange[1]) {
      setOptions(defaultOptions(next))
      setDeepTouched(false)
      setEstimate(null)
    }
  }

  const record = loaded && loaded.id === idParam ? loaded : null
  const loadingRecord = idParam !== null && record === null

  // 查询串里的 id → 加载解析记录并进入校正（不存在 / 他人 → 提示并清掉 id）
  useEffect(() => {
    if (idParam === null || loaded?.id === idParam) return
    let alive = true
    bookImportApi
      .get(idParam)
      .then((r) => {
        if (!alive) return
        setLoadError(null)
        adopt(r, true)
        setStep(1)
      })
      .catch((err: unknown) => {
        if (!alive) return
        setLoadError(errorMessage(err))
        setParams({}, { replace: true })
      })
    return () => {
      alive = false
    }
    // loaded 只用于跳过重复加载；adopt 每次渲染都是新函数
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idParam])

  const optsError = record && options ? optionsError(options, record) : null
  const stepCtx = useMemo(() => ({ record, optionsError: options ? optsError : '请先完成校正' }), [record, options, optsError])
  // 前置条件不再满足（如章节被删光、选项变得不合法）时退回能到的一步
  const current = resolveStep(step, stepCtx)

  const goStep = (s: BookStep) => {
    if (canEnterStep(s, stepCtx)) setStep(s)
  }

  const openRecord = (id: number) => {
    if (record?.id === id) {
      setStep(1)
      return
    }
    setParams({ id: String(id) })
  }

  const refetch = useCallback(async () => {
    if (!record) throw new Error('没有正在校正的解析')
    return bookImportApi.get(record.id)
  }, [record])

  /** 带修订号的写操作：成功替换记录；修订号过期 → 重新拉取并提示 */
  const mutate = async (
    call: (revision: number) => Promise<BookImport>,
    success?: string,
    resetOptions = false,
  ): Promise<boolean> => {
    if (!record) return false
    setBusy(true)
    try {
      const outcome = await withStaleRefresh(() => call(record.revision), refetch)
      if (outcome.ok) {
        adopt(outcome.value, resetOptions)
        if (success) message.success(success)
        return true
      }
      adopt(outcome.record, false)
      message.warning(STALE_TIP)
      return false
    } catch (err) {
      message.error(errorMessage(err))
      return false
    } finally {
      setBusy(false)
    }
  }

  const handleOp = (body: Omit<BookOpRequest, 'revision'>, success?: string) =>
    mutate((revision) => bookImportApi.op(record!.id, { ...body, revision }), success)

  // 重切后章节完全重排：选项按新结果重置
  const handleResplit = (mode: 'auto' | 'regex', pattern: string | null) =>
    mutate((revision) => bookImportApi.resplit(record!.id, { mode, pattern, revision }), '已重新切章', true)

  // 选项变化后防抖调用 estimate（选项不合法时不请求）
  useEffect(() => {
    if (!record || !options || optsError || current < 2) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      setEstimating(true)
      setEstimateError(null)
      bookImportApi
        .estimate(
          record.id,
          {
            import_range: options.importRange,
            reverse_range: options.reverseRange,
            extract_style: options.extractStyle,
            deep_analyze: options.deepAnalyze,
          },
          controller.signal,
        )
        .then((e) => setEstimate(e))
        .catch((err: unknown) => {
          if (!controller.signal.aborted) setEstimateError(errorMessage(err))
        })
        .finally(() => {
          if (!controller.signal.aborted) setEstimating(false)
        })
    }, ESTIMATE_DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [record, options, optsError, current])

  const handleStart = async () => {
    if (!record || !options || optsError) return
    setStarting(true)
    try {
      const r = await bookImportApi.start(record.id, {
        revision: record.revision,
        title: options.title.trim(),
        genre: options.genre.trim(),
        import_range: options.importRange,
        reverse_range: options.reverseRange,
        extract_style: options.extractStyle,
        deep_analyze: options.deepAnalyze,
        ai_config_id: options.aiConfigId,
      })
      track(r.task)
      message.success('项目已创建，AI 正在后台反推设定，可在右下角任务面板查看进度')
      navigate(`/projects/${r.project_id}/overview`)
    } catch (err) {
      if (isStaleRevision(err)) {
        try {
          adopt(await refetch(), false)
          setStep(1)
          message.warning(STALE_TIP)
        } catch (e) {
          message.error(errorMessage(e))
        }
      } else {
        message.error(errorMessage(err))
      }
    } finally {
      setStarting(false)
    }
  }

  const handleDeleted = (id: number) => {
    reloadPending()
    if (record?.id === id) setParams({})
  }

  const steps = (
    <Steps
      size="small"
      current={current}
      responsive={false}
      onChange={(s) => goStep(s as BookStep)}
      items={BOOK_STEPS.map((s, i) => ({ title: s.title, disabled: !canEnterStep(i as BookStep, stepCtx) }))}
      style={{ minWidth: isMobile ? 360 : undefined }}
    />
  )

  return (
    <div style={{ maxWidth: 1280, margin: '0 auto', padding: isMobile ? 12 : 24 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
        <Typography.Title level={3} style={{ margin: 0 }}>
          <ImportOutlined /> 拆书导入
        </Typography.Title>
        <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/projects')}>
          返回书架
        </Button>
      </div>
      <Typography.Paragraph type="secondary">
        上传一本已有的小说，切出章节并人工校正后导入为新项目；AI 会通读全文反推项目信息、大纲摘要、角色、组织、世界观、职业体系、关系与写作风格。
      </Typography.Paragraph>
      {/* 移动端步骤条横向滚动（不压缩成竖排），不撑出页面级横向溢出 */}
      <div style={{ overflowX: 'auto', marginBottom: 16 }}>{steps}</div>
      {loadError && <Alert type="error" showIcon closable message={loadError} style={{ marginBottom: 16 }} onClose={() => setLoadError(null)} />}
      <Spin spinning={loadingRecord}>
        {current === 0 && (
          <UploadStep
            pending={pending}
            pendingLoading={pendingLoading}
            currentId={record?.id ?? null}
            onUploaded={(r) => {
              reloadPending()
              adopt(r, true)
              setParams({ id: String(r.id) })
              setStep(1)
            }}
            onOpen={openRecord}
            onDeleted={handleDeleted}
          />
        )}
        {current === 1 && record && (
          <CorrectionStep key={record.id} record={record} busy={busy} onOp={handleOp} onResplit={handleResplit} />
        )}
        {current === 2 && record && options && (
          <OptionsStep
            record={record}
            options={options}
            onChange={setOptions}
            deepTouched={deepTouched}
            onDeepTouched={() => setDeepTouched(true)}
            estimate={estimate}
            estimating={estimating}
            estimateError={estimateError}
            error={optsError}
          />
        )}
        {current === 3 && record && options && (
          <Card size="small" title="确认并开始">
            <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
              <Descriptions.Item label="书名">《{options.title.trim()}》</Descriptions.Item>
              <Descriptions.Item label="主题材">{options.genre.trim() || '由 AI 反推'}</Descriptions.Item>
              <Descriptions.Item label="导入">
                第 {options.importRange[0]}–{options.importRange[1]} 章（{rangeSize(options.importRange)} 章，导入后从第 1 章编号）
              </Descriptions.Item>
              <Descriptions.Item label="反推">
                第 {options.reverseRange[0]}–{options.reverseRange[1]} 章（{rangeSize(options.reverseRange)} 章）
              </Descriptions.Item>
              <Descriptions.Item label="提取风格">{options.extractStyle ? '是' : '否'}</Descriptions.Item>
              <Descriptions.Item label="深度分析">{options.deepAnalyze ? '是' : '否（之后可在章节列表「一键分析」）'}</Descriptions.Item>
              {estimate && <Descriptions.Item label="预估 AI 输入">{formatChars(estimate.total_input)}</Descriptions.Item>}
            </Descriptions>
            <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
              开始后立即新建项目并导入正文（每章保存一个「导入」版本），这条解析随之删除；AI 反推在后台进行，失败时可在任务面板继续、跳过本批或结束，已写入的内容都会保留。
            </Typography.Paragraph>
          </Card>
        )}
      </Spin>
      {current > 0 && (
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, marginTop: 16, flexWrap: 'wrap' }}>
          <Button onClick={() => setStep((current - 1) as BookStep)} disabled={busy || starting}>
            上一步
          </Button>
          {current < 3 ? (
            <Button type="primary" onClick={() => goStep((current + 1) as BookStep)} disabled={busy || !canEnterStep((current + 1) as BookStep, stepCtx)}>
              下一步
            </Button>
          ) : (
            <Button type="primary" icon={<RocketOutlined />} loading={starting} disabled={optsError !== null} onClick={() => void handleStart()}>
              开始导入
            </Button>
          )}
        </div>
      )}
    </div>
  )
}
