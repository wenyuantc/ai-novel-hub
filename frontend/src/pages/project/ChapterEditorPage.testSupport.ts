import type { Chapter } from '../../api/types'

/** 任务与模型权限页测试集中覆盖任务入口，真实保存编排另有状态与请求测试。 */
export function autosavePageFixture(chapter: Chapter, _scene: unknown, _blocked: boolean, receiveChapter: (value: Chapter) => void) {
  return {
    title: { getSnapshot: () => ({ value: chapter.title, conflict: null }) },
    journal: { getSnapshot: () => ({ ids: [], local: undefined }) },
    titleStamp: () => '',
    actionLocked: false,
    readyToSave: true,
    runSavedAction: (action: () => Promise<unknown>) => action(),
    receiveChapter,
  }
}
