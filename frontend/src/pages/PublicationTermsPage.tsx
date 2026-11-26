import { Alert, App, Button, Input, List, Modal, Skeleton, Space } from 'antd'
import { useCallback, useId, useRef, useState, useSyncExternalStore } from 'react'
import { ApiError, currentRequestGeneration, errorMessage, watchRequestGeneration } from '../api/client'
import { publicationTermsApi, type PublicationTermScope } from '../api/publicationTerms'
import { StudioPage, StudioPanel } from '../components/StudioUI'
import { useAuthorizationScope } from '../hooks/useAuthorizationScope'
import { useLoader } from '../hooks/useLoader'
import { useAuthStore } from '../stores/auth'
import { authorizationKey, can } from '../utils/permissions'

type Edit = { original: string | null; value: string; removing: boolean }
const actorMismatch = (err: unknown) => err instanceof ApiError && err.body?.code === 'editor_actor_changed'
const subscribeSession = (notify: () => void) => watchRequestGeneration(notify).dispose

function TermList({ scope, canEdit, generation, actorId, createdAt, authorization }: {
  scope: PublicationTermScope; canEdit: boolean; generation: number; actorId: number; createdAt: string; authorization: string
}) {
  const { message } = App.useApp()
  const access = useAuthorizationScope(scope === 'global' ? 'publication_terms.read' : undefined)
  const currentActor = useCallback(() => {
    const auth = useAuthStore.getState()
    return currentRequestGeneration() === generation && auth.status === 'authenticated'
      && auth.user?.id === actorId && auth.user.created_at === createdAt
      && authorizationKey(auth.user) === authorization
      && (scope === 'personal' || can(auth.user, 'publication_terms.read'))
  }, [generation, actorId, createdAt, scope, authorization])
  const [actorChanged, setActorChanged] = useState(false)
  const read = useCallback(() => publicationTermsApi.get(scope, {
    isCurrent: currentActor, editorActor: { id: actorId, createdAt },
  }).catch((err: unknown) => {
    if (currentActor() && actorMismatch(err)) setActorChanged(true)
    throw err
  }), [scope, currentActor, actorId, createdAt])
  const { data, loading, reload, setData, error } = useLoader(read)
  const [edit, setEdit] = useState<Edit | null>(null)
  const [issue, setIssue] = useState('')
  const [saving, setSaving] = useState(false)
  const [stale, setStale] = useState(false)
  const savingRef = useRef(false)
  const inputId = useId()
  const title = scope === 'global' ? '全站发布词表' : '个人发布词表'

  function open(editing: Edit) {
    setIssue('')
    setStale(false)
    setEdit(editing)
  }
  function close() {
    if (savingRef.current) return
    setEdit(null)
    if (stale) reload()
  }
  async function save() {
    if (!edit || !data || stale || savingRef.current || !currentActor() || !access.allowed()
      || (scope === 'global' && !can(useAuthStore.getState().user, 'publication_terms.update'))) return
    const { original, removing } = edit
    const value = edit.value.trim()
    if (!removing) {
      if (!value || Array.from(value).length > 128) { setIssue('请输入 1 至 128 个字符的词条。'); return }
      if (original !== value && data.terms.includes(value)) { setIssue('词表中已有相同词条。'); return }
    }
    let terms: string[]
    if (original === null) terms = [...data.terms, value]
    else if (removing) terms = data.terms.filter((term) => term !== original)
    else terms = data.terms.map((term) => term === original ? value : term)
    const ticket = access.begin()
    const isCurrent = () => currentActor() && access.current(ticket)
    savingRef.current = true
    setSaving(true)
    setIssue('')
    try {
      const updated = await publicationTermsApi.update(scope, terms, data.revision, {
        isCurrent, editorActor: { id: actorId, createdAt },
      })
      if (!isCurrent()) return
      setData(() => updated, { invalidatePending: true })
      setEdit(null)
      void message.success('词表已保存')
    } catch (err) {
      if (!isCurrent()) return
      if (actorMismatch(err)) {
        setActorChanged(true)
        setEdit(null)
        return
      }
      if (err instanceof ApiError && (err.status === 403 || err.status === 401)) {
        setEdit(null)
        reload()
        return
      }
      const conflict = err instanceof ApiError && err.status === 409
      setStale(conflict)
      setIssue(conflict ? '词表或账号已变化。关闭后将重新加载，请核对最新词条后再操作。' : errorMessage(err))
    } finally {
      savingRef.current = false
      if (isCurrent()) setSaving(false)
    }
  }

  if (actorChanged) return <StudioPage title={title}><Alert type="warning" showIcon
    message="当前登录账号已变化，请刷新页面后重新核对词表。"
    action={<Button onClick={() => window.location.reload()}>刷新页面</Button>} /></StudioPage>

  return <StudioPage title={title} hint={scope === 'global'
    ? '所有作品定稿和登记发布时，都会核对这些词条。'
    : '你名下的作品在定稿和登记发布时，会同时核对这份词表和全站词表。'}>
    <StudioPanel>
      <Space wrap style={{ marginBottom: 16 }}>
        {canEdit && <Button type="primary" disabled={!data || loading || !!error || data.terms.length >= 1000} onClick={() => open({ original: null, value: '', removing: false })}>新增词条</Button>}
        <Button onClick={reload} loading={loading} disabled={saving}>刷新词表</Button>
        {!!data && !loading && !error && <span>共 {data.terms.length} 个词条</span>}
      </Space>
      {error ? <Alert showIcon type="error" message={error} /> : loading || !data ? <Skeleton active /> :
        <List dataSource={data.terms} locale={{ emptyText: '词表为空' }}
          pagination={data.terms.length > 20 ? { pageSize: 20, showSizeChanger: false, simple: true } : false}
          renderItem={(term) => <List.Item key={term} style={{ gap: 12, flexWrap: 'wrap' }}>
            <span style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', minWidth: 0 }}>{term}</span>
            {canEdit && <Space>
              <Button size="small" aria-label={`编辑「${term}」`} onClick={() => open({ original: term, value: term, removing: false })}>编辑</Button>
              <Button size="small" danger aria-label={`删除「${term}」`} onClick={() => open({ original: term, value: term, removing: true })}>删除</Button>
            </Space>}
          </List.Item>} />}
    </StudioPanel>
    {edit && <Modal open title={edit.removing ? '删除词条' : edit.original === null ? '新增词条' : '编辑词条'}
      onCancel={close} onOk={() => void save()} confirmLoading={saving} cancelButtonProps={{ disabled: saving }}
      okButtonProps={{ danger: edit.removing, disabled: stale }} okText={edit.removing ? '删除' : '保存'} cancelText={stale ? '关闭并刷新' : '取消'}>
      {edit.removing ? <p style={{ overflowWrap: 'anywhere' }}>从{title}删除「{edit.original}」？</p> : <>
        <label htmlFor={inputId}>词条</label>
        <Input.TextArea id={inputId} value={edit.value} autoSize={{ minRows: 1, maxRows: 5 }}
          disabled={saving || stale} aria-invalid={!!issue} aria-describedby={issue ? `${inputId}-issue` : undefined}
          onChange={(event) => { setEdit({ ...edit, value: event.target.value }); setIssue('') }} />
      </>}
      {issue && <div id={`${inputId}-issue`}><Alert showIcon type="error" message={issue} /></div>}
    </Modal>}
  </StudioPage>
}

function TermsPage({ scope }: { scope: PublicationTermScope }) {
  const user = useAuthStore((s) => s.user)
  const generation = useSyncExternalStore(subscribeSession, currentRequestGeneration, currentRequestGeneration)
  if (!user || (scope === 'global' && !can(user, 'publication_terms.read'))) return null
  const authorization = authorizationKey(user)
  return <TermList key={`${scope}:${generation}:${user.id}:${user.created_at}:${authorization}`}
    authorization={authorization} actorId={user.id} createdAt={user.created_at} generation={generation} scope={scope}
    canEdit={scope === 'personal' || can(user, 'publication_terms.update')} />
}

export default function PublicationTermsPage() { return <TermsPage scope="personal" /> }
export function AdminPublicationTermsPage() { return <TermsPage scope="global" /> }
