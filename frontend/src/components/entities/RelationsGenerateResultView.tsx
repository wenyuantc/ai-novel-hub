import { Alert, App, Button, Empty, Space, Spin, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { ApiError, errorMessage } from '../../api/client'
import {
  filterExistingGeneratedSuggestions,
  generatedSuggestionsToBatch,
  parseRelationsGenerateResult,
  toEditable,
  type EditableGeneratedSuggestion,
} from '../../api/entityTasks'
import { relationshipApi } from '../../api/relationships'
import type { RelationsGenerateResult } from '../../api/types'
import { useLoader } from '../../hooks/useLoader'
import { invalidateProject } from '../../utils/projectRefresh'
import RelationSuggestionList from './RelationSuggestionList'
import type { TaskResultViewerProps } from './types'

/** 弹窗与任务面板共用：重新核对已存关系，预览勾选后才写入。 */
export default function RelationsGenerateResultView({ projectId, result, onSaved }: TaskResultViewerProps) {
  const parsed = parseRelationsGenerateResult(result)
  if (!parsed) return <Empty description="结果格式不正确，无法预览" />
  return <Editor projectId={projectId} parsed={parsed} onSaved={onSaved} />
}

function Editor({ projectId, parsed, onSaved }: { projectId: number; parsed: RelationsGenerateResult; onSaved?: () => void }) {
  const { message } = App.useApp()
  const [list, setList] = useState<EditableGeneratedSuggestion[]>(() => toEditable(parsed.suggestions))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const loader = useCallback(() => relationshipApi.list(projectId), [projectId])
  const { data: existing = [], loading, error, reload } = useLoader(loader, { projectId, resources: ['relationships'] })
  const available = filterExistingGeneratedSuggestions(list, existing)
  const items = generatedSuggestionsToBatch(available, parsed.character_ids)
  const groups = new Map<number | null, EditableGeneratedSuggestion[]>()
  for (const s of available) groups.set(s.subject_id, [...(groups.get(s.subject_id) ?? []), s])

  const handleSave = async () => {
    if (items.length === 0 || saving || saved || loading || error) return
    setSaving(true)
    try {
      const out = await relationshipApi.batch(projectId, items)
      setSaved(true)
      message.success(`已写入 ${out.length} 条关系`)
      invalidateProject(projectId, ['relationships'])
      onSaved?.()
    } catch (err) {
      message.error(errorMessage(err))
      if (err instanceof ApiError && err.status === 409) reload()
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%', minWidth: 0 }}>
      <Typography.Paragraph style={{ marginBottom: 0 }}>
        为 {parsed.character_ids.length} 名所选角色构思了 {parsed.suggestions.length} 条关系建议。可编辑称呼、亲密度与描述，勾选后统一保存。
      </Typography.Paragraph>
      {parsed.warnings.map((warning) => <Alert key={warning} type="warning" showIcon message={warning} />)}
      {error && <Alert type="error" showIcon message="未能核对已有关系" description={error} action={<Button onClick={reload}>重试</Button>} />}
      {saved && <Alert type="success" showIcon message="已写入，关系列表和图谱已刷新。" />}
      {!loading && !saved && items.length === 0 && (
        <Alert type="info" showIcon message="暂无可保存的关系建议" description="已有或无效的关系不可保存；也可以检查勾选与称呼。" />
      )}
      <Spin spinning={loading}>
        {groups.size === 0 ? <Empty description="暂无可保存的关系建议" /> : [...groups].map(([id, group]) => {
          const subject = `${group[0].subject_name || '未识别角色'}${id === null ? '' : `（ID=${id}）`}`
          return (
            <section key={id ?? 'invalid'} style={{ marginBottom: 16, minWidth: 0 }} aria-label={`${subject}的关系建议`}>
              <Typography.Title level={5}>{subject}</Typography.Title>
              <RelationSuggestionList
                subjectName={subject}
                value={group}
                showStatus
                showTargetId
                disabled={saving || saved || loading || !!error}
                onChange={(next) => setList((current) => current.map((s) => ({ ...s, ...next.find((n) => n.key === s.key) })))}
              />
            </section>
          )
        })}
      </Spin>
      <Button type="primary" loading={saving} disabled={items.length === 0 || saved || loading || !!error} onClick={() => void handleSave()}>
        写入 {items.length} 条关系
      </Button>
    </Space>
  )
}
