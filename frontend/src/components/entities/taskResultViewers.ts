// 任务面板扩展点：kind → 结果查看组件。新增「只产出草稿」的任务类型时在这里注册即可，TaskPanel 不需要改动。

import type { ComponentType } from 'react'
import type { TaskKind } from '../../api/types'
import CharacterDraftResult from './CharacterDraftResult'
import OrganizationDraftResult from './OrganizationDraftResult'
import RelationsParseResultView from './RelationsParseResultView'
import type { TaskResultViewerProps } from './types'

export const TASK_RESULT_VIEWERS: Partial<Record<TaskKind, ComponentType<TaskResultViewerProps>>> = {
  'character.generate': CharacterDraftResult,
  'organization.generate': OrganizationDraftResult,
  'relations.parse': RelationsParseResultView,
}

export function hasResultViewer(kind: TaskKind): boolean {
  return kind in TASK_RESULT_VIEWERS
}
