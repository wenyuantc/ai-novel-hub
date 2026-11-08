// 合成账号 + 专用 SQLite；真实浏览器确认删除失效标记，并核对作品备份内容。
async (page) => {
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('http://127.0.0.1:5181/')
  const seeded = await page.evaluate(async () => {
    const call = async (url, method = 'GET', body) => {
      const response = await fetch(`/api/v1${url}`, {
        method, headers: { 'content-type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
      })
      if (!response.ok) throw new Error(`${url}: ${response.status} ${await response.text()}`)
      return response.json()
    }
    await call('/auth/register', 'POST', { username: `mkr${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' })
    const project = await call('/projects', 'POST', { title: '标记删除备份隔离测试', genre: '科幻', premise: '测试', style: '' })
    const chapter = await call(`/projects/${project.id}/chapters`, 'POST', { title: '正文', content: '普通正文🙂' })
    const scene = (await call(`/projects/${project.id}/chapters/${chapter.id}/scenes`)).items[0]
    const endpoint = `/projects/${project.id}/chapters/${chapter.id}/scenes/${scene.id}/document`
    const current = await call(endpoint)
    const key = crypto.randomUUID()
    current.document.markers.push({ key, section_key: null, status: 'stale', color: 'blue', start: null, end: null,
      last_start: 0, excerpt: '旧标记摘录唯一哨兵', excerpt_truncated: false,
      draft_excerpt: '草稿摘录唯一哨兵', draft_excerpt_truncated: false, draft_last_start: 0 })
    await call(endpoint, 'PUT', { expected_content_revision: current.content_revision, document: current.document,
      add_section_keys: [], delete_section_keys: [], add_marker_keys: [key], delete_marker_keys: [],
      mapped_section_keys: [], mapped_marker_keys: [] })
    return { projectId: project.id, chapterId: chapter.id, sceneId: scene.id, key }
  })
  await page.goto(`http://127.0.0.1:5181/projects/${seeded.projectId}/chapters/${seeded.chapterId}?scene_id=${seeded.sceneId}`)
  await page.getByRole('textbox', { name: '当前场景正文' }).waitFor({ timeout: 30000 })
  await page.getByRole('button', { name: '全书标记' }).click()
  const list = page.getByRole('list', { name: '全书正文标记' })
  await list.getByText('已失效').waitFor()
  const deletion = list.getByRole('button', { name: /删除第 1 章/ })
  await deletion.click()
  const confirmation = page.getByRole('tooltip').filter({ hasText: '删除这条标记' })
  await confirmation.getByRole('button', { name: /取\s*消/ }).click()
  if ((await page.evaluate(async (pid) => (await (await fetch(`/api/v1/projects/${pid}/markers`)).json()).total, seeded.projectId)) !== 1) {
    throw new Error('取消删除仍然修改了标记')
  }
  await deletion.click()
  const responsePromise = page.waitForResponse((response) => response.url().includes('/markers/') && response.request().method() === 'DELETE')
  await confirmation.getByRole('button', { name: /删\s*除/ }).click()
  if ((await responsePromise).status() !== 200) throw new Error('删除标记请求失败')
  await page.getByText('标记已删除，正文未改变').waitFor()
  return await page.evaluate(async ({ projectId, chapterId, key }) => {
    const get = async (path) => {
      const response = await fetch(`/api/v1${path}`)
      if (!response.ok) throw new Error(`${path}: ${response.status}`)
      return response.json()
    }
    const noVersions = await get(`/projects/${projectId}/backup?versions=0&snapshots=0&logs=0&inspiration=0`)
    const withVersions = await get(`/projects/${projectId}/backup?versions=1&snapshots=0&logs=0&inspiration=0`)
    const row = noVersions.tables.scene_markers.find((item) => item.logical_key === key)
    if (!row || row.status !== 'deleted' || row.excerpt !== '' || row.last_start !== null || row.draft_excerpt !== null) {
      throw new Error('不含版本的备份未清理已删除标记的当前摘录')
    }
    if (JSON.stringify(noVersions).includes('旧标记摘录唯一哨兵') || JSON.stringify(noVersions).includes('草稿摘录唯一哨兵')) {
      throw new Error('不含历史的备份泄露已删除标记摘录')
    }
    if (!JSON.stringify(withVersions).includes('旧标记摘录唯一哨兵')) throw new Error('版本备份未保留可恢复历史')
    const reader = await get(`/projects/${projectId}/reader/chapters/${chapterId}`)
    if (reader.chapter.content !== '普通正文🙂') throw new Error('删除标记意外更改正文')
    return { status: 'PASS', checked: '375px 取消/确认删除 → 当前备份清理摘录 → 版本备份保留历史 → 阅读正文不变' }
  }, seeded)
}
