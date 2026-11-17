import { Alert, Descriptions, Form, Modal, Radio, Select, Spin, Typography } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import { errorMessage } from '../../api/client'
import { consistencyApi, type CheckMode, type CheckPreview } from '../../api/consistency'
import type { ChapterBrief, Task } from '../../api/types'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { useModalWidth } from '../../hooks/useIsMobile'
import { checkScopeBody, isCheckConflict } from '../../utils/consistency'
import AIConfigSelect from '../AIConfigSelect'

interface Props {
  projectId: number
  chapters: readonly ChapterBrief[]
  /** 调用方的授权作用域仍有效（切换作品、撤权后迟到的响应不得注册任务） */
  isCurrent: () => boolean
  onClose: () => void
  /** 创建成功（202 TaskOut） */
  onStarted: (task: Task) => void
  /** 已有进行中的检查：调用方刷新概要以接上该任务 */
  onConflict: () => void
  /** 预填的起止章节（从「修改影响」页签快捷发起）；给出时默认按范围检查，仍可改回全书 */
  initialRange?: { startChapterId: number; endChapterId: number } | null
}

const PREVIEW_DEBOUNCE_MS = 300

/** 发起检查：选择全书或起止章节 → 预览（将调用模型的章数、可复用、空章与警告）→ 确认创建后台任务。 */
export default function CheckModal({ projectId, chapters, isCurrent, onClose, onStarted, onConflict, initialRange }: Props) {
  const width = useModalWidth(560)
  const hasConfig = useHasAIConfig()
  const sorted = useMemo(() => [...chapters].sort((a, b) => a.chapter_no - b.chapter_no || a.id - b.id), [chapters])
  const [mode, setMode] = useState<CheckMode>(initialRange ? 'range' : 'all')
  const [startId, setStartId] = useState<number | null>(initialRange?.startChapterId ?? sorted[0]?.id ?? null)
  const [endId, setEndId] = useState<number | null>(initialRange?.endChapterId ?? sorted[sorted.length - 1]?.id ?? null)
  const [aiConfigId, setAiConfigId] = useState<number | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState('')
  // 预览结果按请求键保存：范围变化后旧结果自动失效
  const [previewState, setPreviewState] = useState<{ key: string; preview: CheckPreview | null; error: string } | null>(null)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  const scope = useMemo(() => checkScopeBody({ mode, startChapterId: startId, endChapterId: endId }, sorted), [mode, startId, endId, sorted])
  const previewKey = 'body' in scope ? JSON.stringify(scope.body) : ''

  useEffect(() => {
    if (!previewKey) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      consistencyApi.preview(projectId, JSON.parse(previewKey) as Parameters<typeof consistencyApi.preview>[1], controller.signal).then(
        (preview) => { if (!controller.signal.aborted) setPreviewState({ key: previewKey, preview, error: '' }) },
        (err: unknown) => { if (!controller.signal.aborted) setPreviewState({ key: previewKey, preview: null, error: errorMessage(err) }) },
      )
    }, PREVIEW_DEBOUNCE_MS)
    return () => { clearTimeout(timer); controller.abort() }
  }, [projectId, previewKey])

  const fresh = previewKey !== '' && previewState?.key === previewKey
  const preview = fresh ? previewState.preview : null
  const previewError = fresh ? previewState.error : ''
  const previewing = previewKey !== '' && !fresh
  const nothingToCheck = !!preview && preview.to_extract + preview.reusable === 0

  const start = async () => {
    if (!('body' in scope) || !preview || !isCurrent()) return
    setStarting(true)
    setStartError('')
    try {
      const task = await consistencyApi.start(projectId, { ...scope.body, ai_config_id: aiConfigId })
      if (alive.current && isCurrent()) onStarted(task)
    } catch (err) {
      if (!alive.current || !isCurrent()) return
      setStartError(errorMessage(err))
      if (isCheckConflict(err)) onConflict()
    } finally {
      if (alive.current) setStarting(false)
    }
  }

  const chapterOptions = sorted.map((chapter) => ({ value: chapter.id, label: `第 ${chapter.chapter_no} 章 · ${chapter.title || '未命名'}` }))
  return <Modal
    open title="发起一致性检查" width={width} okText="开始检查" cancelText="取消" onCancel={onClose} onOk={() => void start()}
    confirmLoading={starting} maskClosable={!starting}
    okButtonProps={{ disabled: !preview || nothingToCheck || hasConfig === false }}
  >
    <Form layout="vertical">
      <Form.Item label="检查范围">
        <Radio.Group value={mode} onChange={(event) => setMode(event.target.value as CheckMode)} options={[
          { value: 'all', label: '全书' },
          { value: 'range', label: '起止章节', disabled: sorted.length === 0 },
        ]} />
      </Form.Item>
      {mode === 'range' && <>
        <Form.Item label="起始章节" htmlFor="consistency-range-start">
          <Select id="consistency-range-start" showSearch optionFilterProp="label" value={startId} onChange={setStartId} options={chapterOptions} placeholder="选择起始章节" />
        </Form.Item>
        <Form.Item label="结束章节" htmlFor="consistency-range-end" extra="按章号两端包含。只重新抽取范围内的章节；比对时会参考范围外已有的最新台账。">
          <Select id="consistency-range-end" showSearch optionFilterProp="label" value={endId} onChange={setEndId} options={chapterOptions} placeholder="选择结束章节" />
        </Form.Item>
      </>}
      <Form.Item label="模型" htmlFor="consistency-ai-config">
        <AIConfigSelect id="consistency-ai-config" value={aiConfigId} onChange={setAiConfigId} disabled={starting} />
      </Form.Item>
    </Form>
    {'error' in scope && <Alert type="error" showIcon message={scope.error} style={{ marginBottom: 12 }} />}
    {previewing && <div style={{ textAlign: 'center', padding: 12 }}><Spin tip="正在预览范围…"><div style={{ height: 32 }} /></Spin></div>}
    {previewError && <Alert type="error" showIcon message="预览失败" description={previewError} style={{ marginBottom: 12 }} />}
    {preview && <>
      <Descriptions size="small" bordered column={1} aria-label="检查预览" items={[
        { key: 'total', label: '范围内章节', children: `${preview.total} 章` },
        { key: 'extract', label: '将调用模型抽取事实', children: <Typography.Text strong>{preview.to_extract} 章</Typography.Text> },
        { key: 'reuse', label: '复用已有台账（不调用模型）', children: `${preview.reusable} 章` },
        { key: 'empty', label: '空章（不参与检查）', children: `${preview.empty} 章` },
      ]} />
      {preview.warnings.length > 0 && <Alert type="warning" showIcon style={{ marginTop: 12 }} message="请注意"
        description={<ul style={{ margin: 0, paddingInlineStart: 18 }}>{preview.warnings.map((text) => <li key={text}>{text}</li>)}</ul>} />}
      <Typography.Paragraph type="secondary" style={{ margin: '12px 0 0' }}>
        抽取之后还会分批调用模型做跨章比对；不提供费用估算。出站内容只有章节正文（遵守作者区块的 AI 可见设置）、事实台账与允许进入上下文的伏笔记录。检查不会改动正文与设定。
      </Typography.Paragraph>
    </>}
    {startError && <Alert type="error" showIcon message="未能发起检查" description={startError} style={{ marginTop: 12 }} />}
  </Modal>
}
