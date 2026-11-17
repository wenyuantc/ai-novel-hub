import { useEffect, useState } from 'react'
import { consistencyApi, type ImpactDetail } from '../api/consistency'
import { editorImpactHint } from '../utils/consistencyImpact'

/**
 * 章节编辑页页头的「派生内容可能过期」提示：进入页面及 refreshKey 变化（正文保存成功、重新分析完成）时读取单章接口。
 * 只读取、不调用模型；请求失败静默（保留上一次读到的结果），不影响编辑、保存与离页拦截。切换作品或章节后旧结果不再显示。
 */
export function useChapterImpactHint(projectId: number, chapterId: number, refreshKey: string): string | null {
  const scope = `${projectId}:${chapterId}`
  const [state, setState] = useState<{ scope: string; detail: ImpactDetail } | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    consistencyApi.impact(projectId, chapterId, controller.signal).then(
      (detail) => { if (!controller.signal.aborted) setState({ scope: `${projectId}:${chapterId}`, detail }) },
      () => { /* 提示只是辅助信息：读取失败不打扰写作 */ },
    )
    return () => controller.abort()
  }, [projectId, chapterId, refreshKey])
  return editorImpactHint(state?.scope === scope ? state.detail : null, chapterId)
}
