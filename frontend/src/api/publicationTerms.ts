import { request, type RequestOptions } from './client'

export type PublicationTermScope = 'personal' | 'global'
export interface PublicationTerms {
  terms: string[]
  revision: number
}

function path(scope: PublicationTermScope) {
  return scope === 'global' ? '/admin/publication-terms' : '/publication-terms'
}

export const publicationTermsApi = {
  get: (scope: PublicationTermScope, options: Pick<RequestOptions, 'signal' | 'isCurrent' | 'editorActor'> = {}) =>
    request<PublicationTerms>(path(scope), options),
  update: (scope: PublicationTermScope, terms: string[], expectedRevision: number, options: Pick<RequestOptions, 'isCurrent' | 'editorActor'>) =>
    request<PublicationTerms>(path(scope), { ...options, method: 'PUT', body: { terms, expected_revision: expectedRevision } }),
}
