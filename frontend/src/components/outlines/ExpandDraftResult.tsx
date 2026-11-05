import { ArrowDownOutlined, ArrowUpOutlined, DeleteOutlined } from '@ant-design/icons'
import { Alert, App, Button, Card, Checkbox, Empty, Space, Spin, Tag, theme, Typography } from 'antd'
import { lazy, Suspense, useCallback, useMemo, useState } from 'react'
import { moveItem } from '../../api/careers'
import { errorMessage } from '../../api/client'
import {
  buildExpandSync,
  keptPlans,
  outlineProApi,
  parseExpandResult,
  planDraftError,
  replacedPlans,
  toEditablePlanDrafts,
  type EditablePlanDraft,
} from '../../api/outlines'
import { characterApi } from '../../api/story'
import type { OutlineExpandResult } from '../../api/types'
import { readPlanningConflict, type PlanningMergeConflict } from '../../utils/planning'
import { useProjectAccess } from '../../hooks/useProjectAccess'
import { useLoader } from '../../hooks/useLoader'
import type { TaskResultViewerProps } from '../entities/types'
import PlanFieldsEditor from './PlanFieldsEditor'

const PlanningMergeModal = lazy(() => import('../planning/PlanningMergeModal'))

/**
 * outline.expand 结果：有正文的细纲标「已有正文，保留」（置灰、不可删除，提交时带 id），其后是可编辑的新细纲草稿
 * （勾选 / 删除 / 上下移）；确认后 PUT /outlines/{oid}/plans 整体同步。没有正文的旧细纲被替换（已建的空章节一并删除）。
 * 保留项以**最新**细纲为准（结果可能是很久以前的任务）。
 */
export default function ExpandDraftResult({ projectId, result, onSaved }: TaskResultViewerProps) {
  const parsed = parseExpandResult(result)
  if (!parsed) return <Empty description="结果格式不正确，无法预览" />
  if (parsed.drafts.length === 0) return <Empty description="没有可用的细纲草稿" />
  return <Editor projectId={projectId} parsed={parsed} onSaved={onSaved} />
}

function Editor({ projectId, parsed, onSaved }: { projectId: number; parsed: OutlineExpandResult; onSaved?: () => void }) {
  const { token } = theme.useToken()
  const { message } = App.useApp()
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const [merge, setMerge] = useState<PlanningMergeConflict>()
  const oid = parsed.outline_id
  const loader = useCallback(
    () => Promise.all([outlineProApi.plans(projectId, oid), characterApi.list(projectId)]),
    [projectId, oid],
  )
  const { data, loading, error } = useLoader(loader)
  const [drafts, setDrafts] = useState<EditablePlanDraft[]>(() => toEditablePlanDrafts(parsed.drafts))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [building, setBuilding] = useState(false)
  const [built, setBuilt] = useState(false)

  const plans = useMemo(() => data?.[0] ?? [], [data])
  const characters = useMemo(() => data?.[1] ?? [], [data])
  const kept = useMemo(() => keptPlans(plans), [plans])
  const replaced = useMemo(() => replacedPlans(plans), [plans])
  const characterOptions = useMemo(() => characters.map((c) => ({ value: c.id, label: c.name })), [characters])
  const nameOf = useMemo(() => new Map(characters.map((c) => [c.id, c.name])), [characters])

  if (error && !data) return <Alert type="error" showIcon message={`无法加载当前细纲：${error}`} />
  if (loading && !data) return <Spin />

  const checked = drafts.filter((d) => d.checked)
  const invalid = checked.filter((d) => planDraftError(d) !== null)
  const disabled = saving || saved
  const replacedBuilt = replaced.filter((p) => p.chapter_id !== null).length

  const patch = (key: string, p: Partial<EditablePlanDraft>) =>
    setDrafts((list) => list.map((d) => (d.key === key ? { ...d, ...p } : d)))

  const handleSave = async () => {
    setSaving(true)
    try {
      const out = await outlineProApi.syncPlans(projectId, oid, buildExpandSync(kept, drafts))
      message.success(`已保存 ${out.length} 章细纲${plans.length > 0 ? '（替换前已自动保存快照）' : ''}`)
      setSaved(true)
      onSaved?.()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const handleBuild = async () => {
    if (!canEditNow()) return
    setBuilding(true)
    try {
      const r = await outlineProApi.build(projectId, oid)
      if (!canEditNow()) return
      message.success(`已建章 ${r.chapters.length} 个，后续章节号已顺延`)
      setBuilt(true)
      onSaved?.()
    } catch (err) {
      if (!canEditNow()) return
      const conflict = readPlanningConflict(err)
      if (conflict) setMerge(conflict)
      else message.error(errorMessage(err))
    } finally {
      setBuilding(false)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {merge && <Suspense fallback={<Spin />}><PlanningMergeModal projectId={projectId} conflict={merge} onClose={() => setMerge(undefined)} onSaved={() => { onSaved?.(); message.success('规划已合并，请再次点击建章') }} /></Suspense>}
      {parsed.warnings.map((w) => (
        <Alert key={w} type="warning" showIcon message={w} />
      ))}
      {replaced.length > 0 && !saved && (
        <Alert
          type="info"
          showIcon
          message={`保存后将替换 ${replaced.length} 条没有正文的旧细纲${replacedBuilt > 0 ? `（其中 ${replacedBuilt} 个已建的空章节会一并删除，后续章节号前移）` : ''}`}
          description={replaced.map((p) => `${p.seq}. ${p.title}`).join('；')}
        />
      )}
      {saved && (
        <Alert
          type="success"
          showIcon
          message="细纲已保存。"
          action={
            <Button size="small" type="primary" loading={building} disabled={built || !canEdit} onClick={() => void handleBuild()}>
              {built ? '已建章' : '为新细纲建章'}
            </Button>
          }
        />
      )}
      {kept.map((p, i) => (
        <Card
          key={`kept-${p.id}`}
          size="small"
          style={{ background: token.colorFillQuaternary }}
          title={
            <Space wrap>
              <Tag>第 {i + 1} 章</Tag>
              <Tag color="green">已有正文，保留</Tag>
              {p.chapter_no !== null && <Tag>章节 {p.chapter_no}</Tag>}
              <Typography.Text>{p.title}</Typography.Text>
            </Space>
          }
        >
          <Typography.Paragraph type="secondary" style={{ marginBottom: 0 }} ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}>
            {p.summary || '（无梗概）'}
          </Typography.Paragraph>
          {p.key_events.length > 0 && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              关键事件：{p.key_events.join('；')}
            </Typography.Text>
          )}
        </Card>
      ))}
      {drafts.map((d, i) => {
        const err = d.checked ? planDraftError(d) : null
        const order = d.checked ? kept.length + checked.indexOf(d) + 1 : null
        return (
          <Card
            key={d.key}
            size="small"
            title={
              <Space wrap>
                <Checkbox checked={d.checked} disabled={disabled} onChange={(e) => patch(d.key, { checked: e.target.checked })} />
                {order !== null ? <Tag color="blue">第 {order} 章 · 新</Tag> : <Tag>不采纳</Tag>}
                {d.unresolved.length > 0 && (
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    未匹配角色：{d.unresolved.join('、')}
                  </Typography.Text>
                )}
                {err && <Typography.Text type="danger">{err}</Typography.Text>}
              </Space>
            }
            extra={
              <Space size={0}>
                <Button
                  type="text"
                  size="small"
                  icon={<ArrowUpOutlined />}
                  disabled={disabled || i === 0}
                  onClick={() => setDrafts((list) => moveItem(list, i, -1))}
                />
                <Button
                  type="text"
                  size="small"
                  icon={<ArrowDownOutlined />}
                  disabled={disabled || i === drafts.length - 1}
                  onClick={() => setDrafts((list) => moveItem(list, i, 1))}
                />
                <Button
                  type="text"
                  size="small"
                  danger
                  icon={<DeleteOutlined />}
                  disabled={disabled}
                  onClick={() => setDrafts((list) => list.filter((x) => x.key !== d.key))}
                />
              </Space>
            }
          >
            <PlanFieldsEditor
              value={d}
              characterOptions={characterOptions}
              disabled={disabled}
              onChange={(p) => patch(d.key, p)}
            />
            {d.character_ids.some((id) => !nameOf.has(id)) && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                部分角色已被删除，保存前请移除
              </Typography.Text>
            )}
          </Card>
        )
      })}
      <Space wrap>
        <Button
          type="primary"
          loading={saving}
          disabled={saved || checked.length === 0 || invalid.length > 0}
          onClick={() => void handleSave()}
        >
          保存细纲（共 {kept.length + checked.length} 章）
        </Button>
        <Typography.Text type={invalid.length > 0 ? 'danger' : 'secondary'}>
          {invalid.length > 0
            ? `有 ${invalid.length} 条已勾选的细纲需要修改`
            : '保存只写入细纲，不创建章节；不保存则不写入任何数据。'}
        </Typography.Text>
      </Space>
    </Space>
  )
}
