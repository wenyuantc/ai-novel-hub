import { ApiError, request } from './client'
import type {
  SettingCategoryCreate, SettingCategoryEntryPage, SettingCategoryEntryQuery, SettingCategoryListQuery,
  SettingCategoryMutation, SettingCategoryPage, SettingCategoryPreview, SettingCategoryUpdate, SettingCategoryVersions,
  SettingCategory, SettingCategoryInput, SettingCategorySpec,
  SettingEntryRef, SettingTagSuggestions, SettingTagSuggestionsQuery, SettingTagsOut, SettingTagsUpdate,
} from './types'

const categories = (pid: number) => `/projects/${pid}/setting-categories`
const category = (pid: number, id: number) => `${categories(pid)}/${id}`
const tags = (pid: number, entry: SettingEntryRef) => `/projects/${pid}/setting-entries/${entry.kind}/${entry.id}/tags`

function assertCategoryPosition(query: SettingCategoryEntryQuery = {}): void {
  if (query.chapter_id != null && query.chapter_no != null) throw new Error('章节 ID 与计划章节号只能选择一个')
}

/** 只封装传输，不拼合跨请求版本、不逐条求值，也不自动刷新或重放冲突请求。 */
export const settingCategoriesApi = {
  list: (pid: number, query: SettingCategoryListQuery = {}, signal?: AbortSignal) =>
    request<SettingCategoryPage>(categories(pid), { query: { ...query }, signal }),
  get: (pid: number, id: number, signal?: AbortSignal) =>
    request<SettingCategoryMutation>(category(pid, id), { signal }),
  create: (pid: number, body: SettingCategoryCreate, signal?: AbortSignal) =>
    request<SettingCategoryMutation>(categories(pid), { method: 'POST', body, signal }),
  update: (pid: number, id: number, body: SettingCategoryUpdate, signal?: AbortSignal) =>
    request<SettingCategoryMutation>(category(pid, id), { method: 'PATCH', body, signal }),
  remove: (pid: number, id: number, expectedCategoriesRevision: number, signal?: AbortSignal) =>
    request<SettingCategoryVersions>(category(pid, id), { method: 'DELETE', query: { expected_categories_revision: expectedCategoriesRevision }, signal }),
  entries: (pid: number, id: number, query: SettingCategoryEntryQuery = {}, signal?: AbortSignal) => {
    assertCategoryPosition(query)
    return request<SettingCategoryEntryPage>(`${category(pid, id)}/entries`, { query: { ...query }, signal })
  },
  preview: (pid: number, body: SettingCategoryPreview, signal?: AbortSignal) => {
    assertCategoryPosition(body.filters)
    return request<SettingCategoryEntryPage>(`${categories(pid)}/preview`, { method: 'POST', body, signal })
  },
  getTags: (pid: number, entry: SettingEntryRef, signal?: AbortSignal) =>
    request<SettingTagsOut>(tags(pid, entry), { signal }),
  updateTags: (pid: number, entry: SettingEntryRef, body: SettingTagsUpdate, signal?: AbortSignal) =>
    request<SettingTagsOut>(tags(pid, entry), { method: 'PATCH', body, signal }),
  suggestTags: (pid: number, query: SettingTagSuggestionsQuery = {}, signal?: AbortSignal) =>
    request<SettingTagSuggestions>(`/projects/${pid}/setting-entry-tags`, { query: { ...query }, signal }),
}

/** scope明确的目录版本冲突与同名、停用、图依赖等业务409分开处理。 */
export function settingCategoryConflictScope(error: unknown): 'categories' | 'fields' | null {
  if (!(error instanceof ApiError) || error.status !== 409 || error.body?.code !== 'stale_revision') return null
  const scope = error.body.scope
  return scope === 'categories' || scope === 'fields' ? scope : null
}

/** 条件位置来自服务端的零基索引，仅接受合法条件范围，不能宽泛强转错误体。 */
export function settingCategoryRuleIndex(error: unknown): number | null {
  const index = error instanceof ApiError ? error.body?.rule_index : undefined
  return typeof index === 'number' && Number.isInteger(index) && index >= 0 && index < 20 ? index : null
}

/** 从规范响应显式构造编辑payload；保留规则顺序和业务身份，不携带数据库规则行ID。 */
export function settingCategoryToInput(category: SettingCategory | SettingCategorySpec): SettingCategoryInput {
  return {
    name: category.name, applicable_types: [...category.applicable_types], match_mode: category.match_mode, sort: category.sort,
    rules: category.rules.map((rule) => rule.kind === 'tag' ? { kind: 'tag', tag: rule.tag, sort: rule.sort }
      : { kind: 'dropdown', definition_id: rule.definition_id, option_id: rule.option_id, sort: rule.sort }),
  }
}
