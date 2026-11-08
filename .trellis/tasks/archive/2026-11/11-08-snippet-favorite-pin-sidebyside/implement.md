# 实施清单

## 1. 后端数据与 API
- [ ] `alembic/versions/0041_note_flags.py`：加两列和一个索引；upgrade/downgrade。
- [ ] `app/models/notes.py`：`is_favorite`、`is_pinned`。
- [ ] `app/schemas/notes.py`：Summary/Out 增加字段；新增 `NoteFlags`（strict bool、拒绝 null、至少一项）。
- [ ] `app/services/note_service.py`：`list_notes` 增加 favorite/pinned 过滤和新排序；新增 `set_note_flags`（加锁、`projects.edit`、保持 revision 与 updated_at 不变）。
- [ ] `app/api/v1/notes.py`：列表查询参数；`PUT .../flags`。
- [ ] 测试 `tests/test_notes.py`：过滤、组合搜索、排序、标记不改 revision/updated_at、标记后 PATCH 旧 revision 仍成功、空体或 null 或非 bool 时 422、只读 403、跨项目 404。`tests/test_notes_migration.py`：0041 升降级以及旧行的默认值。

回滚点：提交 1（后端 + 迁移）。

## 2. 备份 v9
- [ ] `exporter.py`：`VERSION=9`、`APP_SCHEMA="0041"`。
- [ ] `importer.py`：`_reject_future_note_flags`、`_migrate_v8_to_v9`、登记到 steps；确认布尔类型校验。
- [ ] 测试（`tests/test_project_transfer.py` / `test_backup_migrations.py`）：v9 往返保留标记；v8 导入为默认值；v8 夹带字段时拒绝；非 bool 值拒绝。全仓搜索硬编码的 `version: 8` / `v8` 断言和 fixture，同步更新。

## 3. 前端笔记页
- [ ] `api/notes.ts` 类型、查询参数、`setFlags`；更新 `api/notes.test.ts`。
- [ ] `components/notes/NoteFlagButtons.tsx`。
- [ ] `NotesPanel.tsx`：收藏筛选、列表标记按钮、已固定标签。
- [ ] `NoteEditor.tsx`：头部标记按钮；`embedded` 模式（拆出 `NoteRouteGuard`、回调替代 Navigate、Ctrl+S 阻止冒泡）。

## 4. 前端编辑页笔记栏
- [ ] `components/notes/NoteSidePanel.tsx`：固定列表、搜索打开、新建、切换确认、嵌入编辑器。
- [ ] `ChapterEditorPage.tsx`：`'notes'` 标签（桌面端与移动端）、保持挂载、`noteDirty` 并入 guard/beforeunload/blocker 及其文案。
- [ ] 单元测试：`noteDraft` / 侧栏切换确认逻辑（可提取为纯函数测试）；如有现成的编辑页测试，补充标签类型相关断言。

## 5. 规范与文档
- [ ] `.trellis/spec/backend/mention-tracking.md`（笔记合同段落）、`project-transfer.md`（v9 规则）。
- [ ] `TODOS.md` 第 9 项勾选并更新当前状态与验收记录。

## 验证命令
```bash
cd backend && uv run ruff check . && uv run pytest tests/test_notes.py tests/test_notes_migration.py tests/test_project_transfer.py tests/test_backup_migrations.py -q
cd backend && uv run pytest -q   # 完整回归
cd frontend && npm run lint && npm test && npm run build
```

## 浏览器验收（隔离数据）
- 笔记页：收藏筛选、固定排序、标记不打断编辑中的草稿（AC1–AC3）。
- 编辑页：笔记栏编辑与保存、Ctrl/⌘+S 隔离、切标签保留草稿、切换笔记确认、离开拦截、移动端抽屉（AC4–AC5）。
- 只读账号（AC6）；备份导出与导入（AC7）。

## 风险文件
- `ChapterEditorPage.tsx`（1312 行，已有 blocker 与快捷键逻辑）：改动最小化，只加标签、guard 条件和挂载方式。
- `importer.py` 版本链：遗漏 step 会导致所有旧备份导入失败；以 v1→v9 的全链测试兜底。
