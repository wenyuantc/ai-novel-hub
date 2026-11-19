/**
 * 保存后焦点回到状态选项时的目标：优先当前选中项，没有选中项才退回第一个。
 * 不能写成一个选择器列表：querySelector 按文档顺序返回第一个匹配任一选择器的元素，
 * 会落到第一个单选上，键盘用户此时按空格就会把状态改掉。
 */
export function statusFocusTarget(root: Pick<ParentNode, 'querySelector'>): HTMLElement | null {
  return root.querySelector<HTMLElement>('input[type="radio"]:checked') ?? root.querySelector<HTMLElement>('input[type="radio"]')
}
