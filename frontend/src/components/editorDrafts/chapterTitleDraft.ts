import type { ChapterTitleSave } from '../../api/types'
import { trimSceneTitle as normalizedChapterTitle } from '../scenes/sceneDraftRecord'
import type { TitleDraftPayload } from './editorDraftRecord'

type TitleBase = TitleDraftPayload['base']
type TitleAttempt = NonNullable<TitleDraftPayload['attempt']>

export interface ChapterTitleState {
  base: TitleBase
  value: string
  anchor: number | null
  editSeq: number
  attempt: TitleAttempt | null
  unknown: boolean
  retryable: boolean
  conflict: TitleBase | null
  targetLost: boolean
}

const titleChanged = (value: string, base: string) => value !== base && normalizedChapterTitle(value) !== base

export const chapterTitleDirty = (state: ChapterTitleState) => titleChanged(state.value, state.base.title)

const sameBase = (a: TitleBase, b: TitleBase) => a.revision === b.revision && a.title === b.title

/** 本地输入、请求快照、已确认标题分别推进。 */
export class ChapterTitleDraft {
  private state: ChapterTitleState
  private readonly listeners = new Set<() => void>()

  constructor(base: TitleBase) {
    this.state = {
      base, value: base.title, anchor: null, editSeq: 0,
      attempt: null, unknown: false, retryable: false, conflict: null, targetLost: false,
    }
  }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  private publish(change: Partial<ChapterTitleState>) {
    this.state = { ...this.state, ...change }
    for (const listener of this.listeners) listener()
  }

  edit(value: string, sceneId: number) {
    if (value === this.state.value) return
    const anchor = this.state.anchor ?? sceneId
    this.publish({ value, anchor, editSeq: this.state.editSeq + 1 })
  }

  capture(): TitleDraftPayload {
    const { base, value, attempt } = this.state
    return { base, value, ...(attempt ? { attempt } : {}) }
  }

  freeze(id: string, again = false): { attempt: TitleAttempt; body: ChapterTitleSave } | null {
    const state = this.state
    if (!state.anchor || state.targetLost || state.conflict) return null
    if (again ? !state.retryable || !state.attempt : !!state.attempt || !chapterTitleDirty(state)) return null
    const attempt = again && state.attempt ? state.attempt : {
      id, title: normalizedChapterTitle(state.value), expectedTitle: state.base.title,
      revision: state.base.revision, editSeq: state.editSeq,
    }
    this.publish({ attempt, unknown: false, retryable: false })
    return { attempt, body: {
      title: attempt.title, expected_title: attempt.expectedTitle,
      expected_scene_id: state.anchor, expected_content_revision: attempt.revision,
    } }
  }

  failed(attempt: TitleAttempt, unknown: boolean) {
    if (this.state.attempt !== attempt) return
    this.publish({ attempt: unknown ? attempt : null, unknown, retryable: false })
  }

  accept(attempt: TitleAttempt, base: TitleBase): boolean {
    if (this.state.attempt !== attempt || !this.matchesAttempt(attempt, base)) return false
    const value = this.state.editSeq === attempt.editSeq ? base.title : this.state.value
    this.publish({ base, value, anchor: titleChanged(value, base.title) ? this.state.anchor : null,
      attempt: null, unknown: false, retryable: false, conflict: null })
    return true
  }

  /** 场景写入成功的回执可以消耗这个已知版本；普通 GET 不能在草稿上自动切换基线。 */
  advanceOwnSceneRevision(from: number, to: number) {
    if (this.state.attempt || this.state.conflict || this.state.base.revision !== from || to <= from) return
    this.publish({ base: { ...this.state.base, revision: to } })
  }

  observe(remote: TitleBase) {
    const state = this.state
    if (remote.revision < state.base.revision) return
    if (state.attempt && !state.unknown) return
    if (state.targetLost) return
    if (state.unknown && state.attempt) {
      if (this.matchesAttempt(state.attempt, remote)) {
        this.accept(state.attempt, remote)
      } else if (sameBase(state.base, remote)) {
        if (!state.retryable || state.conflict) this.publish({ retryable: true, conflict: null })
      } else if (remote.revision > state.attempt.revision) {
        // 单调修订号已消耗，旧请求不再可能提交。
        this.publish({ attempt: null, unknown: false, retryable: false, conflict: remote })
      } else {
        this.publish({ retryable: false, conflict: remote })
      }
    } else if (sameBase(state.base, remote)) {
      if (state.conflict) this.publish({ conflict: null })
    } else if (chapterTitleDirty(state)) {
      if (!state.conflict || !sameBase(state.conflict, remote)) this.publish({ conflict: remote })
    } else {
      this.publish({ base: remote, value: remote.title, anchor: null, conflict: null })
    }
  }

  restore(payload: TitleDraftPayload, anchor: number, inputSeq: number, remote: TitleBase) {
    this.publish({ base: payload.base, value: payload.value, anchor,
      editSeq: Math.max(inputSeq, this.state.editSeq + 1), attempt: payload.attempt ?? null,
      unknown: !!payload.attempt, retryable: false, conflict: null, targetLost: false })
    this.observe(remote)
  }

  loseTarget() {
    if (this.state.anchor !== null && !this.state.targetLost) this.publish({ targetLost: true, retryable: false })
  }

  stamp(draftId: string | undefined): string {
    const { conflict, editSeq } = this.state
    return JSON.stringify([draftId, this.state.anchor, conflict?.revision, conflict?.title, editSeq])
  }

  resolve(choice: 'keep-local' | 'use-remote', stamp: string, draftId: string | undefined): boolean {
    const state = this.state
    if (!state.conflict || state.targetLost || state.attempt || this.stamp(draftId) !== stamp) return false
    const value = choice === 'use-remote' ? state.conflict.title : state.value
    this.publish({ base: state.conflict, value,
      anchor: titleChanged(value, state.conflict.title) ? state.anchor : null,
      conflict: null, unknown: false, retryable: false })
    return true
  }

  private matchesAttempt(attempt: TitleAttempt, remote: TitleBase) {
    return remote.revision === attempt.revision + 1 && remote.title === attempt.title
  }
}
