/** 路径和所属用户深链只接受正的十进制安全整数，不把 1e2 等输入改指其他用户。 */
export function parseAdminUserId(value: string | null | undefined): number | null {
  if (!value || !/^\d+$/.test(value)) return null
  const id = Number(value)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}
