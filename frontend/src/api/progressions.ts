import { request } from './client'
import { storyPositionQuery } from '../utils/storyPosition'
import type {
  ChapterSettingContext,
  ProgressionBaselineStatus,
  ProgressionBoundary,
  ProgressionList,
  ProgressionMutation,
  ProgressionPositionInput,
  ProgressionRevision,
  ProgressionSubject,
  ProgressionSubjectFilter,
  SettingBaseline,
  SettingBaselineUpdate,
  SettingEffective,
  SettingProgression,
  SettingProgressionInput,
  SettingProgressionUpdate,
  SettingTextField,
  SettingTextFieldInput,
  SettingTextFieldUpdate,
  SettingTimeline,
  WorldSetting,
  WorldSettingInput,
} from './types'

const base = (projectId: number) => `/projects/${projectId}/setting-timelines`
const timeline = (projectId: number, timelineId: number) => `${base(projectId)}/${timelineId}`

export const progressionApi = {
  listTimelines: (projectId: number, filter: ProgressionSubjectFilter = {}, signal?: AbortSignal) =>
    request<{ items: SettingTimeline[]; total: number }>(base(projectId), { query: { ...filter }, signal }),
  async findTimeline(projectId: number, subject: ProgressionSubject, signal?: AbortSignal) {
    const result = await request<{ items: SettingTimeline[]; total: number }>(base(projectId), {
      query: { ...subject }, signal,
    })
    return result.items[0] ?? null
  },
  getBaseline: (projectId: number, timelineId: number, signal?: AbortSignal) =>
    request<SettingBaseline>(`${timeline(projectId, timelineId)}/baseline`, { signal }),
  getWorldSetting: (projectId: number, worldId: number, signal?: AbortSignal) =>
    request<WorldSetting>(`/projects/${projectId}/world/${worldId}`, { signal }),
  updateBaseline: (projectId: number, timelineId: number, body: SettingBaselineUpdate) =>
    request<SettingBaseline>(`${timeline(projectId, timelineId)}/baseline`, { method: 'PATCH', body }),
  updateWorldBaseline: (
    projectId: number,
    worldId: number,
    body: Partial<WorldSettingInput> & { edit_mode: 'baseline'; expected_revision: number },
  ) => request<WorldSetting>(`/projects/${projectId}/world/${worldId}`, { method: 'PATCH', body }),
  listProgressions: (
    projectId: number,
    timelineId: number,
    options: { offset?: number; limit?: number; field?: string } = {},
    signal?: AbortSignal,
  ) => request<ProgressionList<SettingProgression>>(`${timeline(projectId, timelineId)}/progressions`, {
    query: options, signal,
  }),
  getProgression: (projectId: number, timelineId: number, id: number, signal?: AbortSignal) =>
    request<ProgressionMutation<SettingProgression>>(`${timeline(projectId, timelineId)}/progressions/${id}`, { signal }),
  createProgression: (projectId: number, timelineId: number, body: SettingProgressionInput) =>
    request<ProgressionMutation<SettingProgression>>(`${timeline(projectId, timelineId)}/progressions`, {
      method: 'POST', body,
    }),
  updateProgression: (projectId: number, timelineId: number, id: number, body: SettingProgressionUpdate) =>
    request<ProgressionMutation<SettingProgression>>(`${timeline(projectId, timelineId)}/progressions/${id}`, {
      method: 'PATCH', body,
    }),
  removeProgression: (projectId: number, timelineId: number, id: number, expectedRevision: number, expectedFieldsRevision?: number) =>
    request<ProgressionRevision>(`${timeline(projectId, timelineId)}/progressions/${id}`, {
      method: 'DELETE', query: { expected_revision: expectedRevision, expected_fields_revision: expectedFieldsRevision },
    }),
  effective: (
    projectId: number,
    timelineId: number,
    chapterId: number,
    boundary: ProgressionBoundary,
    signal?: AbortSignal,
  ) => request<SettingEffective>(`${timeline(projectId, timelineId)}/effective`, {
    query: { chapter_id: chapterId, boundary }, signal,
  }),
  effectiveAtPosition: (
    projectId: number,
    timelineId: number,
    position: ProgressionPositionInput,
    signal?: AbortSignal,
  ) => {
    const query = storyPositionQuery(position)
    return request<SettingEffective>(`${timeline(projectId, timelineId)}/effective`, { query: { ...query }, signal })
  },
  chapterSettingContext: (projectId: number, chapterId: number, signal?: AbortSignal) =>
    request<ChapterSettingContext>(`/projects/${projectId}/chapters/${chapterId}/setting-context`, { signal }),
  listTextFields: (projectId: number, timelineId: number, signal?: AbortSignal) =>
    request<ProgressionList<SettingTextField>>(`${timeline(projectId, timelineId)}/text-fields`, { signal }),
  createTextField: (projectId: number, timelineId: number, body: SettingTextFieldInput) =>
    request<ProgressionMutation<SettingTextField>>(`${timeline(projectId, timelineId)}/text-fields`, {
      method: 'POST', body,
    }),
  updateTextField: (projectId: number, timelineId: number, id: number, body: SettingTextFieldUpdate) =>
    request<ProgressionMutation<SettingTextField>>(`${timeline(projectId, timelineId)}/text-fields/${id}`, {
      method: 'PATCH', body,
    }),
  removeTextField: (projectId: number, timelineId: number, id: number, expectedRevision: number) =>
    request<ProgressionRevision>(`${timeline(projectId, timelineId)}/text-fields/${id}`, {
      method: 'DELETE', query: { expected_revision: expectedRevision },
    }),
}

export function baselineStatusLabel(status: ProgressionBaselineStatus): string {
  return status === 'legacy_unverified' ? '旧资料待校准' : '基础设定已确认'
}

/** 只读用户看到查看提示，避免把不可编辑的资料误称为正在编辑。 */
export function baselineEditorLabel(status: ProgressionBaselineStatus, canEdit: boolean): string {
  return status === 'legacy_unverified' ? '旧资料待校准' : canEdit ? '正在编辑基础设定' : '正在查看基础设定'
}
