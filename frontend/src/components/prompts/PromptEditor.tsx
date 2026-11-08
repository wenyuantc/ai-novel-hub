import { HistoryOutlined, LockOutlined, SwapOutlined, UndoOutlined } from '@ant-design/icons'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Input,
  Popconfirm,
  Result,
  Row,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { CSSProperties } from 'react'
import { useCallback, useEffect, useState } from 'react'
import { errorMessage } from '../../api/client'
import { promptApi } from '../../api/prompts'
import type { PromptPreview, PromptTemplateDetail, TemplateIssue, TemplatePart, TemplateVar } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { formatIssue, issuesForPart, issuesFromError, lineRange, PART_LABELS, templateBadges } from '../../utils/prompts'
import { formatTime } from '../../utils/time'
import { componentRef, insertSnippet } from '../../utils/promptComponents'
import { ComponentInsertSelect, ComponentRefTags } from '../promptComponents/ComponentRefs'
import PromptDiffModal from './PromptDiffModal'
import PromptPreviewPane from './PromptPreviewPane'
import PromptThreeWayModal from './PromptThreeWayModal'
import PromptVersionDrawer from './PromptVersionDrawer'

interface Props {
  templateKey: string
  /** 覆盖状态变化（保存 / 停用 / 恢复默认 / 确认新默认）后刷新列表徽标 */
  onChanged: () => void
  onDirtyChange: (dirty: boolean) => void
}

/** 预览防抖（毫秒） */
const PREVIEW_DEBOUNCE_MS = 600

const mono: CSSProperties = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
  fontSize: 13,
  lineHeight: 1.6,
}

const KIND_LABELS: Record<string, string> = { text: '文本', int: '整数', bool: '布尔', list: '列表' }

/** 单个模板的编辑器：加载详情后交给 Inner；保存 / 停用 / 确认新默认不重置编辑内容，回滚 / 恢复默认以服务端内容替换编辑框。 */
export default function PromptEditor({ templateKey, onChanged, onDirtyChange }: Props) {
  const loader = useCallback(() => promptApi.get(templateKey), [templateKey])
  const { data: detail, loading, error, setData } = useLoader(loader)
  if (loading && !detail) return <Spin style={{ display: 'block', margin: '80px auto' }} />
  if (error || !detail) return <Result status="warning" title={error ?? '加载失败'} />
  return (
    <EditorInner
      key={detail.key}
      detail={detail}
      onDetail={(d) => {
        setData(() => d)
        onChanged()
      }}
      onDirtyChange={onDirtyChange}
    />
  )
}

interface InnerProps {
  detail: PromptTemplateDetail
  onDetail: (d: PromptTemplateDetail) => void
  onDirtyChange: (dirty: boolean) => void
}

function EditorInner({ detail, onDetail, onDirtyChange }: InnerProps) {
  const { message } = App.useApp()
  const saved = { system: detail.override?.system ?? detail.default_system, user: detail.override?.user ?? detail.default_user }
  const [system, setSystem] = useState(saved.system)
  const [user, setUser] = useState(saved.user)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [issues, setIssues] = useState<TemplateIssue[]>([])
  const [preview, setPreview] = useState<PromptPreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [diffOpen, setDiffOpen] = useState(false)
  const [threeWayOpen, setThreeWayOpen] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  // 最近聚焦的段与各段光标位置（UTF-16），点击变量时插入到这里
  const [lastFocused, setLastFocused] = useState<TemplatePart>('user')
  const [carets, setCarets] = useState<Record<TemplatePart, [number, number] | null>>({ system: null, user: null })

  const dirty = system !== saved.system || user !== saved.user
  const ov = detail.override
  // 只有用户段的模板（chapter.continue / style.block）：系统段为空时不显示
  const showSystem = detail.default_system !== '' || system !== ''

  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])
  useEffect(() => () => onDirtyChange(false), [onDirtyChange])

  // 防抖实时预览：校验问题随预览返回（与保存时同一套校验）
  useEffect(() => {
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      setPreviewLoading(true)
      promptApi
        .preview(detail.key, { system, user }, controller.signal)
        .then((p) => {
          setPreview(p)
          setIssues(p.errors)
          setPreviewError(null)
        })
        .catch((err: unknown) => {
          if (!controller.signal.aborted) setPreviewError(errorMessage(err))
        })
        .finally(() => {
          if (!controller.signal.aborted) setPreviewLoading(false)
        })
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [detail.key, system, user])

  /** 以服务端内容替换编辑框（回滚 / 恢复默认） */
  const replaceText = (d: PromptTemplateDetail) => {
    setSystem(d.override?.system ?? d.default_system)
    setUser(d.override?.user ?? d.default_user)
    setNote('')
  }

  const textOf = (part: TemplatePart) => (part === 'system' ? system : user)
  const setTextOf = (part: TemplatePart, v: string) => (part === 'system' ? setSystem(v) : setUser(v))

  const textareaOf = (part: TemplatePart) => {
    const el = document.getElementById(`prompt-part-${part}`)
    return el instanceof HTMLTextAreaElement ? el : null
  }

  const syncCaret = (part: TemplatePart, el: HTMLTextAreaElement) => {
    const next: [number, number] = [el.selectionStart, el.selectionEnd]
    setCarets((prev) => (prev[part]?.[0] === next[0] && prev[part]?.[1] === next[1] ? prev : { ...prev, [part]: next }))
  }

  const jumpTo = (issue: TemplateIssue) => {
    if (!issue.part) return
    const el = textareaOf(issue.part)
    if (!el) return
    const [start, end] = issue.line ? lineRange(textOf(issue.part), issue.line) : [0, 0]
    el.focus()
    el.setSelectionRange(start, end)
    el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }

  /** 在最近聚焦段的光标处插入片段（没有光标时插在末尾） */
  const insertText = (snippet: string) => {
    const part = showSystem ? lastFocused : 'user'
    const next = insertSnippet(textOf(part), snippet, carets[part])
    setTextOf(part, next.text)
    setCarets((prev) => ({ ...prev, [part]: [next.caret, next.caret] }))
    requestAnimationFrame(() => {
      const el = textareaOf(part)
      if (!el) return
      el.focus()
      el.setSelectionRange(next.caret, next.caret)
    })
  }

  const insertVar = (v: TemplateVar) => insertText(`{{ ${v.name} }}`)

  const run = async (name: string, fn: () => Promise<void>) => {
    setBusy(name)
    try {
      await fn()
    } catch (err) {
      const found = issuesFromError(err)
      if (found.length) {
        setIssues(found)
        message.error('模板校验失败，请按提示修改')
      } else {
        message.error(errorMessage(err))
      }
    } finally {
      setBusy(null)
    }
  }

  const handleSave = () =>
    run('save', async () => {
      const d = await promptApi.save(detail.key, { system, user, note: note.trim() })
      message.success(ov ? '已保存' : '已保存为我的覆盖，之后的 AI 调用将使用它')
      setNote('')
      onDetail(d)
    })

  const handleReset = () =>
    run('reset', async () => {
      await promptApi.reset(detail.key)
      message.success('已恢复系统默认')
      const d = await promptApi.get(detail.key)
      replaceText(d)
      onDetail(d)
    })

  const handleToggle = (enabled: boolean) =>
    run('toggle', async () => {
      const d = await promptApi.patch(detail.key, { enabled })
      message.success(enabled ? '已启用覆盖' : '已停用覆盖，AI 调用改用系统默认（覆盖内容保留）')
      onDetail(d)
    })

  const handleAck = () =>
    run('ack', async () => {
      const d = await promptApi.patch(detail.key, { ack_default: true })
      message.success('已确认新默认')
      onDetail(d)
    })

  const partEditor = (part: TemplatePart) => {
    const list = issuesForPart(issues, part)
    return (
      <div>
        <Typography.Text strong>{PART_LABELS[part]}</Typography.Text>
        <Typography.Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
          {part === 'system' ? '系统消息（为空则不发送）' : '用户消息'} · {Array.from(textOf(part)).length} / 20000 字
        </Typography.Text>
        <Input.TextArea
          id={`prompt-part-${part}`}
          value={textOf(part)}
          onChange={(e) => {
            setTextOf(part, e.target.value)
            syncCaret(part, e.target)
          }}
          onFocus={() => setLastFocused(part)}
          onSelect={(e) => syncCaret(part, e.currentTarget)}
          onKeyUp={(e) => syncCaret(part, e.currentTarget)}
          onMouseUp={(e) => syncCaret(part, e.currentTarget)}
          status={list.length ? 'error' : undefined}
          autoSize={{ minRows: part === 'system' ? 4 : 10, maxRows: 28 }}
          spellCheck={false}
          style={{ ...mono, marginTop: 4 }}
        />
        {list.length > 0 && (
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {list.map((i, idx) => (
              <li key={idx}>
                <Typography.Link type="danger" onClick={() => jumpTo(i)}>
                  {formatIssue(i)}
                </Typography.Link>
              </li>
            ))}
          </ul>
        )}
      </div>
    )
  }

  const general = issues.filter((i) => !i.part)

  return (
    <Card
      title={
        <Space wrap>
          {detail.name}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {detail.key}
          </Typography.Text>
          {templateBadges(detail).map((b) => (
            <Tag key={b.label} color={b.color}>
              {b.label}
            </Tag>
          ))}
          {dirty && <Tag color="gold">未保存</Tag>}
        </Space>
      }
      extra={
        <Space wrap>
          {ov && (
            <Tooltip title="停用后 AI 调用使用系统默认，覆盖内容保留">
              <Space size={4}>
                <Switch
                  size="small"
                  checked={ov.enabled}
                  loading={busy === 'toggle'}
                  onChange={(v) => void handleToggle(v)}
                />
                <Typography.Text>{ov.enabled ? '覆盖已启用' : '覆盖已停用'}</Typography.Text>
              </Space>
            </Tooltip>
          )}
          <Button icon={<SwapOutlined />} onClick={() => setDiffOpen(true)}>
            与系统默认对比
          </Button>
          {ov && (
            <Button icon={<HistoryOutlined />} onClick={() => setVersionsOpen(true)}>
              历史版本
            </Button>
          )}
          {ov && (
            <Popconfirm
              title="恢复系统默认？"
              description="会删除我的覆盖及全部历史版本，之后的 AI 调用使用系统默认。"
              okText="恢复默认"
              cancelText="取消"
              onConfirm={() => void handleReset()}
            >
              <Button danger icon={<UndoOutlined />} loading={busy === 'reset'}>
                恢复默认
              </Button>
            </Popconfirm>
          )}
        </Space>
      }
    >
      <Space direction="vertical" style={{ width: '100%' }} size="middle">
        {detail.description && <Typography.Text type="secondary">{detail.description}</Typography.Text>}
        {ov && detail.outdated && (
          <Alert
            type="warning"
            showIcon
            message="系统默认已更新"
            description="你保存覆盖之后，这个模板的系统默认有了变化。可以查看旧默认、新默认与我的版本的差异，把需要的改动合并进来；确认后不再提示。"
            action={
              <Space direction="vertical">
                <Button size="small" onClick={() => setThreeWayOpen(true)}>
                  查看三方对比
                </Button>
                <Button size="small" type="primary" loading={busy === 'ack'} onClick={() => void handleAck()}>
                  确认已阅
                </Button>
              </Space>
            }
          />
        )}
        {ov && !ov.enabled && (
          <Alert type="info" showIcon message="覆盖已停用：AI 调用当前使用系统默认，重新启用后恢复使用我的版本。" />
        )}
        {!ov && <Alert type="info" showIcon message="当前使用系统默认。修改后保存即成为我的覆盖，对我的全部项目生效。" />}
        <Row gutter={16}>
          <Col xs={24} xl={14}>
            <Space direction="vertical" style={{ width: '100%' }}>
              {showSystem ? (
                partEditor('system')
              ) : (
                <Typography.Text type="secondary">该模板只使用用户段。</Typography.Text>
              )}
              {partEditor('user')}
              {general.map((i, idx) => (
                <Alert key={idx} type="error" showIcon message={formatIssue(i)} />
              ))}
              <Space wrap size={8}>
                {/* 预览按编辑器当前内容解析引用；预览还没返回时用已保存覆盖的引用 */}
                <ComponentRefTags components={preview?.components ?? detail.components ?? []} />
                <Tooltip title="在最近编辑段的光标处插入 {{> 标识 }}，保存时按原样展开组件内容再校验">
                  <span>
                    <ComponentInsertSelect onInsert={(slug) => insertText(componentRef(slug))} />
                  </span>
                </Tooltip>
              </Space>
              <Space wrap>
                <Input
                  placeholder="版本备注（可选）"
                  value={note}
                  maxLength={200}
                  onChange={(e) => setNote(e.target.value)}
                  style={{ width: 240 }}
                />
                <Button type="primary" disabled={!dirty} loading={busy === 'save'} onClick={() => void handleSave()}>
                  保存
                </Button>
                <Button
                  disabled={!dirty}
                  onClick={() => {
                    setSystem(saved.system)
                    setUser(saved.user)
                  }}
                >
                  放弃修改
                </Button>
                {ov && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    上次保存：{formatTime(ov.updated_at)}
                  </Typography.Text>
                )}
              </Space>
              <Table<TemplateVar>
                size="small"
                rowKey="name"
                pagination={false}
                dataSource={detail.variables}
                title={() => '可用变量（点击变量名插入到光标处）'}
                columns={[
                  {
                    title: '变量',
                    render: (_, v) => (
                      <Tooltip title={`插入 {{ ${v.name} }}`}>
                        <Typography.Link code onClick={() => insertVar(v)}>
                          {v.name}
                        </Typography.Link>
                      </Tooltip>
                    ),
                  },
                  { title: '说明', dataIndex: 'label' },
                  { title: '类型', width: 64, render: (_, v) => KIND_LABELS[v.kind] ?? v.kind },
                  {
                    title: '约束',
                    width: 130,
                    render: (_, v) => (
                      <Space size={4}>
                        {v.required && <Tag color="red">必需</Tag>}
                        {v.locked && (
                          <Tooltip title="内容由系统提供：必须以 {{ 变量 }} 原样出现且只出现一次，不能放进 if / for，位置可以调整">
                            <Tag icon={<LockOutlined />}>锁定</Tag>
                          </Tooltip>
                        )}
                      </Space>
                    ),
                  },
                ]}
              />
            </Space>
          </Col>
          <Col xs={24} xl={10}>
            <div style={{ position: 'sticky', top: 16 }}>
              <PromptPreviewPane preview={preview} loading={previewLoading} error={previewError} />
            </div>
          </Col>
        </Row>
      </Space>
      <PromptDiffModal
        open={diffOpen}
        title="系统默认 → 编辑器当前内容"
        hint="删除（红）为系统默认中有、当前没有的内容；新增（绿）为我加的内容。"
        pairs={[
          { label: '系统段', a: detail.default_system, b: system },
          { label: '用户段', a: detail.default_user, b: user },
        ]}
        onClose={() => setDiffOpen(false)}
      />
      {ov && (
        <PromptThreeWayModal
          open={threeWayOpen}
          parts={[
            { label: '系统段', oldDefault: ov.base_system, newDefault: detail.default_system, mine: system },
            { label: '用户段', oldDefault: ov.base_user, newDefault: detail.default_user, mine: user },
          ]}
          onClose={() => setThreeWayOpen(false)}
        />
      )}
      <PromptVersionDrawer
        templateKey={detail.key}
        open={versionsOpen}
        current={{ system, user }}
        dirty={dirty}
        version={ov?.updated_at ?? null}
        onClose={() => setVersionsOpen(false)}
        onRestored={(d) => {
          setVersionsOpen(false)
          replaceText(d)
          onDetail(d)
        }}
      />
    </Card>
  )
}
