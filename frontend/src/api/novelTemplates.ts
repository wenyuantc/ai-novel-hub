import { buildUrl, currentRequestGeneration, request, type RequestOptions } from './client'
import type { Career, Character, Organization, Outline, Project, WorldSetting } from './types'

type Scope = Pick<RequestOptions, 'signal' | 'isCurrent'>
export type NovelTemplateKind = 'character' | 'world' | 'organization' | 'career'

export interface NovelTemplateSelection {
  include_config: boolean
  character_ids: number[]
  world_ids: number[]
  organization_ids: number[]
  career_ids: number[]
  outline_ids: number[]
}

export interface NovelTemplateDependency {
  kind: NovelTemplateKind
  id: number
  name: string
  reason: string
}

export interface NovelTemplateCounts {
  characters: number
  world_settings: number
  organizations: number
  careers: number
  outlines: number
  plans: number
  images: number
}

export interface NovelTemplatePreview {
  source_stamp: string
  missing_dependencies: NovelTemplateDependency[]
  included_relations: number
  omitted_relations: number
  counts: NovelTemplateCounts
  estimated_bytes: number
  json_bytes: number
  image_bytes: number
  warnings: string[]
  included_definitions: TemplateIncludedDefinitions
}

export interface TemplateIncludedDefinitions {
  fields: { name: string; field_type: string; options: string[] }[]
  categories: string[]
  relationship_types: { origin: string; category: string; role_a: string; role_b: string }[]
  careers: { name: string; stages: string[] }[]
}

export interface NovelTemplateSummary {
  id: number
  name: string
  description: string
  revision: number
  source_title: string
  counts: NovelTemplateCounts
  created_at: string
  updated_at: string
}

export interface NovelTemplatePage {
  items: NovelTemplateSummary[]
  total: number
  page: number
  page_size: number
}

export interface NovelTemplateDetail extends NovelTemplateSummary {
  include_config: boolean
  config_summary: NovelTemplateConfigSummary | null
  contents: Record<NovelTemplateContentGroup, NovelTemplateContent[]>
}

export interface NovelTemplateConfigSummary {
  genre: string
  premise: string
  default_style_name: string | null
  field_count: number
  category_count: number
}

export type NovelTemplateContentGroup = 'characters' | 'world_settings' | 'organizations' | 'careers' | 'outlines'
export interface NovelTemplateContent { ref: string; name: string; has_image: boolean }

export interface NovelTemplateCapture {
  source_project_id: number
  selection: NovelTemplateSelection
  source_stamp: string
}

export interface NovelTemplateProjectRequest {
  expected_revision: number
  intent_key: string
  title: string
  pen_name_id: number | null
  overrides?: NovelTemplateOverrides
}

export interface NovelTemplateOverrides {
  genre?: string
  premise?: string
  synopsis?: string
  theme?: string
  tags?: string[]
  selling_points?: string
  audience?: string
  target_words?: number | null
}

export interface TemplateCandidates {
  characters: Character[]
  world: WorldSetting[]
  organizations: Organization[]
  careers: Career[]
  outlines: Outline[]
}

const base = '/novel-templates'

/** 模板库是当前用户的私人资源，所有请求遵守当前组件身份守卫。 */
export const novelTemplatesApi = {
  list: (query: { page: number; page_size: number; q?: string }, scope?: Scope) =>
    request<NovelTemplatePage>(base, { query, ...scope }),
  get: (id: number, scope?: Scope, query?: { group?: NovelTemplateContentGroup; page?: number; page_size?: number }) =>
    request<NovelTemplateDetail>(`${base}/${id}`, { query, ...scope }),
  preview: (projectId: number, selection: NovelTemplateSelection, scope?: Scope) =>
    request<NovelTemplatePreview>(`/projects/${projectId}/novel-template-preview`, { method: 'POST', body: selection, ...scope }),
  create: (body: NovelTemplateCapture & { name: string; description: string }, scope?: Scope) =>
    request<NovelTemplateSummary>(base, { method: 'POST', body, ...scope }),
  rename: (id: number, body: { name: string; description: string; expected_revision: number }, scope?: Scope) =>
    request<NovelTemplateSummary>(`${base}/${id}`, { method: 'PATCH', body, ...scope }),
  replace: (id: number, body: NovelTemplateCapture & { expected_revision: number }, scope?: Scope) =>
    request<NovelTemplateSummary>(`${base}/${id}/snapshot`, { method: 'PUT', body, ...scope }),
  remove: (id: number, revision: number, scope?: Scope) =>
    request<void>(`${base}/${id}`, { method: 'DELETE', query: { expected_revision: revision }, ...scope }),
  createProject: (id: number, body: NovelTemplateProjectRequest, scope?: Scope) =>
    request<Project>(`${base}/${id}/projects`, { method: 'POST', body, ...scope }),
  imageUrl: (id: number, ref: string, revision: number) =>
    buildUrl(`${base}/${id}/images/${encodeURIComponent(ref)}`, { revision, g: currentRequestGeneration() }),
  candidates: async (projectId: number, scope?: Scope): Promise<TemplateCandidates> => {
    const prefix = `/projects/${projectId}`
    const [characters, world, organizations, careers, outlines] = await Promise.all([
      request<Character[]>(`${prefix}/characters`, scope),
      request<WorldSetting[]>(`${prefix}/world`, scope),
      request<Organization[]>(`${prefix}/organizations`, scope),
      request<Career[]>(`${prefix}/careers`, scope),
      request<Outline[]>(`${prefix}/outlines`, scope),
    ])
    return { characters, world, organizations, careers, outlines }
  },
}
