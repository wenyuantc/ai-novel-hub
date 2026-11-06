/** 页面数据保留在各自 loader；这里只广播失效范围，不缓存业务数据。 */
export type ProjectResource = 'project' | 'characters' | 'organizations' | 'relationships' | 'careers' | 'world' | 'outlines' | 'chapters' | 'foreshadows' | 'memories' | 'knowledge' | 'notes' | 'chat' | 'planning' | 'setting_fields' | 'setting-categories'
type Listener = (projectId: number, resources: readonly ProjectResource[]) => void
const listeners = new Set<Listener>()
export function invalidateProject(projectId: number, resources: readonly ProjectResource[]) {
  listeners.forEach((listener) => listener(projectId, resources))
}
export function subscribeProjectRefresh(listener: Listener) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}
