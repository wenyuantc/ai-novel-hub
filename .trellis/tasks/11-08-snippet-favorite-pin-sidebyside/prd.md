# 笔记收藏、固定与并排查看

## Goal

补齐 TODOS 第 9 项剩余待办：作者可以收藏和固定作品笔记，并在章节写作时于同一页面查看、编辑笔记，不必在笔记页与编辑页之间来回切换。

参考：[Novelcrafter Snippets](https://www.novelcrafter.com/help/docs/snippets/snippets)，snippet 可「starred for easy access」，也可「pinned to the sidebar for use with other interfaces」。

## Background（现状）

- `ProjectNote`（`backend/app/models/notes.py`）只有 `project_id/title/content/revision` 和时间戳；最新迁移 `0040`。
- 写操作通过 `expected_revision` CAS（`backend/app/services/note_service.py`），冲突返回 409 `stale_revision`；`TimestampMixin.updated_at` 设置了 `onupdate=func.now()`（`backend/app/models/base.py:21`）。
- 列表按 `updated_at desc, id desc` 排序，支持分页和搜索，只返回 180 字摘要。读取权限为 `get_manageable_project`，写入需要 `projects.edit`。
- 备份通过通用 `TableRule("project_notes")` 导出（`project_transfer/registry.py:191`），当前版本 `exporter.py:33 VERSION = 8`。
- 前端 `NotesPanel.tsx` 由列表和 URL 驱动的编辑抽屉组成；`NoteEditor.tsx` 显式保存，自带 `useBlocker`、beforeunload 和 Ctrl/⌘+S，冲突时保留草稿。
- `ChapterEditorPage.tsx` 桌面端为右侧 360px 检视面板（AI / 场景规划 / 章节设置，标签页条件渲染），移动端为底部抽屉；页面有自己的 `useBlocker`（`:385`）和 window 级 Ctrl+S。

## Key Decisions（用户已确认）

- D1 收藏与固定是两个独立标记，与 Novelcrafter 一致。
  - 收藏：笔记带星标；笔记列表可筛选「只看收藏」。
  - 固定：固定的笔记在笔记列表中排在最前，并出现在章节编辑页笔记栏的快速切换列表中。
- D2 笔记栏中的笔记**可编辑**，复用笔记编辑器，独立保存，并有冲突提示与未保存拦截。
- D3 收藏和固定属于作品数据（与笔记本身一样对能访问作品的人可见），不按用户区分。

## Requirements

- R1 收藏：有编辑权限的用户可在笔记列表和笔记编辑器中切换收藏；列表提供「全部 / 收藏」筛选，可与搜索组合使用。
- R2 固定：有编辑权限的用户可在笔记列表、笔记编辑器和笔记栏中切换固定；列表排序为固定优先，其次按更新时间倒序。
- R3 切换收藏或固定不改变笔记修订号和更新时间，不会让正在编辑同一笔记的草稿产生修订冲突。
- R4 章节编辑页的检视面板新增「笔记」标签（桌面端侧栏和移动端抽屉均有）：
  - 顶部为固定笔记的快速切换列表；另可搜索打开任意笔记，或新建笔记。
  - 下方嵌入笔记编辑器，可编辑并独立保存（按钮或在笔记区内按 Ctrl/⌘+S），且不会触发正文保存。
  - 笔记有未保存修改时：切换到另一篇笔记需确认；离开编辑页时被拦截；切换检视面板标签或收起侧栏不丢草稿。
  - 只读用户可查看笔记，不能编辑或切换标记。
- R5 备份升级到 v9，导出收藏和固定状态；v1–v8 备份导入时两者默认为未收藏、未固定；旧版本备份中夹带这两个字段时拒绝导入（沿用现有「旧格式夹带未来字段拒绝」规则）。

## Acceptance Criteria

- AC1 收藏一篇笔记后，在笔记列表选「收藏」只显示收藏的笔记；与搜索词组合时结果同时满足两者。（R1）
- AC2 固定一篇较早更新的笔记后，它在笔记列表第一页排在未固定笔记之前；取消固定后恢复按更新时间排序。（R2）
- AC3 在抽屉中打开笔记 A 并修改正文但不保存，另一处切换 A 的固定状态后，保存 A 成功，不出现修订冲突；A 的修订号和更新时间不因切换标记而变化。（R3）
- AC4 在章节编辑页打开「笔记」标签，能看到所有固定笔记；点击后编辑并保存成功，正文未保存标记不受影响；在笔记区按 Ctrl/⌘+S 只保存笔记。（R4）
- AC5 笔记栏有未保存修改时：切换到其他检视标签再切回，草稿仍在；切换到另一篇笔记会提示确认；导航到其他页面会被拦截。（R4）
- AC6 只读账号在笔记栏和笔记列表中看不到编辑或标记操作，后端拒绝其标记请求（403）。（R1、R2、R4）
- AC7 导出 v9 备份再导入后，收藏和固定状态保留；导入 v8 备份成功，所有笔记均为未收藏、未固定；v8 备份夹带 `is_pinned` 时预览即拒绝。（R5）

## Out of Scope

- 按用户区分的收藏或固定、固定的手动排序、固定数量上限。
- 笔记富文本、笔记进入 AI 上下文、跨作品或系列共享笔记、笔记文件夹或标签。
- 在章节编辑页以外的页面（如规划页、阅读器）提供笔记栏。
- 笔记自动保存（属于第 15 项）。
