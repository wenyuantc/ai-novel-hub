import type { RequestOptions } from '../../api/client'
import type { ExportOptions } from '../../api/projects'
import { captureProjectScope, isProjectScopeCurrent, useProjectStore, type ProjectScope } from '../../stores/project'

/** null 表示用户未编辑，由服务器在实际导出时决定作品署名；空串是显式不署名。 */
export function epubAuthorOptions(draft: string | null): Pick<ExportOptions, 'author'> {
  return draft === null ? {} : { author: draft.trim() }
}

/** 未编辑时跟随最新作品署名，编辑后保留用户草稿。 */
export function epubAuthorText(defaultAuthor: string, draft: string | null): string {
  return draft ?? defaultAuthor
}

/** 书架没有当前项目作用域；先受控加载，关闭或切换账号后不落地旧响应。 */
export async function prepareShelfExportScope(
  projectId: number, options: Pick<RequestOptions, 'signal' | 'isCurrent'>,
): Promise<ProjectScope | null> {
  const current = () => !options.signal?.aborted && (options.isCurrent?.() ?? true)
  if (!current()) return null
  await useProjectStore.getState().load(projectId, options)
  const scope = captureProjectScope(projectId)
  // load 在同一作品临时失败时保留旧内容；此入口不能把旧授权快照当作本次加载成功。
  return current() && !useProjectStore.getState().error && isProjectScopeCurrent(scope, 'canExport') ? scope : null
}
