import { Node } from '@tiptap/core'
import { NodeSelection, Selection } from '@tiptap/pm/state'
import './richSectionNode.css'

// 与后端 Python str.strip() 的空白字符保持一致；名称本身不做 trim 或截断。
function isPythonWhitespace(char: string): boolean {
  const code = char.codePointAt(0)!
  return (code >= 0x09 && code <= 0x0d) || (code >= 0x1c && code <= 0x20)
    || (code >= 0x2000 && code <= 0x200a)
    || [0x85, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000].includes(code)
}

export function validateSectionTitle(title: string): string | null {
  const characters = Array.from(title)
  if (characters.every(isPythonWhitespace)) return '请输入区块名称。'
  if (characters.length > 200) return '区块名称不能超过 200 个字符。'
  return null
}

type SectionAction = 'delete' | 'convert' | 'move-up' | 'move-down' | 'move-to-cursor' | 'rewrite-selection'

let nextTitleHelpId = 0

/**
 * 区块本体始终是 ProseMirror 子文档。界面只更新名称和 AI 开关；
 * 涉及位置、删除和出站的操作交给宿主验证并处理。
 */
export const AuthorSection = Node.create({
  name: 'authorSection',
  group: 'block',
  content: '(paragraph|heading|blockquote|bulletList|orderedList)+',
  defining: true,
  isolating: true,
  draggable: true,

  addAttributes() {
    return {
      key: { default: '', rendered: false },
      title: { default: '', rendered: false },
      include_in_ai: { default: false, rendered: false },
    }
  },

  // 不从外部 HTML 接受区块身份；即使粘贴 HTML 也不能伪造作者区块。
  renderHTML() {
    return ['div', 0]
  },

  addNodeView() {
    return ({ node: initialNode, editor, view, getPos }) => {
      let node = initialNode
      let collapsed = false
      let composing = false
      let destroyed = false

      const dom = document.createElement('section')
      dom.className = 'rich-author-section'
      const header = document.createElement('div')
      header.className = 'rich-author-section__header'
      header.contentEditable = 'false'
      const contentDOM = document.createElement('div')
      contentDOM.className = 'rich-author-section__content'
      dom.append(header, contentDOM)

      const top = document.createElement('div')
      top.className = 'rich-author-section__top'
      const nameLabel = document.createElement('label')
      nameLabel.className = 'rich-author-section__name'
      const nameCaption = document.createElement('span')
      nameCaption.textContent = '区块名称'
      const nameInput = document.createElement('input')
      nameInput.type = 'text'
      nameInput.setAttribute('aria-label', '区块名称')
      nameInput.setAttribute('aria-required', 'true')
      const titleHelp = document.createElement('span')
      titleHelp.className = 'rich-author-section__error'
      titleHelp.id = `rich-section-title-help-${++nextTitleHelpId}`
      titleHelp.setAttribute('aria-live', 'polite')
      nameInput.setAttribute('aria-describedby', titleHelp.id)
      nameLabel.append(nameCaption, nameInput)
      top.append(nameLabel)

      const aiLabel = document.createElement('label')
      aiLabel.className = 'rich-author-section__ai'
      const aiInput = document.createElement('input')
      aiInput.type = 'checkbox'
      const aiCaption = document.createElement('span')
      aiCaption.textContent = '允许 AI 自动使用'
      aiLabel.append(aiInput, aiCaption)
      top.append(aiLabel)
      header.append(top, titleHelp)

      const info = document.createElement('details')
      info.className = 'rich-author-section__info'
      const infoSummary = document.createElement('summary')
      infoSummary.textContent = 'AI 范围说明'
      const infoText = document.createElement('p')
      infoText.textContent = '关闭后，区块不会被未来自动获取；之前生成的摘要、聊天和记忆仍可能被召回。你也可以明确选择区块中的文字，确认后仅将该选区用于本次 AI 编辑。'
      info.append(infoSummary, infoText)
      header.append(info)

      const actions = document.createElement('div')
      actions.className = 'rich-author-section__actions'
      header.append(actions)
      const foldNotice = document.createElement('p')
      foldNotice.className = 'rich-author-section__error'
      foldNotice.setAttribute('aria-live', 'polite')
      foldNotice.hidden = true
      header.append(foldNotice)

      const button = (text: string, label = text) => {
        const result = document.createElement('button')
        result.type = 'button'
        result.textContent = text
        result.setAttribute('aria-label', label)
        actions.append(result)
        return result
      }

      const fold = button('折叠', '折叠区块')
      contentDOM.id = `${titleHelp.id}-content`
      fold.setAttribute('aria-controls', contentDOM.id)
      const drag = button('拖动', '拖动区块')
      drag.draggable = true
      drag.setAttribute('data-drag-handle', '')
      const up = button('上移', '区块上移')
      const down = button('下移', '区块下移')
      const moveToCursor = button('移至光标', '移动到正文光标')
      moveToCursor.title = '先在正式正文放置光标，再使用此按钮移动区块。'
      const convert = button('转为正文')
      const remove = button('删除区块')
      const rewrite = button('选区 AI 编辑')
      const editButtons = [drag, up, down, moveToCursor, convert, remove]

      // 初次创建 NodeView 时 Editor.editorView 尚未赋值，不能用 editor.isDestroyed 判断。
      const canEdit = () => !destroyed && editor.options.editable && view.editable
      // 人工管理他人作品时允许编辑区块，但不能调用作品所有者的 AI 能力。
      const canRewrite = () => canEdit() && view.dom.getAttribute('data-section-ai-enabled') !== 'false'
      const locate = () => {
        if (destroyed) return null
        const pos = getPos()
        if (typeof pos !== 'number') return null
        const found = view.state.doc.nodeAt(pos)
        if (!found || found.type !== initialNode.type || found.attrs.key !== initialNode.attrs.key) return null
        return { pos, node: found }
      }
      const current = () => canEdit() ? locate() : null

      const visibleTextSelection = (sectionPos: number, sectionSize: number): Selection | null => {
        let after: Selection | null = null
        let before: Selection | null = null
        view.state.doc.forEach((child, offset) => {
          // 不能把光标转入另一个可能已经折叠的区块。
          if (child.type.name === 'authorSection') return
          const end = offset + child.nodeSize
          const direction = offset >= sectionPos + sectionSize ? 1 : end <= sectionPos ? -1 : 0
          if (!direction) return
          const candidate = Selection.findFrom(view.state.doc.resolve(direction > 0 ? offset : end), direction, true)
          if (!candidate || candidate.from <= offset || candidate.to >= end) return
          if (direction > 0 && !after) after = candidate
          if (direction < 0) before = candidate
        })
        return after ?? before
      }

      const dispatchComposition = (active: boolean) => {
        dom.dispatchEvent(new CustomEvent('rich-section-composition', {
          bubbles: true, detail: { key: initialNode.attrs.key, active },
        }))
      }

      const showTitleValidity = (title: string) => {
        const problem = validateSectionTitle(title)
        nameInput.setAttribute('aria-invalid', String(problem !== null))
        titleHelp.textContent = problem ?? ''
        titleHelp.hidden = problem === null
      }

      const refreshNode = () => {
        if (!composing) nameInput.value = node.attrs.title
        aiInput.checked = node.attrs.include_in_ai === true
        showTitleValidity(nameInput.value)
      }

      const refreshEditable = () => {
        const editable = canEdit()
        nameInput.readOnly = !editable
        aiInput.disabled = !editable
        for (const control of editButtons) control.disabled = !editable
        rewrite.disabled = !canRewrite()
        rewrite.title = editable && rewrite.disabled ? '仅作品所有者可以使用 AI 编辑。' : ''
        if (!editable && composing) {
          composing = false
          refreshNode()
          dispatchComposition(false)
        }
      }

      const setMetadata = (patch: { title?: string; include_in_ai?: boolean }) => {
        const target = current()
        if (!target) { refreshNode(); return }
        const unchanged = (patch.title === undefined || target.node.attrs.title === patch.title)
          && (patch.include_in_ai === undefined || target.node.attrs.include_in_ai === patch.include_in_ai)
        if (!unchanged) {
          view.dispatch(view.state.tr.setNodeMarkup(target.pos, undefined, { ...target.node.attrs, ...patch })
            .setMeta('rich-section-intent', { action: 'metadata', key: initialNode.attrs.key }))
        }
        // 插件可能拒绝事务，控件应反映文档中的实际值。
        const after = current()
        if (after) node = after.node
        refreshNode()
      }

      nameInput.addEventListener('compositionstart', () => {
        if (!current() || composing) return
        composing = true
        dispatchComposition(true)
      })
      nameInput.addEventListener('input', () => {
        showTitleValidity(nameInput.value)
        if (!composing) setMetadata({ title: nameInput.value })
      })
      nameInput.addEventListener('compositionend', () => {
        if (!composing) return
        composing = false
        setMetadata({ title: nameInput.value })
        dispatchComposition(false)
      })
      nameInput.addEventListener('keydown', (event) => {
        if (composing && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
          event.preventDefault()
          event.stopPropagation()
        }
      })
      aiInput.addEventListener('change', () => setMetadata({ include_in_ai: aiInput.checked }))

      // 不让点击控件破坏正文选区；输入框仍保留正常的定位和选择行为。
      for (const control of [fold, up, down, moveToCursor, convert, remove, rewrite, aiInput]) {
        control.addEventListener('mousedown', (event) => event.preventDefault())
      }
      const setCollapsed = (value: boolean) => {
        collapsed = value
        contentDOM.hidden = collapsed
        fold.textContent = collapsed ? '展开' : '折叠'
        fold.setAttribute('aria-label', collapsed ? '展开区块' : '折叠区块')
        fold.setAttribute('aria-expanded', String(!collapsed))
      }
      fold.addEventListener('click', () => {
        const target = locate()
        if (!target) return
        if (!collapsed) {
          if (composing || view.composing) {
            foldNotice.textContent = '请先完成当前输入，再折叠此区块。'
            foldNotice.hidden = false
            return
          }
          const selection = view.state.selection
          const end = target.pos + target.node.nodeSize
          const overlaps = selection.empty
            ? selection.from > target.pos && selection.from < end
            : selection.from < end && selection.to > target.pos
          if (overlaps) {
            const outside = visibleTextSelection(target.pos, target.node.nodeSize)
            if (!outside) {
              foldNotice.textContent = '请先在区块外添加正文光标位置，再折叠此区块。'
              foldNotice.hidden = false
              return
            }
            // 只移动选区，不选择整个区块，避免下一次输入替换整块内容。
            view.dispatch(view.state.tr.setSelection(outside))
          }
        }
        foldNotice.hidden = true
        setCollapsed(!collapsed)
      })
      fold.setAttribute('aria-expanded', 'true')
      const reveal = (event: Event) => {
        if (event instanceof CustomEvent && event.detail?.key === initialNode.attrs.key && locate()) {
          foldNotice.hidden = true
          setCollapsed(false)
        }
      }
      view.dom.addEventListener('rich-section-reveal', reveal)

      drag.addEventListener('mousedown', (event) => {
        if (event.button !== 0) return
        const target = current()
        if (target) view.dispatch(view.state.tr.setSelection(NodeSelection.create(view.state.doc, target.pos)))
      })

      const action = (element: HTMLButtonElement, name: SectionAction) => {
        element.addEventListener('click', () => {
          if (!current() || (name === 'rewrite-selection' && !canRewrite())) return
          dom.dispatchEvent(new CustomEvent('rich-section-action', {
            bubbles: true, detail: { action: name, key: initialNode.attrs.key },
          }))
        })
      }
      action(up, 'move-up')
      action(down, 'move-down')
      action(moveToCursor, 'move-to-cursor')
      action(convert, 'convert')
      action(remove, 'delete')
      action(rewrite, 'rewrite-selection')

      refreshNode()
      refreshEditable()
      const editableObserver = new MutationObserver(refreshEditable)
      editableObserver.observe(view.dom, { attributes: true, attributeFilter: ['contenteditable', 'data-section-ai-enabled'] })

      return {
        dom,
        contentDOM,
        update(updated) {
          if (updated.type !== initialNode.type || updated.attrs.key !== initialNode.attrs.key) return false
          node = updated
          refreshNode()
          refreshEditable()
          return true
        },
        stopEvent(event) {
          if (event.type.startsWith('drag') || event.type === 'drop') return false
          if (event.type === 'mousedown' && (event.target === drag || drag.contains(event.target as globalThis.Node))) return false
          return header.contains(event.target as globalThis.Node)
        },
        ignoreMutation(mutation) {
          return header.contains(mutation.target) || (mutation.type === 'attributes' && (
            mutation.target === dom || (mutation.target === contentDOM && mutation.attributeName === 'hidden')
          ))
        },
        destroy() {
          editableObserver.disconnect()
          view.dom.removeEventListener('rich-section-reveal', reveal)
          if (composing) {
            composing = false
            dispatchComposition(false)
          }
          destroyed = true
        },
      }
    }
  },
})

export default AuthorSection
