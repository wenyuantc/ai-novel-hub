import type { SettingAiMode } from '../../api/settingAiVisibility'

export const SETTING_AI_MODES: Record<SettingAiMode, { label: string; description: string }> = {
  always: { label: '始终加入', description: '有设定上下文的 AI 操作可使用此条目，不要求文本中提到它。' },
  detected: { label: '检测到才加入', description: '名称或别名被明确检测到时可加入，也可通过手动指定或合格的关联加入。' },
  manual_only: { label: '检测到不自动加入', description: '字面命中不会自动加入；仍允许手动指定或合格的关联加入。' },
  never: { label: '永不加入', description: '此条目不提供给 AI，手动指定和关联也不能绕过。人工查看与提及统计不受影响。' },
}
export const SETTING_AI_MODE_OPTIONS = (Object.entries(SETTING_AI_MODES) as [SettingAiMode, typeof SETTING_AI_MODES[SettingAiMode]][]).map(([value, { label }]) => ({ value, label }))
const FIELD_LABELS: Record<string, string> = {
  role_type: '角色定位', age: '年龄', gender: '性别', profile: '简介', appearance: '外貌', personality: '性格',
  background: '背景', current_state: '当前处境', life_status: '生死状态', relations_text: '旧关系备注',
  content: '档案正文', org_type: '组织类型', purpose: '宗旨', location: '所在地', power_level: '势力值', description: '描述',
  requirements: '获得条件', abilities: '特殊能力', rules: '体系规则', stages: '职业阶段资料',
}
export const settingAiFieldLabel = (key: string) => FIELD_LABELS[key] ?? key
