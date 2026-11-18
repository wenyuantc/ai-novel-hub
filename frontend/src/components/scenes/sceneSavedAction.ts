/** 章节编辑器把同步与后续写操作放在同一个动作屏障内。 */
export type RunSavedSceneAction = <T>(action: () => Promise<T>) => Promise<T | undefined>

export async function runAfterSceneSave<T>(
  runSavedAction: RunSavedSceneAction | undefined,
  ensureSaved: () => Promise<boolean>,
  action: () => Promise<T>,
): Promise<T | undefined> {
  if (runSavedAction) return runSavedAction(action)
  if (await ensureSaved()) return action()
}
