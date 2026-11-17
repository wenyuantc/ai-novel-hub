import type { TemplateIncludedDefinitions } from '../../api/novelTemplates'

const FIELD_TYPES: Record<string, string> = { text: '多行文本', line: '单行文本', dropdown: '下拉选项', reference: '引用' }
const RELATION_CATEGORIES: Record<string, string> = { family: '家庭', social: '社交', hostile: '敌对', work: '工作', other: '其他' }

/** 预览服务器实际携带的配套定义；无论是否包含完整配置，均需让作者看见。 */
export default function IncludedDefinitions({ definitions }: { definitions: TemplateIncludedDefinitions }) {
  const { fields, categories, relationship_types: relationships, careers } = definitions
  return <details style={{ overflowWrap: 'anywhere', marginBlock: 12 }}>
    <summary>随内容保存的配套定义：字段 {fields.length}、分类 {categories.length}、关系类型 {relationships.length}、职业 {careers.length}</summary>
    <p>以下定义和选项随本次选择一并保存；职业阶段随所选职业保存。</p>
    <details><summary>字段及选项（{fields.length}）</summary>{fields.length ? <ul>{fields.map((field, index) =>
      <li key={index}>{field.name}（{FIELD_TYPES[field.field_type] ?? field.field_type}）{field.options.length > 0 && <>：{field.options.join('、')}</>}</li>)}</ul> : <p>无</p>}</details>
    <details><summary>分类（{categories.length}）</summary>{categories.length ? <ul>{categories.map((name, index) => <li key={index}>{name}</li>)}</ul> : <p>无</p>}</details>
    <details><summary>关系类型（{relationships.length}）</summary>{relationships.length ? <ul>{relationships.map((item, index) =>
      <li key={index}>{item.role_a} → {item.role_b}（{item.origin === 'global' ? '全局' : '作品专用'}，{RELATION_CATEGORIES[item.category] ?? item.category}）</li>)}</ul> : <p>无</p>}</details>
    <details><summary>职业及阶段（{careers.length}）</summary>{careers.length ? <ul>{careers.map((career, index) =>
      <li key={index}>{career.name}：{career.stages.join('、') || '无阶段'}</li>)}</ul> : <p>无</p>}</details>
  </details>
}
