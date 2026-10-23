// 本地偏好（localStorage）；不可用（隐私模式等）时回退默认值

const AUTO_CONTINUE_KEY = 'ainovel.autoContinue'

/** 「字数不足时自动续写」：默认开启。 */
export function readAutoContinue(): boolean {
  try {
    return localStorage.getItem(AUTO_CONTINUE_KEY) !== '0'
  } catch {
    return true
  }
}

export function writeAutoContinue(value: boolean): void {
  try {
    localStorage.setItem(AUTO_CONTINUE_KEY, value ? '1' : '0')
  } catch {
    // 不可写时忽略
  }
}
