export type EntrySelectValue = string | number
export interface EntrySelectOption<Value extends EntrySelectValue> { value: Value; label: string; disabled?: boolean }
export interface EntrySelectQuery { q: string; page: number; page_size: number }
export interface EntrySelectPage<Value extends EntrySelectValue> { items: EntrySelectOption<Value>[]; total: number }
export interface EntrySelectProps<Value extends EntrySelectValue> {
  projectId: number
  /** 区分目录、类型或配置代次；作品和当前授权身份由查询 Hook 另行隔离。 */
  queryKey: string
  loadEntries: (query: EntrySelectQuery, signal: AbortSignal) => Promise<EntrySelectPage<Value>>
  value?: Value[]
  onChange?: (value: Value[]) => void
  /** 分页外的已选名称由调用方提供，不根据搜索缺失推断条目已删除。 */
  selectedOptions?: EntrySelectOption<Value>[]
  disabled?: boolean
  single?: boolean
  label: string
  id?: string
}

/** 当前页优先；只补回已选项，搜索为空或失败均不清空受控值。 */
export function entrySelectOptions<Value extends EntrySelectValue>(
  items: readonly EntrySelectOption<Value>[], value: readonly Value[], selectedOptions: readonly EntrySelectOption<Value>[] = [],
): EntrySelectOption<Value>[] {
  const options = new Map(items.map((item) => [item.value, item]))
  const selected = new Map(selectedOptions.map((item) => [item.value, item]))
  for (const id of value) if (!options.has(id)) options.set(id, selected.get(id) ?? { value: id, label: `已选条目 #${id}` })
  return [...options.values()]
}

export function entrySelectValues<Value extends EntrySelectValue>(value: Value | Value[] | undefined): Value[] {
  return Array.isArray(value) ? value : value === undefined ? [] : [value]
}
