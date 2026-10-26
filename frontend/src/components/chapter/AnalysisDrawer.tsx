import { ExperimentOutlined, StopOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Badge,
  Button,
  Card,
  Checkbox,
  Collapse,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Select,
  Space,
  Spin,
  Statistic,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import type { ReactNode } from 'react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ANALYSIS_STATUS,
  analysisApi,
  buildReviseRequest,
  countPending,
  deltaColor,
  formatDelta,
  formatRatio,
  formatScore,
  MAX_REVISE_INSTRUCTION,
  priorityLabel,
  replaceChange,
  REVISE_PRESERVE_PRESETS,
  type ReviseCompare,
  SCORE_KEYS,
  SCORE_LABELS,
  SCORE_RUBRIC,
  scoreDelta,
} from '../../api/analysis'
import { errorMessage } from '../../api/client'
import { candidateLabel, readCandidates } from '../../api/foreshadows'
import { queueLabel } from '../../api/tasks'
import type { Analysis, AnalysisBrief, AnalysisChange, AnalysisScores, ReviseChapterRequest } from '../../api/types'
import type { ChapterAnalysis } from '../../hooks/useChapterAnalysis'
import { useLoader } from '../../hooks/useLoader'
import { formatTime } from '../../utils/time'
import AnalysisChangeList from './AnalysisChangeList'
import AnalysisHistory from './AnalysisHistory'

export type { ReviseCompare } from '../../api/analysis'

interface Props {
  open: boolean
  onClose: () => void
  chapterId: number
  analysis: ChapterAnalysis
  /** 项目是否开启自动分析（重写后是否会自动重新分析） */
  autoAnalyze: boolean
  /** 生成中 / 正文为空 / 没有 AI 配置等情况下禁止发起分析 */
  analyzeDisabled: boolean
  /** 由编辑器执行（含未保存修改时先自动保存） */
  onAnalyze: () => Promise<void>
  /** 禁止重写的原因；为空表示可重写 */
  reviseBlockedReason: string
  /** 编辑器中正在进行按建议重写 */
  reviseRunning: boolean
  /** 由编辑器创建并订阅重写任务（与整章生成同样的只读处理；ai_config_id 由编辑器覆盖），创建成功返回 true */
  onRevise: (body: ReviseChapterRequest) => Promise<boolean>
  compare: ReviseCompare | null
  /** 重写表单中的写作风格选择（由编辑器提供，与 AI 助手的「本次风格」同一状态） */
  reviseStyle?: ReactNode
}

/** 章节分析抽屉：状态与分析按钮、评分对比、分析结果（含建议与重写表单）、回写清单、历史与评分走势。 */
export default function AnalysisDrawer({ open, onClose, analysis, ...rest }: Props) {
  const pending = countPending(analysis.latest?.changes)
  return (
    <Drawer
      title={
        <Space>
          <span>章节分析</span>
          <Tag color={ANALYSIS_STATUS[analysis.status].color}>{ANALYSIS_STATUS[analysis.status].label}</Tag>
          {pending > 0 && <Badge count={`${pending} 项待确认`} color="gold" />}
        </Space>
      }
      width={600}
      open={open}
      onClose={onClose}
      destroyOnHidden
    >
      <AnalysisBody analysis={analysis} {...rest} />
    </Drawer>
  )
}

function AnalysisBody({
  chapterId,
  analysis,
  autoAnalyze,
  analyzeDisabled,
  onAnalyze,
  reviseBlockedReason,
  reviseRunning,
  onRevise,
  compare,
  reviseStyle,
}: Omit<Props, 'open' | 'onClose'>) {
  const { message } = App.useApp()
  const { latest, version, patchChange } = analysis
  const latestId = analysis.state?.latest_analysis_id ?? null
  const [tab, setTab] = useState('result')
  const [starting, setStarting] = useState(false)
  // null = 查看最新一次
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [other, setOther] = useState<Analysis | null>(null)
  const [otherLoading, setOtherLoading] = useState(false)

  const historyLoader = useCallback(
    () => (version >= 0 ? analysisApi.list(chapterId) : Promise.resolve<AnalysisBrief[]>([])),
    [chapterId, version],
  )
  const { data: history = [], loading: historyLoading } = useLoader(historyLoader)

  const viewingOther = selectedId !== null && selectedId !== latestId
  const detail = viewingOther ? (other && other.id === selectedId ? other : null) : latest

  useEffect(() => {
    if (!viewingOther || selectedId === null) return
    let alive = true
    analysisApi
      .get(selectedId)
      .then((a) => {
        if (alive) setOther(a)
      })
      .catch((err: unknown) => {
        if (alive) message.error(errorMessage(err))
      })
      .finally(() => {
        if (alive) setOtherLoading(false)
      })
    return () => {
      alive = false
    }
  }, [viewingOther, selectedId, message])

  const selectAnalysis = (id: number) => {
    if (id === latestId) {
      setSelectedId(null)
    } else {
      setOtherLoading(true)
      setSelectedId(id)
    }
    setTab('result')
  }

  const handleChange = (updated: AnalysisChange) => {
    patchChange(updated)
    setOther((prev) =>
      prev && prev.id === updated.analysis_id ? { ...prev, changes: replaceChange(prev.changes, updated) } : prev,
    )
  }

  const handleAnalyze = async () => {
    setStarting(true)
    try {
      await onAnalyze()
      setSelectedId(null)
    } finally {
      setStarting(false)
    }
  }

  // 与上一次（历史中更早的一次）分析的差值
  const previousScores = useMemo<AnalysisScores | null>(() => {
    if (!detail) return null
    const idx = history.findIndex((a) => a.id === detail.id)
    return idx >= 0 && idx + 1 < history.length ? history[idx + 1].scores : null
  }, [detail, history])

  const running = analysis.running
  const task = analysis.task
  const hasAnalysis = latestId !== null

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Space wrap>
        {running ? (
          <>
            <Spin size="small" />
            <Typography.Text type="secondary">
              {task?.status === 'queued' ? queueLabel(task) : task?.stage || '正在分析…'}
            </Typography.Text>
            <Button
              size="small"
              danger
              icon={<StopOutlined />}
              onClick={() => void analysis.cancel().catch((err: unknown) => message.error(errorMessage(err)))}
            >
              取消分析
            </Button>
          </>
        ) : (
          <Button
            type="primary"
            icon={<ExperimentOutlined />}
            loading={starting}
            disabled={analyzeDisabled}
            onClick={() => void handleAnalyze()}
          >
            {hasAnalysis ? '重新分析' : '分析本章'}
          </Button>
        )}
        {analysis.state?.analyzed_at && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            最近分析：{formatTime(analysis.state.analyzed_at)}
          </Typography.Text>
        )}
      </Space>
      {analysis.status === 'stale' && (
        <Alert type="warning" showIcon message="正文在分析后已修改，分析结果可能已过期，可重新分析。" />
      )}
      {analysis.error && !running && <Alert type="error" showIcon message="分析失败" description={analysis.error} />}
      <CompareCard
        compare={compare}
        history={history}
        latestId={latestId}
        analyzing={running}
        autoAnalyze={autoAnalyze}
        reviseRunning={reviseRunning}
      />
      {!hasAnalysis && !running ? (
        <Empty description="本章尚未分析。分析会提炼摘要、情节点与钩子，识别设定变化并回写，给出评分和改进建议。" />
      ) : (
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: 'result',
              label: '分析结果',
              children: (
                <Spin spinning={viewingOther && (otherLoading || !detail)}>
                  {viewingOther && detail && (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      message={`正在查看历史分析（${formatTime(detail.created_at)}）`}
                      action={
                        <Button size="small" onClick={() => setSelectedId(null)}>
                          回到最新
                        </Button>
                      }
                    />
                  )}
                  {detail ? (
                    <AnalysisResult
                      key={detail.id}
                      detail={detail}
                      previousScores={previousScores}
                      reviseBlockedReason={reviseBlockedReason}
                      reviseRunning={reviseRunning}
                      onRevise={onRevise}
                      reviseStyle={reviseStyle}
                    />
                  ) : (
                    !viewingOther && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={running ? '分析进行中…' : '加载中…'} />
                  )}
                </Spin>
              ),
            },
            {
              key: 'changes',
              label: (
                <Badge count={countPending(detail?.changes)} size="small" offset={[8, 0]}>
                  回写清单
                </Badge>
              ),
              children: detail ? (
                <AnalysisChangeList changes={detail.changes} onChanged={handleChange} />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} />
              ),
            },
            {
              key: 'history',
              label: `历史（${history.length}）`,
              children: (
                <AnalysisHistory
                  items={history}
                  loading={historyLoading}
                  selectedId={detail?.id ?? null}
                  onSelect={selectAnalysis}
                />
              ),
            },
          ]}
        />
      )}
    </Space>
  )
}

/** 重写前后评分对比：重写成功后等待新的分析完成再显示 */
function CompareCard({
  compare,
  history,
  latestId,
  analyzing,
  autoAnalyze,
  reviseRunning,
}: {
  compare: ReviseCompare | null
  history: AnalysisBrief[]
  latestId: number | null
  analyzing: boolean
  autoAnalyze: boolean
  reviseRunning: boolean
}) {
  if (reviseRunning) {
    return <Alert type="info" showIcon message="正在按建议重写，完成后正文自动覆盖并重新分析。" />
  }
  if (!compare) return null
  const fresh = latestId !== null && (compare.sinceAnalysisId === null || latestId > compare.sinceAnalysisId)
  if (!fresh) {
    return (
      <Alert
        type="info"
        showIcon
        message={
          analyzing || autoAnalyze
            ? '重写完成，正在重新分析，完成后显示前后评分对比…'
            : '重写完成。项目已关闭自动分析，点击「重新分析」后可对比前后评分。'
        }
      />
    )
  }
  const base = history.find((a) => a.id === compare.baseAnalysisId)
  const after = history.find((a) => a.id === latestId)
  if (!base || !after) return null
  const delta = scoreDelta(after.scores, base.scores)
  return (
    <Card size="small" title="按建议重写前后评分对比">
      <Space size="large" wrap>
        {SCORE_KEYS.map((key) => (
          <div key={key}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {SCORE_LABELS[key]}
            </Typography.Text>
            <div>
              {formatScore(base.scores, key)} → <Typography.Text strong>{formatScore(after.scores, key)}</Typography.Text>{' '}
              <span style={{ color: deltaColor(delta[key]), fontSize: 12 }}>{formatDelta(delta[key])}</span>
            </div>
          </div>
        ))}
      </Space>
    </Card>
  )
}

interface ResultProps {
  detail: Analysis
  previousScores: AnalysisScores | null
  reviseBlockedReason: string
  reviseRunning: boolean
  onRevise: Props['onRevise']
  reviseStyle?: ReactNode
}

/** 单次分析结果：评分（与上次差值）、指标、摘要、情节点、钩子、冲突与情感、建议 + 按建议重写表单 */
function AnalysisResult({ detail, previousScores, reviseBlockedReason, reviseRunning, onRevise, reviseStyle }: ResultProps) {
  const { message } = App.useApp()
  const delta = scoreDelta(detail.scores, previousScores)
  const m = detail.metrics
  const [checked, setChecked] = useState<number[]>([])
  const [instruction, setInstruction] = useState('')
  const [preserve, setPreserve] = useState<string[]>([])
  const [targetWords, setTargetWords] = useState<number | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const candidates = useMemo(() => readCandidates(detail.foreshadow_candidates), [detail.foreshadow_candidates])

  const handleRevise = async () => {
    const built = buildReviseRequest({
      analysisId: detail.id,
      suggestionIds: checked,
      instruction,
      preserve,
      targetWords,
      aiConfigId: null,
    })
    if (!built.ok) {
      message.error(built.error)
      return
    }
    setSubmitting(true)
    try {
      // ai_config_id 由编辑器按 AI 助手中选择的配置覆盖
      if (await onRevise(built.body)) {
        setChecked([])
        setInstruction('')
      }
    } finally {
      setSubmitting(false)
    }
  }

  const conflictTypes = detail.conflict?.types ?? []
  const emotion = detail.emotion ?? {}
  const emotionFlow = [emotion.start, emotion.middle, emotion.end].filter(Boolean).join(' → ')

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card size="small" title="评分" extra={<Typography.Text type="secondary" style={{ fontSize: 12 }}>{SCORE_RUBRIC}</Typography.Text>}>
        <Space size="large" wrap>
          {SCORE_KEYS.map((key) => (
            <Statistic
              key={key}
              title={SCORE_LABELS[key]}
              value={formatScore(detail.scores, key)}
              valueStyle={key === 'overall' ? { color: '#1677ff' } : undefined}
              suffix={
                delta[key] !== null ? (
                  <span style={{ fontSize: 12, color: deltaColor(delta[key]) }} title="与上一次分析相比">
                    {formatDelta(delta[key])}
                  </span>
                ) : undefined
              }
            />
          ))}
        </Space>
        {detail.stale && (
          <Typography.Text type="warning" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
            该次分析基于旧正文。
          </Typography.Text>
        )}
      </Card>
      <Descriptions size="small" column={4} title="客观指标" bordered layout="vertical">
        <Descriptions.Item label="字数">{m.words ?? '-'}</Descriptions.Item>
        <Descriptions.Item label="段落数">{m.paragraphs ?? '-'}</Descriptions.Item>
        <Descriptions.Item label="平均段长">{m.avg_paragraph_chars ?? '-'}</Descriptions.Item>
        <Descriptions.Item label="对白占比">{formatRatio(m.dialogue_ratio)}</Descriptions.Item>
      </Descriptions>
      <div>
        <Typography.Title level={5}>摘要</Typography.Title>
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>
          {detail.summary || <Typography.Text type="secondary">（模型没有给出摘要）</Typography.Text>}
        </Typography.Paragraph>
      </div>
      {detail.plot_points.length > 0 && (
        <div>
          <Typography.Title level={5}>情节点</Typography.Title>
          <List
            size="small"
            dataSource={detail.plot_points}
            renderItem={(p, i) => (
              <List.Item key={i}>
                <Space align="start">
                  <Tag color={p.importance >= 4 ? 'red' : p.importance >= 3 ? 'orange' : 'default'}>
                    重要度 {p.importance}
                  </Tag>
                  <span>{p.text}</span>
                </Space>
              </List.Item>
            )}
          />
        </div>
      )}
      {detail.hooks.length > 0 && (
        <div>
          <Typography.Title level={5}>钩子</Typography.Title>
          <List
            size="small"
            dataSource={detail.hooks}
            renderItem={(h, i) => (
              <List.Item key={i}>
                <Space align="start">
                  <Tag color="purple">{h.type}</Tag>
                  <Tag>强度 {h.strength}</Tag>
                  <span>{h.text}</span>
                </Space>
              </List.Item>
            )}
          />
        </div>
      )}
      {(detail.conflict?.level != null || conflictTypes.length > 0 || emotion.tone || emotionFlow) && (
        <Descriptions size="small" column={1} bordered>
          {(detail.conflict?.level != null || conflictTypes.length > 0) && (
            <Descriptions.Item label="冲突">
              {detail.conflict?.level != null && <Tag color="volcano">强度 {detail.conflict.level}</Tag>}
              {conflictTypes.map((t) => (
                <Tag key={t}>{t}</Tag>
              ))}
            </Descriptions.Item>
          )}
          {(emotion.tone || emotionFlow) && (
            <Descriptions.Item label="情感走向">
              {emotion.tone && <Tag color="blue">{emotion.tone}</Tag>}
              {emotionFlow}
            </Descriptions.Item>
          )}
        </Descriptions>
      )}
      <Card size="small" title={`改进建议（${detail.suggestions.length}）`}>
        {detail.suggestions.length === 0 ? (
          <Typography.Text type="secondary">没有改进建议。也可以直接填写修改要求进行重写。</Typography.Text>
        ) : (
          <Checkbox.Group
            value={checked}
            onChange={(v) => setChecked(v as number[])}
            disabled={reviseRunning}
            style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
          >
            {detail.suggestions.map((s) => {
              const pr = priorityLabel(s.priority)
              return (
                <Checkbox key={s.id} value={s.id}>
                  <Tag color={pr.color}>{pr.label}</Tag>
                  <Tag>{s.category}</Tag>
                  {s.text}
                </Checkbox>
              )
            })}
          </Checkbox.Group>
        )}
        <Form layout="vertical" style={{ marginTop: 12 }} disabled={reviseRunning || submitting}>
          <Form.Item label="补充修改要求（可选）" style={{ marginBottom: 8 }}>
            <Input.TextArea
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              maxLength={MAX_REVISE_INSTRUCTION}
              autoSize={{ minRows: 2, maxRows: 6 }}
              placeholder="例如：加强结尾的悬念，压缩中段的环境描写"
            />
          </Form.Item>
          <Form.Item label="需要保留的要素" style={{ marginBottom: 8 }}>
            <Select
              mode="tags"
              value={preserve}
              onChange={setPreserve}
              maxCount={10}
              placeholder="选择或输入后回车"
              options={REVISE_PRESERVE_PRESETS.map((p) => ({ value: p, label: p }))}
            />
          </Form.Item>
          <Form.Item label="目标字数（留空 = 原文字数）" style={{ marginBottom: 8 }}>
            <InputNumber
              min={300}
              max={20000}
              step={500}
              value={targetWords}
              onChange={(v) => setTargetWords(typeof v === 'number' ? v : null)}
              style={{ width: '100%' }}
            />
          </Form.Item>
          {reviseStyle && (
            <Form.Item label="写作风格" style={{ marginBottom: 8 }}>
              {reviseStyle}
            </Form.Item>
          )}
        </Form>
        {reviseBlockedReason && !reviseRunning && (
          <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
            {reviseBlockedReason}
          </Typography.Text>
        )}
        <Button
          type="primary"
          block
          loading={submitting || reviseRunning}
          disabled={Boolean(reviseBlockedReason) || (checked.length === 0 && !instruction.trim())}
          onClick={() => void handleRevise()}
        >
          {reviseRunning ? '正在按建议重写…' : `按建议重写${checked.length ? `（${checked.length} 条）` : ''}`}
        </Button>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
          重写在后台运行，完成后覆盖正文，旧稿自动存为历史版本（来源「按建议重写」），并自动重新分析以对比评分。
        </Typography.Paragraph>
      </Card>
      {candidates.length > 0 && (
        <Card size="small" title={`本章识别的伏笔（${candidates.length}）`}>
          <List
            size="small"
            dataSource={candidates}
            renderItem={(c, i) => (
              <List.Item key={i}>
                <Space direction="vertical" size={0} style={{ minWidth: 0 }}>
                  <Space size={4} wrap>
                    <Tag color={c.action === 'resolve' ? 'green' : c.ref_id !== null ? 'blue' : 'gold'}>{candidateLabel(c)}</Tag>
                    {c.keyword && <Typography.Text strong>{c.keyword}</Typography.Text>}
                    {c.expected_resolve_chapter !== null && (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        预计第 {c.expected_resolve_chapter} 章回收
                      </Typography.Text>
                    )}
                  </Space>
                  {c.text && <Typography.Text style={{ fontSize: 13 }}>{c.text}</Typography.Text>}
                </Space>
              </List.Item>
            )}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            处理结果见「回写清单」：按编号匹配的自动写入，新伏笔需确认后创建。
          </Typography.Text>
        </Card>
      )}
      {detail.warnings.length > 0 && (
        <Collapse
          size="small"
          items={[
            {
              key: 'warnings',
              label: `解析提示（${detail.warnings.length}）`,
              children: (
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {detail.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              ),
            },
          ]}
        />
      )}
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        模型：{detail.model || '-'} · {formatTime(detail.created_at)}
      </Typography.Text>
    </Space>
  )
}
