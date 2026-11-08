// 专属 5181/18766 服务、专用临时 SQLite 和带令牌 FakeLLM；不是完整 UI 验收。
async (page) => {
  await page.goto('http://127.0.0.1:5181/')
  return await page.evaluate(async () => {
    const token = sessionStorage.getItem('codex-probe-token')
    if (!token) throw new Error('隔离探针令牌未就绪')
    const json = { 'content-type': 'application/json' }
    const api = async (path, init = {}) => {
      const response = await fetch(path, init)
      if (!response.ok) throw new Error(`${path}: ${response.status} ${await response.text()}`)
      return response
    }
    await api('/api/v1/auth/register', { method: 'POST', headers: json,
      body: JSON.stringify({ username: `prose${crypto.randomUUID().slice(0, 8)}`, password: 'secret123' }) })
    const project = await (await api('/api/v1/projects', { method: 'POST', headers: json,
      body: JSON.stringify({ title: '隔离正文改写', genre: '科幻', premise: '仅用于测试', style: '' }) })).json()
    const chapter = await (await api(`/api/v1/projects/${project.id}/chapters`, { method: 'POST', headers: json,
      body: JSON.stringify({ title: '格式确认', content: '前甲🙂尾' }) })).json()
    await api('/api/v1/ai-configs', { method: 'POST', headers: json, body: JSON.stringify({ name: '测试模型',
      provider: 'openai_compat', base_url: 'https://api.example.com/v1/', api_key: 'sk-test-abcdefgh1234', model: 'gpt-test' }) })
    const base = `/api/v1/projects/${project.id}/chapters/${chapter.id}`
    const scene = (await (await api(`${base}/scenes`)).json()).items[0]
    const endpoint = `${base}/scenes/${scene.id}/document`
    const original = await (await api(endpoint)).json()
    original.document.formatting.inline = [{ kind: 'bold', start: 1, end: 3 }]
    const marker = crypto.randomUUID()
    original.document.markers = [{ key: marker, section_key: null, status: 'active', color: 'yellow', start: 1, end: 3 }]
    const saved = await (await api(endpoint, { method: 'PUT', headers: json, body: JSON.stringify({
      expected_content_revision: original.content_revision, document: original.document,
      add_section_keys: [], delete_section_keys: [], add_marker_keys: [marker], delete_marker_keys: [],
      mapped_section_keys: [], mapped_marker_keys: [],
    }) })).json()
    await api('/api/__codex_probe/responses', { method: 'POST', headers: { ...json, 'x-codex-probe-token': token },
      body: JSON.stringify({ items: ['改好🙂'] }) })
    const revisions = { expected_content_revision: saved.content_revision, expected_planning_revision: saved.planning_revision }
    const stream = await api(`${base}/scenes/${scene.id}/rewrite`, { method: 'POST', headers: json,
      body: JSON.stringify({ ...revisions, op: 'rewrite', start: 1, end: 3 }) })
    const frames = (await stream.text()).split('\n\n')
    const frame = frames.find((item) => item.startsWith('event: done\n'))
    if (!frame) throw new Error('真实服务未返回完成事件')
    const result = JSON.parse(frame.split('\n').find((line) => line.startsWith('data: ')).slice(6))
    const apply = { ...revisions, start: result.start, end: result.end, original: result.original, before: result.before,
      log_id: result.log_id, replacement: result.text }
    const rejected = await fetch(`${base}/scenes/${scene.id}/rewrite/apply`, { method: 'POST', headers: json, body: JSON.stringify(apply) })
    if (rejected.status !== 409 || (await rejected.json()).code !== 'rich_document_confirmation_required') throw new Error('服务未要求格式确认')
    const accepted = await api(`${base}/scenes/${scene.id}/rewrite/apply`, { method: 'POST', headers: json,
      body: JSON.stringify({ ...apply, confirm_format_loss: true }) })
    if ((await accepted.json()).content !== '前改好🙂尾') throw new Error('采纳内容错误')
    const after = (await (await api(endpoint)).json()).document
    if (after.markers[0].status !== 'stale' || after.markers[0].excerpt !== '甲🙂') throw new Error('失效标记未保留')
    const calls = await (await api('/api/__codex_probe/calls', { headers: { 'x-codex-probe-token': token } })).json()
    if (calls.calls.length !== 1 || !JSON.stringify(calls.calls[0].messages).includes('甲🙂')) throw new Error('FakeLLM 调用未按选区绑定')
    return { status: 'PASS', projectId: project.id, chapterId: chapter.id, sceneId: scene.id,
      checked: '真实 HTTP / SQLite / FakeLLM 正文选区、格式确认、标记与模型出站' }
  })
}
