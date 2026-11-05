# 作品笔记实施记录

## 已实现

- `backend/app/models/notes.py`：独立 `ProjectNote`，作品外键级联、稳定 SQLite AUTOINCREMENT 身份、正修订号约束。
- `backend/app/schemas/notes.py`、`services/note_service.py`、`api/v1/notes.py`：分页/字面搜索/详情/创建/修改/删除；标题规范化，正文按原样保留；PATCH/DELETE 原子 expected_revision CAS，409 返回 stale_revision；人工作品权限及生命周期锁。
- `backend/alembic/versions/0032_project_notes.py`：0031 → 0032；表仍有笔记时拒绝有损降级。
- `backend/app/models/__init__.py`、`api/v1/__init__.py`、`services/project_transfer/registry.py`、`exporter.py`：模型/API/备份注册，APP_SCHEMA 暂升0032，随后交提及后端代理推进0033。
- `frontend/src/api/notes.ts`：镜像类型和 API，不自动重试写入。
- `frontend/src/pages/project/NotesPanel.tsx`、`components/notes/NoteEditor.tsx`：列表/搜索/分页、独立笔记抽屉、编辑/原文切换、?id= 与 ?new=1、保存状态、删除确认、409保留输入、加载最新版本确认、离页和跨笔记草稿保护；按身份/作品隔离异步状态。
- `components/notes/noteDraft.ts`：严格深链ID、码点长度、草稿比较及仅清理本域查询参数。

## 验证证据

1. 先运行 `tests/test_notes.py::test_note_crud_search_pagination_and_revision`，原接口404，确认失败原因是功能缺失。
2. `UV_CACHE_DIR=/tmp/ainovel-notes-uv-cache uv run pytest tests/test_notes.py -q`：最初3项通过（CRUD/修订、校验/隔离、备份新旧格式往返）。
3. 补充并发、管理权限和迁移测试后，`LC_ALL=en_US.UTF-8 UV_CACHE_DIR=/tmp/ainovel-notes-uv-cache uv run pytest tests/test_notes.py tests/test_notes_migration.py tests/test_project_transfer.py -q`：30项通过、2项失败。笔记6项全部通过；两个失败为并行提及后端尚未完成的 `world_settings.aliases` JSON备份登记和 APP_SCHEMA 过渡值，已交给该代理完成后统一复跑，未降低断言。
4. 笔记专属 ruff：`LC_ALL=en_US.UTF-8 UV_CACHE_DIR=/tmp/ainovel-notes-uv-cache uv run ruff check app/models/notes.py app/schemas/notes.py app/services/note_service.py app/api/v1/notes.py tests/test_notes.py tests/test_notes_migration.py` 全部通过。
5. `npm test -- src/api/notes.test.ts src/components/notes/noteDraft.test.ts`：2文件、5项全部通过。
6. `npm run lint`：本轮全仓通过。`npm run build`：笔记文件无类型报错，因并行统计页 `./pages/project/MentionsPanel` 当时尚未落盘而失败，待集成完成统一执行。
7. 主会话已通过真实浏览器验证：列表 / ?id=1 打开、修改正文、关闭触发未保存确认、继续编辑保留草稿、保存成功状态。主会话继续负责新建、冲突、提及深链与最终端到端验收。

环境说明：macOS 当前默认 `C.UTF-8` 不可用，使 Alembic 配置中的中文按 ASCII 解码失败；迁移验证使用 `LC_ALL=en_US.UTF-8`。默认 uv 缓存目录受沙箱限制，故指定任务临时缓存。测试修正了 SQLite 的 PRAGMA foreign_keys 启用时机，未修改产品约束来消除失败。

## 明确交接

- 共享后端注册文件已移交 `mention_backend_implement`；后续 APP_SCHEMA、世界观别名 JSON、备份预览中文表名等由其统一整合。
- `NotesPanel.tsx`、`NoteEditor.tsx` 已移交 `mention_frontend_implement` 接入精确提及定位/高亮；本代理不会继续并发编辑。
- `frontend/src/utils/projectRefresh.ts` 的 notes/chat 资源枚举、笔记路由与菜单由提及前端代理负责；本模块保存/删除已广播 notes 失效。
- 本子任务未提交、未归档；最终全范围质量检查、TODOS/规范更新和收尾由主会话完成。
