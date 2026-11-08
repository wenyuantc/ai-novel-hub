import { Extension } from '@tiptap/core'
import Document from '@tiptap/extension-document'
import { closeHistory, isHistoryTransaction } from '@tiptap/pm/history'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { Decoration, DecorationSet } from '@tiptap/pm/view'
import type { RichDocument, RichMarker } from '../../api/richDocument'
import { mapMarkerTransaction, snapshotInvalidatedMarker } from './richMarkerMapping'
import { mapSectionMarkerTransaction } from './richSectionMarkerMapping'
import { projectSectionDocument } from './richSectionProjection'
import { mappedSectionIdentities, verifySectionMetadataTransaction, verifySectionPlanTransaction } from './richSectionTransaction'
import { projectEditorDocument } from './richTextProjection'

/** 标记元数据与正文步骤进入同一个 ProseMirror 历史，不能靠文字重定位。 */
export const RichMarkerDocument = Document.extend({
  addAttributes() { return { markers: { default: [] }, retiredSectionKeys: { default: [] }, sectionOrders: { default: {} } } },
})

const COLOR_NAMES = { yellow: '黄色', green: '绿色', blue: '蓝色', purple: '紫色', red: '红色' }

/** 每字符只扫描一次；交叠标记以共同下划线和数量提示呈现，不依赖颜色区分。 */
export function markerDecorations(state: import('@tiptap/pm/state').EditorState): DecorationSet {
  type MarkerEvent = { point: number; color: RichMarker['color']; delta: number }
  interface Scope { events: MarkerEvent[]; active: Map<RichMarker['color'], number>; event: number; offset: number; label: string }
  const markers = state.doc.attrs.markers as RichMarker[]
  const scopes = new Map<string | null, Scope>()
  for (const marker of markers) {
    if (marker.status !== 'active') continue
    let scope = scopes.get(marker.section_key)
    if (!scope) {
      scope = { events: [], active: new Map(), event: 0, offset: 0, label: marker.section_key === null ? '正文' : '区块' }
      scopes.set(marker.section_key, scope)
    }
    scope.events.push({ point: marker.start!, color: marker.color, delta: 1 }, { point: marker.end!, color: marker.color, delta: -1 })
  }
  if (!scopes.size) return DecorationSet.empty
  for (const scope of scopes.values()) scope.events.sort((a, b) => a.point - b.point)
  const decorations: Decoration[] = []
  const advance = (scope: Scope) => {
    while (scope.event < scope.events.length && scope.events[scope.event].point <= scope.offset) {
      const item = scope.events[scope.event++]
      const count = (scope.active.get(item.color) ?? 0) + item.delta
      if (count) scope.active.set(item.color, count); else scope.active.delete(item.color)
    }
  }
  const visit = (node: import('@tiptap/pm/model').Node, pos: number, scope: Scope) => {
    if (node.isTextblock) { scope.offset += Array.from(node.attrs.prefix || '').length; advance(scope) }
    if (!node.isText || node.text === undefined) return
    let pm = pos; let segmentStart = pm; let segmentKey = ''; let title = ''
    const flush = () => {
      if (segmentKey && pm > segmentStart) decorations.push(Decoration.inline(segmentStart, pm, {
        class: `rich-marker ${segmentKey}`, title, 'aria-description': title,
      }))
    }
    for (const char of node.text) {
      advance(scope)
      const colors = [...scope.active.keys()].sort()
      const key = colors.map((color) => `rich-marker-${color}`).join(' ')
      const count = [...scope.active.values()].reduce((sum, value) => sum + value, 0)
      const nextTitle = count ? `${colors.map((color) => COLOR_NAMES[color]).join('、')}${scope.label}标记，共 ${count} 条` : ''
      if (key !== segmentKey || title !== nextTitle) { flush(); segmentStart = pm; segmentKey = key; title = nextTitle }
      pm += char.length; scope.offset++
    }
    flush()
  }
  state.doc.descendants((node, pos) => {
    if (node.type.name === 'authorSection') {
      const scope = scopes.get(node.attrs.key)
      if (scope) node.descendants((child, offset) => { visit(child, pos + 1 + offset, scope) })
      return false // 区块文字不计入正式正文坐标。
    }
    const prose = scopes.get(null)
    if (prose) visit(node, pos, prose)
  })
  return DecorationSet.create(state.doc, decorations)
}

export const RichMarkerState = Extension.create<{
  getSavedDocument: () => RichDocument | null; onCompositionChange: (composing: boolean) => void
  onCompositionCommit: (mappedKeys: string[], mappedSectionKeys: string[]) => void; sectionMode: boolean
}>({
  addOptions() { return { getSavedDocument: () => null, onCompositionChange: () => {}, onCompositionCommit: () => {}, sectionMode: false } },
  name: 'richMarkerState', priority: 2400,
  addProseMirrorPlugins() {
    const editor = this.editor
    const { getSavedDocument, onCompositionChange, onCompositionCommit, sectionMode } = this.options
    const mapTransaction = sectionMode ? mapSectionMarkerTransaction : mapMarkerTransaction
    const key = new PluginKey<DecorationSet>('richMarkerDecorations')
    let composing = false
    let pending: RichMarker[] = []
    let pendingSectionKeys: string[] = []
    let compositionId = -1
    let nextCompositionId = -2
    let finishTimer: ReturnType<typeof setTimeout> | undefined
    const reject = () => queueMicrotask(() => { if (!editor.isDestroyed) editor.view.dom.dispatchEvent(new CustomEvent('rich-mapping-rejected')) })
    return [new Plugin({
    key,
    state: {
      init(_config, state) { return markerDecorations(state) },
      apply(tr, old, _oldState, nextState) {
        // 组合期间只映射已有装饰，不重建外层 DOM，避免打断浏览器的临时输入。
        return composing ? old.map(tr.mapping, tr.doc) : markerDecorations(nextState)
      },
    },
    props: {
      decorations: (state) => key.getState(state) ?? DecorationSet.empty,
      handleDOMEvents: {
        compositionstart(view) {
          if (finishTimer) { clearTimeout(finishTimer); finishTimer = undefined }
          if (!composing) {
            composing = true; pending = (view.state.doc.attrs.markers as RichMarker[]).map((item) => ({ ...item }))
            pendingSectionKeys = sectionMode ? projectSectionDocument(view.state.doc.toJSON()).sections.map((item) => item.key) : []
            compositionId = nextCompositionId--; onCompositionChange(true)
            // 单次组合使用独立历史轮次；即使首个候选延迟，也不能拆开选区删除与标记状态。
            const { from, to, empty, $from, $to } = view.state.selection
            const start = closeHistory(view.state.tr).setMeta('composition', compositionId)
            if (!empty && !$from.sameParent($to)) start.delete(from, to)
            view.dispatch(start)
          }
          return false
        },
        compositionend(view) {
          if (!composing) return false
          if (finishTimer) clearTimeout(finishTimer)
          // PM 在 compositionend 后先 flush DOM observer；随后一次性提交事务元数据。
          finishTimer = setTimeout(() => {
            finishTimer = undefined
            if (view.isDestroyed || !composing) return
            composing = false
            try {
              if (JSON.stringify(view.state.doc.attrs.markers) !== JSON.stringify(pending)) {
                const transaction = view.state.tr.setDocAttribute('markers', pending).setMeta('rich-marker-sync', true)
                  .setMeta('rich-mapped-marker-keys', pending.map((item) => item.key))
                transaction.setMeta('composition', compositionId)
                view.dispatch(transaction)
              }
              // 即使标记数组不变，Tiptap 也可能不触发 update；这里显式交付整个组合后的正文。
              onCompositionCommit(pending.map((item) => item.key), pendingSectionKeys)
              onCompositionChange(false)
            } catch {
              // 状态仍保持“组合中”，禁止保存未同步的元数据；作者可复制可见正文。
              view.dom.dispatchEvent(new CustomEvent('rich-composition-rejected'))
            }
          }, 0)
          return false
        },
      },
    },
    view() { return { destroy() {
      if (finishTimer) clearTimeout(finishTimer)
      if (composing) { composing = false; onCompositionChange(false) }
    } } },
    filterTransaction(transaction, state) {
      if (!transaction.docChanged || transaction.getMeta('rich-marker-sync') || transaction.getMeta('rich-prefix-fix')) return true
      if (composing && (transaction.getMeta('rich-marker-intent') || transaction.getMeta('rich-section-plan')
        || transaction.getMeta('rich-section-intent') || isHistoryTransaction(transaction))) { reject(); return false }
      // 让 PM history 把本轮所有步骤和末尾元数据视作同一组合事务，不依赖 500ms 时间窗。
      if (composing) transaction.setMeta('composition', compositionId)
      if (sectionMode && composing) {
        const mapped = mappedSectionIdentities(transaction)
        if (!mapped) { reject(); return false }
        pendingSectionKeys = pendingSectionKeys.filter((key) => mapped.includes(key))
      }
      if (sectionMode && transaction.getMeta('rich-section-plan')) {
        const saved = getSavedDocument(); const valid = !!saved && verifySectionPlanTransaction(transaction, state, saved)
        if (!valid) reject()
        return valid
      }
      if (sectionMode && transaction.getMeta('rich-section-intent')) {
        const valid = verifySectionMetadataTransaction(transaction, state)
        if (!valid) reject()
        return valid
      }
      if (transaction.getMeta('rich-marker-intent') || isHistoryTransaction(transaction)) return true
      const markers = composing ? pending : state.doc.attrs.markers as RichMarker[]
      const valid = !markers.length || mapTransaction(transaction, markers, getSavedDocument()) !== null
      if (!valid) reject()
      return valid
    },
    appendTransaction(transactions, oldState, newState) {
      if (transactions.some((tr) => tr.getMeta('rich-marker-sync') || tr.getMeta('rich-marker-intent')
        || sectionMode && (tr.getMeta('rich-section-plan') || tr.getMeta('rich-section-intent')))) return null
      if (composing) {
        for (const transaction of transactions) {
          if (!transaction.docChanged || transaction.getMeta('rich-prefix-fix')) continue
          const next = mapTransaction(transaction, pending, getSavedDocument())
          if (!next) { reject(); return null }
          pending = next
        }
        return null
      }
      if (transactions.some(isHistoryTransaction)) {
        // 撤销/重做可跨越一次保存；当前服务端基线已改变时重新保留两种真实片段。
        const oldMarkers = new Map<string, RichMarker>((oldState.doc.attrs.markers as RichMarker[]).map((item) => [item.key, item]))
        const nextMarkers = newState.doc.attrs.markers as RichMarker[]
        const document = sectionMode ? projectSectionDocument(oldState.doc.toJSON()) : projectEditorDocument(oldState.doc.toJSON())
        let changed = false
        const normalized = nextMarkers.map((item) => {
          const old = oldMarkers.get(item.key)
          if (item.status !== 'stale' || !old || old.status !== 'active') return item
          const content = old.section_key === null ? document.content : document.sections.find((section) => section.key === old.section_key)?.content
          if (content === undefined) return item
          const captured = snapshotInvalidatedMarker(old, Array.from(content), getSavedDocument())
          // 历史重做可能将多次编辑合为一组；原事务捕获的草稿片段不能被组起点覆盖。
          const hasDraft = item.draft_excerpt != null
          const result = { ...item, excerpt: captured.excerpt, excerpt_truncated: captured.excerpt_truncated,
            last_start: captured.last_start, draft_excerpt: hasDraft ? item.draft_excerpt : captured.draft_excerpt,
            draft_excerpt_truncated: hasDraft ? item.draft_excerpt_truncated : captured.draft_excerpt_truncated,
            draft_last_start: hasDraft ? item.draft_last_start : captured.draft_last_start }
          if (JSON.stringify(item) !== JSON.stringify(result)) changed = true
          return result
        })
        return changed ? newState.tr.setDocAttribute('markers', normalized).setMeta('rich-marker-sync', true)
          .setMeta('rich-mapped-marker-keys', normalized.map((item) => item.key)) : null
      }
      let markers = oldState.doc.attrs.markers as RichMarker[]
      if (!markers.length) return null
      let checked = false
      for (const transaction of transactions) {
        if (!transaction.docChanged || transaction.getMeta('rich-prefix-fix')) continue
        const next = mapTransaction(transaction, markers, getSavedDocument())
        if (!next) return null // filterTransaction 已拒绝，不应以旧位置覆盖。
        markers = next; checked = true
      }
      if (!checked) return null
      return newState.tr.setDocAttribute('markers', markers).setMeta('rich-marker-sync', true)
        .setMeta('rich-mapped-marker-keys', markers.map((item) => item.key))
    },
  })] },
})
