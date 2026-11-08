/** 由宿主保存并确认选区后提供，偏移按 Unicode 码点计算。 */
export interface SectionRewriteTarget {
  projectId: number
  chapterId: number
  sceneId: number
  sectionKey: string
  sectionTitle: string
  includeInAi: boolean
  contentRevision: number
  planningRevision: number
  start: number
  end: number
  selectedText: string
}

/** 保持与服务端 Python str.strip() 和码点偏移一致，不修改选中文字。 */
export function sectionSelectionProblem(target: SectionRewriteTarget): string | null {
  const chars = Array.from(target.selectedText)
  if (!Number.isSafeInteger(target.start) || !Number.isSafeInteger(target.end)
      || target.start < 0 || target.end <= target.start || target.end - target.start !== chars.length) {
    return '选区位置已失效，请重新选择。'
  }
  if (chars.length > 5000) return '单次选区不能超过 5000 个字符。'
  const hasText = chars.some((char) => {
    const point = char.codePointAt(0)!
    return !/\p{White_Space}/u.test(char) && !(point >= 0x1c && point <= 0x1f)
  })
  return hasText ? null : '请先选择需要处理的文字。'
}

export function sectionDisclosure(includeInAi: boolean): string {
  return includeInAi
    ? '本区块已允许 AI 自动使用：除选区外，本区块名称和其他内容也可能按原有背景设置发送。其他已关闭区块不会自动附带。'
    : '本区块已关闭 AI 自动使用：本次只从该区块额外发送上面的选区，不自动附带其余内容或名称，也不会自动附带其他已关闭区块。'
}
