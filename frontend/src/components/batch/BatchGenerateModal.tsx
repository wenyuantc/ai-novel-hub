import { Alert, App, Checkbox, Form, Input, InputNumber, List, Modal, Select, Space, Spin, Tag, Typography } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import { previewBatch, startBatch } from '../../api/chapters'
import { errorMessage } from '../../api/client'
import type { BatchGenerateRequest, BatchPreview, ChapterBrief, StyleChoice, Task } from '../../api/types'
import { useHasAIConfig } from '../../hooks/useHasAIConfig'
import { defaultBatchRange, endIdUpTo, formatEta, rangeChapters, sortChapters } from '../../utils/batch'
import { useProjectStore } from '../../stores/project'
import AIConfigSelect from '../AIConfigSelect'
import StyleChoiceSelect from '../styles/StyleChoiceSelect'

interface Props {
  projectId: number
  chapters: ChapterBrief[]
  onClose: () => void
  /** 创建成功（202 TaskOut） */
  onStarted: (task: Task) => void
}

interface FormValues {
  start_chapter_id: number
  end_chapter_id: number
  include_written: boolean
  target_words: number
  use_plan_words: boolean
  auto_continue: boolean
  review_every: number
  skip_on_fail: boolean
  instruction: string
  ai_config_id: number | null
  style: StyleChoice
}

/** 参数变化后多久请求预览 */
const PREVIEW_DEBOUNCE_MS = 400

function toRequest(v: Partial<FormValues>): BatchGenerateRequest | null {
  if (!v.start_chapter_id || !v.end_chapter_id) return null
  const words = v.target_words ?? 3000
  if (words < 300 || words > 20000) return null
  return {
    start_chapter_id: v.start_chapter_id,
    end_chapter_id: v.end_chapter_id,
    include_written: !!v.include_written,
    target_words: words,
    use_plan_words: v.use_plan_words ?? true,
    auto_continue: v.auto_continue ?? true,
    review_every: Math.max(0, Math.min(50, v.review_every ?? 0)),
    skip_on_fail: !!v.skip_on_fail,
    instruction: v.instruction?.trim() ?? '',
    ai_config_id: v.ai_config_id ?? null,
    style: v.style ?? 'inherit',
  }
}

/** 批量顺序生成发起弹窗：选择范围与选项，参数变化时防抖预览（将生成的章节、跳过数、预计耗时、警告），确认后创建任务。 */
export default function BatchGenerateModal({ projectId, chapters, onClose, onStarted }: Props) {
  const { message } = App.useApp()
  const [form] = Form.useForm<FormValues>()
  const hasConfig = useHasAIConfig()
  const projectDefaultStyle = useProjectStore((s) => (s.project?.id === projectId ? s.project.default_style : null))
  const styleContext = useMemo(() => ({ projectDefault: projectDefaultStyle }), [projectDefaultStyle])
  const sorted = useMemo(() => sortChapters(chapters), [chapters])
  const range = useMemo(() => defaultBatchRange(chapters), [chapters])
  const values = Form.useWatch([], form) as Partial<FormValues> | undefined
  // 预览结果按请求键保存：键变化后旧结果自动失效（不在 effect 里同步清空）
  const [previewState, setPreviewState] = useState<{ key: string; preview: BatchPreview | null; error: string | null } | null>(
    null,
  )
  const [starting, setStarting] = useState(false)
  const seqRef = useRef(0)

  const request = useMemo(() => (values ? toRequest(values) : null), [values])
  // 预览只依赖影响范围与字数的字段（附加要求 / 模型等变化不重新预览）
  const previewKey = request
    ? JSON.stringify([
        request.start_chapter_id,
        request.end_chapter_id,
        request.include_written,
        request.target_words,
        request.use_plan_words,
      ])
    : ''

  useEffect(() => {
    if (!previewKey) return
    const [start_chapter_id, end_chapter_id, include_written, target_words, use_plan_words] = JSON.parse(previewKey) as [
      number,
      number,
      boolean,
      number,
      boolean,
    ]
    const seq = ++seqRef.current
    const timer = setTimeout(() => {
      previewBatch(projectId, { start_chapter_id, end_chapter_id, include_written, target_words, use_plan_words })
        .then((p) => {
          if (seq === seqRef.current) setPreviewState({ key: previewKey, preview: p, error: null })
        })
        .catch((err) => {
          if (seq === seqRef.current) setPreviewState({ key: previewKey, preview: null, error: errorMessage(err) })
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [projectId, previewKey])

  const fresh = previewKey !== '' && previewState?.key === previewKey
  const preview = fresh ? previewState.preview : null
  const previewError = fresh ? previewState.error : null
  const previewing = previewKey !== '' && !fresh

  const handleOk = async () => {
    const v = await form.validateFields()
    const body = toRequest(v)
    if (!body) return
    setStarting(true)
    try {
      const task = await startBatch(projectId, body)
      message.success('已开始批量生成，可在章节列表顶部或任务面板查看进度')
      onStarted(task)
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setStarting(false)
    }
  }

  const chapterOptions = sorted.map((c) => ({
    value: c.id,
    label: `第 ${c.chapter_no} 章 ${c.title || '（未命名）'}${c.word_count > 0 ? `（已写 ${c.word_count} 字）` : ''}`,
  }))
  const inRange = rangeChapters(chapters, values?.start_chapter_id, values?.end_chapter_id)
  const startNo = sorted.find((c) => c.id === values?.start_chapter_id)?.chapter_no
  const endNo = sorted.find((c) => c.id === values?.end_chapter_id)?.chapter_no
  const rangeInvalid = startNo !== undefined && endNo !== undefined && startNo > endNo
  const nothing = preview !== null && preview.items.length === 0

  return (
    <Modal
      open
      title="批量生成章节"
      width={760}
      okText="开始批量生成"
      cancelText="取消"
      onOk={() => void handleOk()}
      onCancel={onClose}
      confirmLoading={starting}
      okButtonProps={{ disabled: hasConfig === false || rangeInvalid || nothing || !request }}
      maskClosable={false}
      destroyOnHidden
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          start_chapter_id: range?.startId,
          end_chapter_id: range?.endId,
          include_written: false,
          target_words: 3000,
          use_plan_words: true,
          auto_continue: true,
          review_every: 0,
          skip_on_fail: false,
          instruction: '',
          ai_config_id: null,
          style: 'inherit',
        }}
      >
        <Space size="middle" wrap align="start">
          <Form.Item name="start_chapter_id" label="起始章" rules={[{ required: true, message: '请选择起始章' }]}>
            <Select options={chapterOptions} style={{ width: 260 }} showSearch optionFilterProp="label" />
          </Form.Item>
          <Form.Item name="end_chapter_id" label="结束章" rules={[{ required: true, message: '请选择结束章' }]}>
            <Select options={chapterOptions} style={{ width: 260 }} showSearch optionFilterProp="label" />
          </Form.Item>
          <Form.Item label="生成到第 N 章">
            <InputNumber
              min={1}
              placeholder="章节号"
              style={{ width: 120 }}
              onChange={(n) => {
                if (typeof n !== 'number') return
                const id = endIdUpTo(chapters, n)
                if (id !== null) form.setFieldValue('end_chapter_id', id)
              }}
            />
          </Form.Item>
        </Space>
        {rangeInvalid && <Alert type="error" showIcon message="起始章节不能晚于结束章节" style={{ marginBottom: 12 }} />}
        {!rangeInvalid && inRange.length > 200 && (
          <Alert type="error" showIcon message="一次最多批量生成 200 章，请缩小范围" style={{ marginBottom: 12 }} />
        )}
        <Space size="large" wrap align="start">
          <Form.Item name="target_words" label="统一字数" rules={[{ required: true, message: '请输入字数' }]}>
            <InputNumber min={300} max={20000} step={500} style={{ width: 140 }} />
          </Form.Item>
          <Form.Item
            name="review_every"
            label="审核间隔（章）"
            tooltip="每生成 N 章暂停为「待审核」，检查修改后点继续；0 = 不暂停"
          >
            <InputNumber min={0} max={50} style={{ width: 120 }} />
          </Form.Item>
        </Space>
        <Space size="large" wrap>
          <Form.Item name="use_plan_words" valuePropName="checked" style={{ marginBottom: 8 }}>
            <Checkbox>有细纲的章节使用细纲字数</Checkbox>
          </Form.Item>
          <Form.Item name="include_written" valuePropName="checked" style={{ marginBottom: 8 }}>
            <Checkbox>包含已写章节（重写并自动存版本）</Checkbox>
          </Form.Item>
          <Form.Item name="auto_continue" valuePropName="checked" style={{ marginBottom: 8 }}>
            <Checkbox>字数不足时自动续写</Checkbox>
          </Form.Item>
          <Form.Item name="skip_on_fail" valuePropName="checked" style={{ marginBottom: 8 }}>
            <Checkbox>失败时跳过继续</Checkbox>
          </Form.Item>
        </Space>
        <Form.Item name="instruction" label="附加要求（可选，每章通用）" rules={[{ max: 2000, message: '不超过 2000 字' }]}>
          <Input.TextArea autoSize={{ minRows: 2, maxRows: 5 }} placeholder="例如：保持紧凑节奏，多用对话推进" />
        </Form.Item>
        <Space size="large" wrap align="start" style={{ width: '100%' }}>
          <Form.Item name="ai_config_id" label="AI 配置" style={{ minWidth: 300 }}>
            <AIConfigSelect />
          </Form.Item>
          <Form.Item
            name="style"
            label="写作风格"
            tooltip="按章节或项目默认：每章按自己的本章风格，未设置的用项目默认；也可以指定一个风格用于全部章节。续跑 / 重试沿用这次的选择"
            style={{ minWidth: 300 }}
          >
            <StyleChoiceSelect
              context={styleContext}
              inheritLabel={`按章节或项目默认${projectDefaultStyle ? `（项目默认「${projectDefaultStyle.name}」）` : ''}`}
            />
          </Form.Item>
        </Space>
      </Form>
      <Spin spinning={previewing}>
        {previewError && <Alert type="error" showIcon message={previewError} />}
        {preview && (
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            <Space wrap>
              <Typography.Text>
                将生成 <Typography.Text strong>{preview.items.length}</Typography.Text> 章
              </Typography.Text>
              {preview.skipped.length > 0 && (
                <Typography.Text type="secondary">跳过已写 {preview.skipped.length} 章</Typography.Text>
              )}
              <Typography.Text type="secondary">预计耗时 {formatEta(preview.estimated_minutes)}</Typography.Text>
              <Tag color={preview.analyze ? 'blue' : 'default'}>{preview.analyze ? '每章生成后分析' : '不分析'}</Tag>
            </Space>
            {preview.warnings.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={
                  <ul style={{ margin: 0, paddingLeft: 18 }}>
                    {preview.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                }
              />
            )}
            {preview.items.length > 0 && (
              <div style={{ maxHeight: 220, overflow: 'auto', border: '1px solid #f0f0f0', borderRadius: 6 }}>
                <List
                  size="small"
                  dataSource={preview.items}
                  renderItem={(it) => (
                    <List.Item key={it.chapter_id}>
                      <Space wrap size={6}>
                        <Typography.Text strong>第 {it.chapter_no} 章</Typography.Text>
                        <Typography.Text>{it.title || '（未命名）'}</Typography.Text>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          目标 {it.target_words} 字（{it.words_source === 'plan' ? '细纲' : '统一字数'}）
                        </Typography.Text>
                        {it.written && <Tag color="orange">重写</Tag>}
                      </Space>
                    </List.Item>
                  )}
                />
              </div>
            )}
            {preview.skipped.length > 0 && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                跳过的已写章节：{preview.skipped.map((s) => `第 ${s.chapter_no} 章`).join('、')}
              </Typography.Text>
            )}
          </Space>
        )}
      </Spin>
    </Modal>
  )
}
