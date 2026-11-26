const storageKey = 'oauth-return-path'

export function loginReturnPath(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return '/projects'
  if (value.includes('\\') || Array.from(value).some(char => char.charCodeAt(0) < 32)) return '/projects'
  return value
}

export function rememberLoginReturn(value: string) {
  try { sessionStorage.setItem(storageKey, loginReturnPath(value)) } catch { /* 无存储时回到书架 */ }
}

export function readLoginReturn() {
  try { return loginReturnPath(sessionStorage.getItem(storageKey)) } catch { return '/projects' }
}
