import { Alert, App, Button, Empty, Form, Space, Tabs, Typography } from 'antd'
import { useCallback, useState } from 'react'
import { careerApi } from '../../api/careers'
import { characterExtraApi } from '../../api/characters'
import { errorMessage } from '../../api/client'
import {
  careerSuggestionLimitError,
  careerSuggestionsToDrafts,
  parseCharacterGenerateResult,
  suggestionsToDrafts,
  toEditable,
  toEditableCareers,
  type EditableCareerSuggestion,
  type EditableSuggestion,
} from '../../api/entityTasks'
import { useLoader } from '../../hooks/useLoader'
import CareerSuggestionList from '../careers/CareerSuggestionList'
import { characterToForm, formToCharacterInput, type CharacterFormValues } from './characterForm'
import { CharacterBasicFields, CharacterDescFields, CharacterStateFields } from './CharacterFormFields'
import RelationSuggestionList from './RelationSuggestionList'
import type { TaskResultViewerProps } from './types'

/** character.generate 结果：可编辑草稿 + 关系 / 职业建议勾选，确认后 with-relations 单事务保存；不保存则什么都不写。 */
export default function CharacterDraftResult({ projectId, result, onSaved }: TaskResultViewerProps) {
  const parsed = parseCharacterGenerateResult(result)
  if (!parsed) return <Empty description="结果格式不正确，无法预览" />
  return <Editor projectId={projectId} parsed={parsed} onSaved={onSaved} />
}

function Editor({
  projectId,
  parsed,
  onSaved,
}: {
  projectId: number
  parsed: NonNullable<ReturnType<typeof parseCharacterGenerateResult>>
  onSaved?: () => void
}) {
  const { message } = App.useApp()
  const [form] = Form.useForm<CharacterFormValues>()
  const [suggestions, setSuggestions] = useState<EditableSuggestion[]>(() => toEditable(parsed.relation_suggestions))
  const [careerSugs, setCareerSugs] = useState<EditableCareerSuggestion[]>(() => toEditableCareers(parsed.career_suggestions))
  const hasCareers = parsed.career_suggestions.length > 0
  const careerLoader = useCallback(async () => (hasCareers ? careerApi.list(projectId) : []), [hasCareers, projectId])
  const { data: careers = [] } = useLoader(careerLoader)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const name = Form.useWatch('name', form) ?? parsed.draft.name

  const careerError = careerSuggestionLimitError(careerSugs)
  const careerDrafts = careerSuggestionsToDrafts(careerSugs)

  const handleSave = async () => {
    const v = await form.validateFields()
    if (careerError) {
      message.warning(careerError)
      return
    }
    setSaving(true)
    try {
      const out = await characterExtraApi.withRelations(projectId, {
        character: formToCharacterInput(v),
        relations: suggestionsToDrafts(suggestions),
        careers: careerDrafts,
      })
      const careerText = out.careers?.length ? `、${out.careers.length} 个职业` : ''
      message.success(`已保存角色「${out.character.name}」及 ${out.relationships.length} 条关系${careerText}`)
      setSaved(true)
      onSaved?.()
    } catch (err) {
      message.error(errorMessage(err))
    } finally {
      setSaving(false)
    }
  }

  const checked = suggestionsToDrafts(suggestions).length
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {parsed.warnings.map((w) => (
        <Alert key={w} type="warning" showIcon message={w} />
      ))}
      {saved && <Alert type="success" showIcon message="已保存。" />}
      <Form form={form} layout="vertical" initialValues={characterToForm(parsed.draft)} disabled={saving}>
        <Tabs
          items={[
            { key: 'basic', label: '基础', forceRender: true, children: <CharacterBasicFields form={form} creating showSort={false} /> },
            { key: 'desc', label: '描写', forceRender: true, children: <CharacterDescFields /> },
            { key: 'state', label: '状态', forceRender: true, children: <CharacterStateFields /> },
            {
              key: 'rel',
              label: `关系建议（${checked}/${parsed.relation_suggestions.length}）`,
              children: (
                <RelationSuggestionList subjectName={name || '新角色'} value={suggestions} onChange={setSuggestions} disabled={saving} />
              ),
            },
            ...(hasCareers
              ? [
                  {
                    key: 'career',
                    label: `职业建议（${careerDrafts.length}/${parsed.career_suggestions.length}）`,
                    children: (
                      <Space direction="vertical" style={{ width: '100%' }}>
                        {careerError && <Alert type="error" showIcon message={careerError} />}
                        <CareerSuggestionList value={careerSugs} onChange={setCareerSugs} careers={careers} disabled={saving} />
                      </Space>
                    ),
                  },
                ]
              : []),
          ]}
        />
      </Form>
      <Space>
        <Button type="primary" loading={saving} disabled={saved} onClick={() => void handleSave()}>
          保存角色{checked > 0 ? `及 ${checked} 条关系` : ''}
          {careerDrafts.length > 0 ? `、${careerDrafts.length} 个职业` : ''}
        </Button>
        <Typography.Text type="secondary">未勾选的关系 / 职业不会写入；不保存则不写入任何数据。</Typography.Text>
      </Space>
    </Space>
  )
}
