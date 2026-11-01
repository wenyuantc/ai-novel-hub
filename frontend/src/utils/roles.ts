import type { PermissionCode, PermissionDefinition } from '../api/types'

/** 显式添加依赖，并在选项和已选结果中展示，不在运行时暗中扩权。 */
export function permissionSelection(codes: PermissionCode[], definitions: PermissionDefinition[]): PermissionCode[] {
  const selected = new Set(codes)
  const visit = (code: PermissionCode) => {
    for (const dependency of definitions.find((item) => item.code === code)?.requires ?? []) {
      if (!selected.has(dependency)) { selected.add(dependency); visit(dependency) }
    }
  }
  for (const code of codes) visit(code)
  return definitions.filter((item) => selected.has(item.code)).map((item) => item.code)
}
