/** 只复制公开示例；剪贴板受限时由代码块选择文本并给出手动复制提示。 */
export async function copyDeveloperCode(code: string): Promise<boolean> {
  try {
    if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) return false
    await navigator.clipboard.writeText(code)
    return true
  } catch {
    return false
  }
}
