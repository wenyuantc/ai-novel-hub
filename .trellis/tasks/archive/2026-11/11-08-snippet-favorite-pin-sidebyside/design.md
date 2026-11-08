# 设计：笔记收藏、固定与并排查看

## 1. 数据层

迁移 `0041_note_flags`：

```
project_notes.is_favorite  BOOLEAN NOT NULL DEFAULT false
project_notes.is_pinned    BOOLEAN NOT NULL DEFAULT false
INDEX ix_project_notes_project_pinned (project_id, is_pinned, updated_at)
```

- 旧数据由 server_default 回填为 false，不改动 revision 和 updated_at。
- downgrade 删除索引与两列。
- 模型 `ProjectNote` 增加两个 `Mapped[bool]`（`default=False, server_default=sa.false()`）。

## 2. API 合同

| 接口 | 变化 |
|---|---|
| `GET /projects/{pid}/notes` | 新增查询参数 `favorite: bool | None`、`pinned: bool | None`（为 true 时过滤；false/缺省不过滤）。排序改为 `is_pinned desc, updated_at desc, id desc`。 |
| `NoteSummary` / `NoteOut` | 新增 `is_favorite: bool`、`is_pinned: bool`。 |
| `PUT /projects/{pid}/notes/{id}/flags`（新增） | 请求体 `NoteFlags {is_favorite?: bool, is_pinned?: bool}`，`extra=forbid`、strict bool、拒绝 null，且至少提供一项（否则 422）。需要 `projects.edit`，并对项目加锁，与其他写入一致。返回 `NoteOut`。 |
| `PATCH /notes/{id}` | 不变；`NoteUpdate` 仍 `extra=forbid`，不接受标记字段，避免两条写路径互相耦合。 |

标记写入语义（对应 R3）：
- 使用 core `update(...).values(is_favorite=..., updated_at=ProjectNote.updated_at)`，显式把 `updated_at` 写回原值，以覆盖 `onupdate=func.now()`，且**不**递增 revision。
- 标记操作为幂等的「设置」，不做 CAS：两端同时切换时以后写者为准。标记不涉及正文，丢失一次切换的代价很小。
- 笔记不存在时返回 404。

## 3. 备份（v9）

- `exporter.VERSION = 9`，`APP_SCHEMA = "0041"`。通用 `TableRule` 会自动导出新列，无需修改注册表。
- `importer.migrate`：
  - 新增 `_reject_future_note_flags(data)`，当 `version < 9` 时调用：`project_notes` 行中出现 `is_favorite` 或 `is_pinned` 键 → `BadRequestError("旧版备份不支持笔记收藏与固定，请使用原始版本文件")`。
  - 新增 `_migrate_v8_to_v9`：为每行 `project_notes` 补 `is_favorite=False, is_pinned=False`。
- 类型校验：确认导入器对 Boolean 列的类型校验能拒绝非 bool 值（例如字符串 `"true"`）；如果通用校验不覆盖，就在 `importer.py:606` 附近 `project_notes` 的专用校验中补上。
- 设定卡片版本（v6）不受影响。
- 同步 `.trellis/spec/backend/project-transfer.md` 和 `mention-tracking.md` 中的笔记合同。

## 4. 前端

### 4.1 API 与共享组件
- `api/notes.ts`：类型增加 `is_favorite/is_pinned`；`NoteQuery` 增加 `favorite?/pinned?`；新增 `noteApi.setFlags(pid, id, flags)`。
- 新增 `components/notes/NoteFlagButtons.tsx`：星标和图钉两个图标按钮（带 aria-label 与 tooltip），只读时只展示状态，不可点击。切换成功后调用 `invalidateProject(pid, ['notes'])`，失败时提示错误并回退。

### 4.2 NotesPanel
- 搜索框旁增加 `Segmented`「全部 / 收藏」，切换时回到第 1 页。
- 列表项标题旁显示 `NoteFlagButtons`；固定的笔记带「已固定」标签。
- 空状态文案区分「没有收藏的笔记」和搜索无结果。

### 4.3 NoteEditor 嵌入模式
为 `NoteEditor` 增加可选属性 `embedded?: { onDirtyChange(dirty): void; onSaved(note): void; onDeleted(): void }`：
- 嵌入模式下**不**调用 `useBlocker`（react-router 同时只支持一个 blocker），也不监听 beforeunload；改为通过 `onDirtyChange` 上报未保存状态，由编辑页统一拦截。实现方式：把拦截逻辑拆成一个子组件（例如 `NoteRouteGuard`），仅在非嵌入模式下渲染，从而不需要条件调用 hook。
- 新建或删除后不写 URL（不渲染 `Navigate`），改为回调 `onSaved` / `onDeleted`。
- 编辑器根节点的 Ctrl/⌘+S 处理器在嵌入模式下调用 `event.stopPropagation()`，防止冒泡到编辑页 window 级快捷键而触发正文保存（React 17+ 的合成事件 stopPropagation 会阻止原生事件冒泡到 window）。
- 编辑器头部显示 `NoteFlagButtons`（已保存的笔记才显示），抽屉模式与嵌入模式都显示。
- 嵌入模式下正文输入框 `autoSize` 的上限调小，以适应 360px 侧栏。

### 4.4 章节编辑页「笔记」标签：`components/notes/NoteSidePanel.tsx`
- 数据：`noteApi.list(pid, { pinned: true, page_size: 100 })`，订阅 `resources: ['notes']` 的刷新；固定笔记超过 100 篇时提示「仅显示前 100 篇」。
- 布局：固定笔记列表（紧凑列表，点击选中）→「打开其他笔记」`Select`（远程搜索 `noteApi.list({q})`）→「新建」按钮 → 选中笔记的嵌入式 `NoteEditor`（通过 `NoteDetail` 风格的加载器按 id 拉取全文，以 `key=id` 重建）。
- 默认不选中笔记，显示提示；选中状态只保存在组件内存中。
- 切换笔记时如果当前有未保存修改，弹出 `Modal.confirm`「放弃修改并切换」。
- 只读用户：隐藏「新建」，编辑器为只读视图。

### 4.5 ChapterEditorPage 集成
- `inspectorTab` 类型增加 `'notes'`；`inspectorTabItems` 增加「笔记」项；移动端 Segmented 与抽屉标题同步。
- **保持挂载**：`NoteSidePanel` 不按标签条件渲染，而是始终渲染，非当前标签时用 `display: none` 包裹隐藏，因此切换标签不会丢草稿。
  - 桌面端：侧栏收起（`inspectorOpen=false`）时，也把侧栏容器改为 `display:none` 而不是卸载。检查这样做是否影响其他标签的现有行为；如果有影响，只让笔记标签的内容保持挂载。
  - 移动端：抽屉已经 `forceRender`，同样用 display 方式保持笔记内容挂载。
  - 桌面端与移动端不会同时渲染两份笔记栏（两者按 `isMobile` 互斥），避免两个编辑器各持一份草稿。
- 未保存拦截：新增 `noteDirty` 状态，由 `onDirtyChange` 更新；页面的 `guard` 与 beforeunload 条件加入 `noteDirty`，blocker 判断「离开页面」时同样生效；对话框文案按来源区分（正文 / 笔记）。注意：仅切换 `scene_id` 不应因笔记草稿被拦截，因为切换场景不会卸载笔记栏。
- `canUseAI=false` 时的默认标签回退逻辑不变。

## 5. 兼容与回滚

- 迁移只做加列和加索引，可安全 downgrade；旧客户端忽略新增的响应字段。
- v9 备份无法被旧版本导入（现有规则会以「来自更新的版本」拒绝），这是预期行为。
- 回滚顺序：先回退前端，再回退 API，最后 downgrade 迁移。

## 6. 权衡

- 标记不进入 revision：避免切换星标或图钉打断正在编辑的草稿；代价是标记并发切换为后写者胜出。
- 笔记栏中选中的笔记不写入 URL：避免与编辑页现有的 `scene_id`、`panel` 等 URL 参数和 blocker 规则冲突；代价是刷新页面后需要重新选择。
- 固定不设数量上限、不支持手动排序：保持首版简单，侧栏最多展示 100 篇。
