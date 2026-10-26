// 任务面板扩展点：kind → 结果查看组件（成功；chapter.batch 待审核时也可查看）。新增「只产出草稿」的任务类型时在这里注册即可，TaskPanel 不需要改动。
// 结果组件用 React.lazy 按需加载（不进入入口 chunk）；渲染方须包一层 <Suspense>（TaskResultModal / EntityTaskModal）。

import { lazy, type ComponentType } from 'react'
import type { TaskKind } from '../../api/types'
import type { TaskResultViewerProps } from './types'

export const TASK_RESULT_VIEWERS: Partial<Record<TaskKind, ComponentType<TaskResultViewerProps>>> = {
  'character.generate': lazy(() => import('./CharacterDraftResult')),
  'organization.generate': lazy(() => import('./OrganizationDraftResult')),
  'relations.parse': lazy(() => import('./RelationsParseResultView')),
  'career.generate': lazy(() => import('../careers/CareerDraftResult')),
  'outline.continue': lazy(() => import('../outlines/ContinueDraftResult')),
  'outline.expand': lazy(() => import('../outlines/ExpandDraftResult')),
  'chapter.batch': lazy(() => import('../batch/BatchResultView')),
}

export function hasResultViewer(kind: TaskKind): boolean {
  return kind in TASK_RESULT_VIEWERS
}
