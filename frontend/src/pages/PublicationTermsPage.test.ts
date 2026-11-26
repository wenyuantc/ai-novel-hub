import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PermissionCode, User } from '../api/types'
import PublicationTermsPage, { AdminPublicationTermsPage } from './PublicationTermsPage'

const auth = vi.hoisted(() => ({ user: null as User | null }))
vi.mock('../stores/auth', () => ({ useAuthStore: (select: (state: typeof auth) => unknown) => select(auth) }))

const list = vi.hoisted(() => vi.fn(() => ({
  data: { terms: ['晨光'], revision: 1 }, loading: false, error: null, reload: () => {}, setData: () => {},
})))
vi.mock('../hooks/useLoader', () => ({ useLoader: list }))

function as(permissions: PermissionCode[]) {
  auth.user = {
    id: 2, username: '读者', created_at: '', is_admin: false, is_super_admin: false,
    effective_permissions: permissions, roles: [], authorization_revision: 1,
  }
}

afterEach(() => { list.mockClear(); auth.user = null })

describe('发布词表读写入口', () => {
  it('普通作者可以维护本人词表，管理页拒绝读取', () => {
    as([])
    expect(renderToString(createElement(AdminPublicationTermsPage))).toBe('')
    expect(list).not.toHaveBeenCalled()
    const html = renderToString(createElement(PublicationTermsPage))
    for (const text of ['个人发布词表', '晨光', '新增词条', '编辑「晨光」', '删除「晨光」']) expect(html).toContain(text)
  })
  it('全站只读者可查看词条，只有具备修改权限才显示写入入口', () => {
    as(['publication_terms.read'])
    const readOnly = renderToString(createElement(AdminPublicationTermsPage))
    expect(readOnly).toContain('全站发布词表')
    expect(readOnly).toContain('晨光')
    expect(readOnly).not.toMatch(/新增词条|编辑「|删除「/)
    as(['publication_terms.read', 'publication_terms.update'])
    expect(renderToString(createElement(AdminPublicationTermsPage))).toContain('编辑「晨光」')
  })
})
