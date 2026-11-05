import { errorMessage } from '../../api/client'

export interface RelationLoadedData<T> { key: string; data: T }
export type RelationRefreshResult<T> = { data: T; error: '' } | { data: null; error: string }

/** 获取成功和失败都作为结果返回，由调用方在请求仍有效时应用。 */
export async function refreshRelationData<T>(load: () => Promise<T>): Promise<RelationRefreshResult<T>> {
  try {
    return { data: await load(), error: '' }
  } catch (error) {
    return { data: null, error: errorMessage(error) }
  }
}

/** 刷新失败保留同一作用域的旧数据，避免卸载仍有草稿的编辑器。 */
export function applyRelationRefresh<T>(previous: RelationLoadedData<T> | null, key: string, result: RelationRefreshResult<T>): RelationLoadedData<T> | null {
  if (result.data !== null) return { key, data: result.data }
  return previous?.key === key ? previous : null
}
