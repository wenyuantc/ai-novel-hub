import { useProjectAccess } from '../../hooks/useProjectAccess'
import { App, Button, List, Popconfirm, Space, Tag, Typography } from 'antd'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { errorMessage } from '../../api/client'
import { outlineProApi, planDraftError, removePlanSync } from '../../api/outlines'
import type { OutlineDetail, Plan } from '../../api/types'
import PlanFieldsEditor, { type PlanFieldValues } from './PlanFieldsEditor'

interface Props {
  projectId: number
  outline: OutlineDetail
  characterOptions: { value: number; label: string }[]
  characterNames: Map<number, string>
  /** 细纲有变化（编辑 / 删除）后刷新大纲概览 */
  onChanged: () => void
  focusedPlanId?: number
}

function toValues(p: Plan): PlanFieldValues {
  return {
    title: p.title,
    summary: p.summary,
    key_events: [...p.key_events],
    character_ids: [...p.character_ids],
    emotion: p.emotion,
    goal: p.goal,
    est_words: p.est_words,
  }
}

/** 大纲页展开行：某条大纲的细纲列表，行内编辑（PATCH 单条）与删除（PUT 整体同步去掉该条；有正文的不能删）。 */
export default function PlanList({ projectId, outline, characterOptions, characterNames, onChanged, focusedPlanId }: Props) {
  const { canEdit, canEditNow } = useProjectAccess(projectId)
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [editingId, setEditingId] = useState<number | null>(null)
  const [draft, setDraft] = useState<PlanFieldValues | null>(null)
  const [pending, setPending] = useState<number | null>(null)
  const plans = [...outline.plans].sort((a, b) => a.seq - b.seq)

  useEffect(() => {
    if (!focusedPlanId) return
    const frame = window.requestAnimationFrame(() => document.getElementById(`outline-plan-${focusedPlanId}`)?.scrollIntoView({ block: 'center' }))
    return () => window.cancelAnimationFrame(frame)
  }, [focusedPlanId])

  const startEdit = (p: Plan) => {
    setEditingId(p.id)
    setDraft(toValues(p))
  }

  const handleSave = async (p: Plan) => {
    if (!canEditNow()) return
    if (!draft) return
    const err = planDraftError(draft)
    if (err) {
      message.error(err)
      return
    }
    setPending(p.id)
    try {
      if (!canEditNow()) return
      await outlineProApi.updatePlan(projectId, outline.id, p.id, {
        title: draft.title.trim(),
        summary: draft.summary.trim(),
        key_events: draft.key_events,
        character_ids: draft.character_ids,
        emotion: draft.emotion.trim(),
        goal: draft.goal.trim(),
        est_words: draft.est_words,
      })
      if (!canEditNow()) return
      message.success('细纲已保存')
      setEditingId(null)
      setDraft(null)
      onChanged()
    } catch (e) {
      if (!canEditNow()) return
      message.error(errorMessage(e))
    } finally {
      setPending(null)
    }
  }

  const handleDelete = async (p: Plan) => {
    if (!canEditNow()) return
    setPending(p.id)
    try {
      if (!canEditNow()) return
      await outlineProApi.syncPlans(projectId, outline.id, removePlanSync(plans, p.id))
      if (!canEditNow()) return
      message.success(p.chapter_id !== null ? '已删除细纲及其空章节' : '已删除细纲')
      onChanged()
    } catch (e) {
      if (!canEditNow()) return
      message.error(errorMessage(e))
    } finally {
      setPending(null)
    }
  }

  return (
    <List
      size="small"
      dataSource={plans}
      renderItem={(p) => {
        const editing = editingId === p.id && draft !== null
        if (editing) {
          return (
            <List.Item key={p.id}>
              <Space direction="vertical" style={{ width: '100%' }}>
                <PlanFieldsEditor
                  value={draft}
                  characterOptions={characterOptions}
                  disabled={!canEdit || pending === p.id}
                  onChange={(patch) => setDraft((d) => (d ? { ...d, ...patch } : d))}
                />
                <Space>
                  <Button type="primary" size="small" loading={pending === p.id} onClick={() => void handleSave(p)} disabled={!canEdit}>
                    保存
                  </Button>
                  <Button
                    size="small"
                    disabled={!canEdit || pending === p.id}
                    onClick={() => {
                      setEditingId(null)
                      setDraft(null)
                    }}
                  >
                    取消
                  </Button>
                </Space>
              </Space>
            </List.Item>
          )
        }
        const names = p.character_ids.map((id) => characterNames.get(id)).filter((n): n is string => !!n)
        return (
          <List.Item
            id={`outline-plan-${p.id}`}
            key={p.id}
            actions={[
              <Button key="edit" type="link" size="small" disabled={!canEdit || editingId !== null} onClick={() => startEdit(p)}>
                {canEdit ? '编辑' : '查看'}</Button>,
              <Popconfirm
                key="del"
                title="删除这条细纲？"
                description={
                  p.chapter_id !== null ? '对应的空章节会一并删除，其后的章节号前移 1。' : '删除后可从「快照」恢复之前保存的版本。'
                }
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
                disabled={p.has_content}
                onConfirm={() => handleDelete(p)}
              >
                <Button
                  type="link"
                  size="small"
                  danger
                  disabled={!canEdit || (p.has_content || editingId !== null)}
                  loading={pending === p.id}
                  title={p.has_content ? '已有正文的细纲不能删除' : undefined}
                >
                  删除
                </Button>
              </Popconfirm>,
            ]}
          >
            <div style={{ width: '100%' }}>
              <Space wrap size={4}>
                <Typography.Text type="secondary">{p.seq}.</Typography.Text>
                <Typography.Text strong>{p.title}</Typography.Text>
                {p.chapter_id !== null ? (
                  <Tag
                    color={p.has_content ? 'green' : 'cyan'}
                    style={{ cursor: 'pointer' }}
                    onClick={() => navigate(`/projects/${projectId}/chapters/${p.chapter_id}`)}
                  >
                    第 {p.chapter_no} 章{p.has_content ? ' · 已有正文' : ' · 空章'}
                  </Tag>
                ) : (
                  <Tag>未建章</Tag>
                )}
                <Tag>{p.est_words} 字</Tag>
                {p.emotion && <Tag color="purple">{p.emotion}</Tag>}
              </Space>
              {p.summary && (
                <Typography.Paragraph
                  type="secondary"
                  style={{ whiteSpace: 'pre-wrap', margin: '4px 0 0' }}
                  ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}
                >
                  {p.summary}
                </Typography.Paragraph>
              )}
              {p.key_events.length > 0 && (
                <Typography.Text style={{ fontSize: 12, display: 'block' }}>关键事件：{p.key_events.join('；')}</Typography.Text>
              )}
              {(names.length > 0 || p.goal) && (
                <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
                  {names.length > 0 ? `出场：${names.join('、')}` : ''}
                  {names.length > 0 && p.goal ? ' · ' : ''}
                  {p.goal ? `目标：${p.goal}` : ''}
                </Typography.Text>
              )}
            </div>
          </List.Item>
        )
      }}
    />
  )
}
