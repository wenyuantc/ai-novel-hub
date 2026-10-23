import { Alert, App, Button, Empty, Space, Typography } from 'antd'
import { useState } from 'react'
import { errorMessage } from '../../api/client'
import { parseRelationsParseResult, suggestionsToBatch, toEditable, type EditableSuggestion } from '../../api/entityTasks'
import { relationshipApi } from '../../api/relationships'
import type { RelationsParseResult } from '../../api/types'
import RelationSuggestionList from './RelationSuggestionList'
import type { TaskResultViewerProps } from './types'

/** relations.parse 结果：逐条勾选后 relationships/batch（source=ai_parse）单事务写入；旧备注保持不变。 */
export default function RelationsParseResultView({ projectId, result, onSaved }: TaskResultViewerProps) {
  const parsed = parseRelationsParseResult(result)
  if (!parsed) return <Empty description="结果格式不正确，无法预览" />
  return <Editor projectId={projectId} parsed={parsed} onSaved={onSaved} />
}

function Editor({ projectId, parsed, onSaved }: { projectId: number; parsed: RelationsParseResult; onSaved?: () => void }) {
  const { message } = App.useApp()
  const [list, setList] = useState<EditableSuggestion[]>(() => toEditable(parsed.suggestions))
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const items = suggestionsToBatch(parsed.character_id, list)

  const handleSave = async () => {
    if (items.length === 0) return
    setSaving(true)
    try {
      const out = await relationshipApi.batch(projectId, items)
      message.success(`已写入 ${out.length} 条关系`)
      setSaved(true)
      onSaved?.()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <Typography.Text>
        从「{parsed.character_name}」的旧关系备注中解析出 {parsed.suggestions.length} 条关系，勾选后写入关系表（旧备注保持不变）。
      </Typography.Text>
      {parsed.warnings.map((w) => (
        <Alert key={w} type="warning" showIcon message={w} />
      ))}
      {saved && <Alert type="success" showIcon message="已写入。" />}
      <RelationSuggestionList subjectName={parsed.character_name} value={list} onChange={setList} disabled={saving || saved} />
      <Button type="primary" loading={saving} disabled={items.length === 0 || saved} onClick={() => void handleSave()}>
        写入 {items.length} 条关系
      </Button>
    </Space>
  )
}
