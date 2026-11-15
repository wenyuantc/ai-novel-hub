# 阶段 A：能力入口和既有基线（2026-11-09）

## 扫描方法与界限

在后端应用工厂构造的实际 FastAPI app 上递归展开每个 `_IncludedRouter.original_router`，累加 `include_context.prefix`，枚举 APIRoute 的方法和路径，再与 `app.openapi()["paths"]` 逐个核对。OpenAPI：346 个路径、469 个操作；递归展开发现 472 个实际操作，其中 3 个不进入主 OpenAPI；469 个文档化操作均被递归扫描捕获。直接平铺 `app.routes` 只有 4 个 APIRoute，不能作为全量检查。`tests/test_collaboration_route_inventory.py` 同时验证数量下限、两者包含关系以及项目/回收/备份/阅读进度/章节无项目 ID/任务流/知识检索/Agent/系列等实际路径，新增端点仍可扩充扫描。

下表覆盖全部 469 个文档化方法+路径，并给出所在 API 模块及路由函数，便于逐条追踪。分类基于模块和具体操作名的第一轮分拣：人工读、人工写及预览标注“候选”，并非已经逐个核验 service 与事务链的最终授权结论；管理员、系列、个人任务、导出、检索和 AI 等按专门规则标注。所有候选及“需复核”须在实施时按真实调用链确认后迁移，特别是 `POST` 预览、卡片/大纲导出、书本上下文中的个人会话和检索向量出站，不能仅凭 HTTP 方法授权。此清单本身不是服务端鉴权或通过权限矩阵的证据。

分类计数：
- AI：19
- AI + 人工写：3
- AI 上下文/检索出站：1
- AI 提示词/私有风格，需复核：1
- AI/向量索引：2
- AI（含任务预览）：3
- 个人会话/导入，项目访问需复核：24
- 人工写 + 按授权创建索引任务：1
- 人工写（候选）：120
- 人工读（候选）：89
- 作品回收/既有管理员规则：1
- 作品导出：1
- 作者备份：2
- 作者阅读进度：1
- 卡片读/导出，能力需复核：2
- 大纲读/导出，能力需复核：2
- 本人书架/创建：2
- 本人任务/当前协作；活动 AI 需授权：12
- 本人会话/AI/提案权限，动作需复核：13
- 本人生成日志：2
- 检索/向量出站，能力需复核：2
- 管理员专用：7
- 系列+受影响作品双重权限：5
- 系列作者专用：5
- 系列编辑（删除需校验绑定）：3
- 系列读：2
- 系列读（书籍投影过滤）：2
- 账户/管理/开放 API，协作范围外：137
- 账户级备份导入：2
- 预览读（候选，需复核副作用）：3

## 代表性调用链及边界

- `GET/PATCH /projects/{project_id}` → `project_service.get_manageable_project` / `update_project`；人工管理员按 `projects.read/edit` 和目标保护放行，`get_owned_project` 仍是严格作者 helper。`DELETE /projects/{project_id}` 经项目回收服务，不能把 editor 等同 owner。列表当前只查作者自己的书。
- 章节常规 CRUD 与 Reader TOC 走 `get_manageable_project/get_manageable_chapter`；`GET /chapters/{chapter_id}/context-preview`、`POST /chapters/{chapter_id}/generate|polish|analyze|revise|rewrite` 仅有章节 ID，必须先定位父项目；AI 路径当前多用 `get_owned_chapter/get_owned_project`。
- `/projects/{project_id}/chapters/{chapter_id}/scenes/...` 人工场景和富文档与 `scene_ai`、同步改写/采纳是不同动作，后者需要 AI 权限并在写时锁内重验。
- `GET /projects/{project_id}/export` → `export_service.export_project` 严格作者；`GET /projects/{project_id}/backup` → `project_transfer.exporter`、`PUT .../reading-progress` → `reader_service.save_progress`，仍是作者专用；管理员人工读写不能放宽备份或进度。
- `GET /tasks/{task_id}/output|stream`、取消/续跑/审核和 Agent 对话/提案依任务或会话 ID 和真实 actor，不允许管理员人工旁路取他人私人输出；后台裁剪也不能要求已撤销用户继续具备作品权限。
- `POST .../kb/search`、`POST .../memories/search` 和 `GET /chapters/{chapter_id}/context-preview` 可能使用向量配置，调用 `kb_service/memory_service` 与检索链；要同时校验项目身份、向量费用与出站策略。记忆人工写后 `_enqueue_vectors` 现通过 `kb_index_service.enqueue_memory(..., project_id)` 派生任务，须传真实触发者而不能隐式归作者。
- `POST .../cards/export`、大纲导出和导入预览并不等同 TXT/Markdown/EPUB；其协作者能力边界需对照设计与现有私有数据逐条确认。
- `/series/{sid}/entries/...` 的作者/设定协作者与单书协作者独立；`promote/use/unuse/move/impact` 还需全部受影响书权限并过滤不可见书信息，条目删除有绑定安全限制。

### 后台任务类型

实际注册包括 `chapter.generate/polish/revise/analyze/batch`、`wizard.outline/characters/world/careers`、`character.generate`、`organization.generate`、`career.generate`、`relations.parse/generate`、`outline.continue/expand`、`agent.turn`、`kb.index`、`book.import`。创建、运行、重试、待审核续跑、出站和落库均须重验；模型/向量发送及每批写入需额外验证。同步改写和封面生成也必须覆盖，不可只扫描任务注册表。

### 目标角色矩阵

| 动作 | 作者 | 已接受 viewer | 已接受 editor | 管理员身份单独存在时 |
| --- | --- | --- | --- | --- |
| 人工内容读 | 是 | 是 | 是 | 原 `projects.read` |
| 人工内容写 | 是 | 否 | 是 | 原 `projects.edit` 与目标保护 |
| TXT/MD/EPUB 导出 | 是 | 是 | 是 | 不因此放行 |
| AI / 当前作品的本人任务 | 是 | 否（本人已完成记录另按设计） | 是 | 不因此放行 |
| 备份 / 进度 / 成员管理 | 是 | 否 | 否 | 不因此放行 |
| 回收 | 是 | 否 | 否 | 原 `projects.recycle` |

待接受、拒绝、撤销和非成员不构成协作授权；管理员与协作者重叠时各能力取各自来源的并集。系列编辑权限与作品权限分开计算。

## 现有回归证据

- `cd backend && uv run pytest -q tests/test_projects_and_story.py::test_project_crud tests/test_projects_and_story.py::test_cross_user_isolation tests/test_admin_projects.py tests/test_recycle_bin.py::test_complete_project_is_hidden_restored_in_place_then_purged tests/test_recycle_bin.py::test_permissions_record_generation_and_exact_confirmation tests/test_project_transfer.py::test_registry_covers_all_project_tables tests/test_project_transfer.py::test_cross_user tests/test_series.py tests/test_series_projection_guards.py`：29 passed in 27.30s（SQLite，既有行为）。
- `cd backend && AINOVEL_SERIES_POSTGRES_TEST_URL=<隔离测试库 URL> uv run pytest -q tests/test_series_postgresql.py`：1 passed in 7.47s（本机隔离 PostgreSQL 16.15；测试创建独立 schema）。这仅验证既有系列引用竞争，不代表新增协作并发已验证。
- `cd backend && uv run pytest -q tests/test_collaboration_route_inventory.py`：1 passed in 5.58s；`uv run ruff check tests/test_collaboration_route_inventory.py`：All checks passed。此阶段不代表完整后端/前端门禁。
- 前端初始 `node_modules` 缺少 lockfile 已声明的 Tiptap 依赖，首次 `npm run test` 有 12 个文件因依赖缺失无法加载；`npm ci` 恢复后，`npm run test` 为 178 个文件、1222 项通过，`npm run build` 通过。恢复依赖前 `npm run lint` 已通过。

## 逐操作能力清单

| 方法 | 路径 | API 模块 / 函数 | 初步分类 |
| --- | --- | --- | --- |
| GET | `/api/open/v1/me` | `open.key_info` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels` | `open.list_novels` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}` | `open.get_novel` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/chapters` | `open.list_chapters` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/chapters/{chapter_id}` | `open.get_chapter` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/characters` | `open.list_characters` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/characters/{character_id}` | `open.get_character` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/cover` | `open.get_cover` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/export` | `open.export_novel` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/organizations` | `open.list_organizations` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/organizations/{organization_id}` | `open.get_organization` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/organizations/{organization_id}/members` | `open.list_members` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/outlines` | `open.list_outlines` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/outlines/{outline_id}` | `open.get_outline` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/outlines/{outline_id}/plans` | `open.list_plans` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/relationship-types` | `open.list_relationship_types` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/relationships` | `open.list_relationships` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/relationships/{relationship_id}` | `open.get_relationship` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/world` | `open.list_world` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/open/v1/novels/{novel_id}/world/{setting_id}` | `open.get_world` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/audit-logs` | `admin_audit.list_logs` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/admin/audit-logs/cleanup` | `admin_audit.cleanup` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/admin/audit-logs/cleanup-preview` | `admin_audit.preview_cleanup` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/audit-logs/{log_id}` | `admin_audit.get_log` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/backups` | `admin_backups.overview` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/backups/runs` | `admin_backups.runs` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/admin/backups/runs` | `admin_backups.run_manual` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/backups/runs/{run_id}` | `admin_backups.run_detail` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/backups/runs/{run_id}/download` | `admin_backups.download` | 账户/管理/开放 API，协作范围外 |
| HEAD | `/api/v1/admin/backups/runs/{run_id}/download` | `admin_backups.download` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/backups/settings` | `admin_backups.settings` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/admin/backups/settings` | `admin_backups.update_settings` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/dashboard/history` | `admin_dashboard.history` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/dashboard/overview` | `admin_dashboard.overview` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/dashboard/tasks` | `admin_dashboard.tasks` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/permissions` | `admin_roles.permissions` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/projects` | `admin.list_projects` | 管理员专用 |
| GET | `/api/v1/admin/recycle-bin/projects` | `admin_recycle_bin.list_projects` | 管理员专用 |
| POST | `/api/v1/admin/recycle-bin/projects/{project_id}/purge` | `admin_recycle_bin.purge` | 管理员专用 |
| POST | `/api/v1/admin/recycle-bin/projects/{project_id}/restore` | `admin_recycle_bin.restore` | 管理员专用 |
| GET | `/api/v1/admin/roles` | `admin_roles.roles` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/admin/roles` | `admin_roles.create` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/admin/roles/{role_id}` | `admin_roles.delete` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/roles/{role_id}` | `admin_roles.role` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/admin/roles/{role_id}` | `admin_roles.update` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/system-settings` | `admin_system_settings.read_settings` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/admin/system-settings` | `admin_system_settings.update_settings` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/tasks` | `admin_tasks.list_tasks` | 管理员专用 |
| GET | `/api/v1/admin/tasks/{task_key}` | `admin_tasks.get_task` | 管理员专用 |
| POST | `/api/v1/admin/tasks/{task_key}/cancel` | `admin_tasks.cancel_task` | 管理员专用 |
| GET | `/api/v1/admin/users` | `admin.list_users` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/admin/users` | `admin.create_user` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/admin/users/{user_id}` | `admin.delete_user` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/users/{user_id}` | `admin.get_user_detail` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/admin/users/{user_id}` | `admin.update_user` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/admin/users/{user_id}/quotas` | `admin_quotas.read` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/admin/users/{user_id}/quotas` | `admin_quotas.update` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/admin/users/{user_id}/reset-password` | `admin.reset_password` | 账户/管理/开放 API，协作范围外 |
| PUT | `/api/v1/admin/users/{user_id}/roles` | `admin_roles.assign` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/ai-configs` | `ai_configs.list_configs` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/ai-configs` | `ai_configs.create_config` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/ai-configs/models` | `ai_configs.list_models` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/ai-configs/{config_id}` | `ai_configs.delete_config` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/ai-configs/{config_id}` | `ai_configs.get_config` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/ai-configs/{config_id}` | `ai_configs.update_config` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/ai-configs/{config_id}/default` | `ai_configs.set_default` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/ai-configs/{config_id}/test` | `ai_configs.test_config` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/analyses/{analysis_id}` | `analysis.get_analysis` | 人工读（候选） |
| POST | `/api/v1/analysis-changes/{change_id}/confirm` | `analysis.confirm_change` | 人工写（候选） |
| POST | `/api/v1/analysis-changes/{change_id}/reject` | `analysis.reject_change` | 人工写（候选） |
| POST | `/api/v1/analysis-changes/{change_id}/revert` | `analysis.revert_change` | 人工写（候选） |
| GET | `/api/v1/api-keys` | `api_keys.list_keys` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/api-keys` | `api_keys.create_key` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/api-keys/{key_id}` | `api_keys.update_key` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/api-keys/{key_id}/reveal` | `api_keys.reveal_key` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/api-keys/{key_id}/revoke` | `api_keys.revoke_key` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/auth/change-password` | `auth.change_password` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/auth/config` | `auth.auth_config` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/auth/login` | `auth.login` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/auth/logout` | `auth.logout` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/auth/me` | `auth.me` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/auth/register` | `auth.register` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/book-imports` | `book_imports.list_imports` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/book-imports` | `book_imports.upload` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/book-imports/tasks/{task_id}/skip` | `book_imports.skip_task` | 个人会话/导入，项目访问需复核 |
| DELETE | `/api/v1/book-imports/{import_id}` | `book_imports.delete_import` | 个人会话/导入，项目访问需复核 |
| GET | `/api/v1/book-imports/{import_id}` | `book_imports.get_import` | 个人会话/导入，项目访问需复核 |
| GET | `/api/v1/book-imports/{import_id}/chapters/{index}` | `book_imports.chapter_text` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/book-imports/{import_id}/estimate` | `book_imports.estimate` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/book-imports/{import_id}/ops` | `book_imports.apply_op` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/book-imports/{import_id}/resplit` | `book_imports.resplit` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/book-imports/{import_id}/start` | `book_imports.start` | 个人会话/导入，项目访问需复核 |
| GET | `/api/v1/chapters/{chapter_id}/analyses` | `analysis.list_analyses` | 人工读（候选） |
| GET | `/api/v1/chapters/{chapter_id}/analysis-status` | `analysis.chapter_analysis_status` | 人工读（候选） |
| POST | `/api/v1/chapters/{chapter_id}/analyze` | `analysis.analyze_chapter` | AI |
| GET | `/api/v1/chapters/{chapter_id}/context-preview` | `memories.context_preview` | AI 上下文/检索出站 |
| POST | `/api/v1/chapters/{chapter_id}/generate` | `chapters.generate_chapter` | AI |
| POST | `/api/v1/chapters/{chapter_id}/polish` | `chapters.polish_chapter` | AI |
| POST | `/api/v1/chapters/{chapter_id}/revise` | `analysis.revise_chapter` | AI |
| POST | `/api/v1/chapters/{chapter_id}/rewrite` | `chapter_versions.rewrite_preview` | AI |
| GET | `/api/v1/inspiration/scale-suggest` | `inspiration.scale_suggest` | 个人会话/导入，项目访问需复核 |
| GET | `/api/v1/inspiration/sessions` | `inspiration.list_sessions` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/inspiration/sessions` | `inspiration.create_session` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/inspiration/sessions/from-project/{project_id}` | `inspiration.from_project` | 个人会话/导入，项目访问需复核 |
| DELETE | `/api/v1/inspiration/sessions/{session_id}` | `inspiration.delete_session` | 个人会话/导入，项目访问需复核 |
| GET | `/api/v1/inspiration/sessions/{session_id}` | `inspiration.get_session` | 个人会话/导入，项目访问需复核 |
| PATCH | `/api/v1/inspiration/sessions/{session_id}` | `inspiration.update_session` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/inspiration/sessions/{session_id}/apply` | `inspiration.apply` | 个人会话/导入，项目访问需复核 |
| GET | `/api/v1/inspiration/sessions/{session_id}/apply-preview` | `inspiration.apply_preview` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/inspiration/sessions/{session_id}/convert` | `inspiration.convert` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/inspiration/sessions/{session_id}/notes` | `inspiration.add_note` | 个人会话/导入，项目访问需复核 |
| DELETE | `/api/v1/inspiration/sessions/{session_id}/notes/{index}` | `inspiration.delete_note` | 个人会话/导入，项目访问需复核 |
| PATCH | `/api/v1/inspiration/sessions/{session_id}/steps/{step}` | `inspiration.update_step` | 个人会话/导入，项目访问需复核 |
| POST | `/api/v1/inspiration/sessions/{session_id}/steps/{step}/generate` | `inspiration.generate_options` | 个人会话/导入，项目访问需复核 |
| GET | `/api/v1/mcp-plugins` | `mcp_plugins.list_plugins` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/mcp-plugins` | `mcp_plugins.create_plugin` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/mcp-plugins/import` | `mcp_plugins.import_plugins` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/mcp-plugins/{plugin_id}` | `mcp_plugins.delete_plugin` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/mcp-plugins/{plugin_id}` | `mcp_plugins.get_plugin` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/mcp-plugins/{plugin_id}` | `mcp_plugins.update_plugin` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/mcp-plugins/{plugin_id}/call` | `mcp_plugins.call_tool` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/mcp-plugins/{plugin_id}/refresh` | `mcp_plugins.refresh_plugin` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/mcp-plugins/{plugin_id}/tools` | `mcp_plugins.update_tool` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/projects` | `projects.list_projects` | 本人书架/创建 |
| POST | `/api/v1/projects` | `projects.create_project` | 本人书架/创建 |
| POST | `/api/v1/projects/import` | `transfer.import_project` | 账户级备份导入 |
| POST | `/api/v1/projects/import/preview` | `transfer.preview_import` | 账户级备份导入 |
| DELETE | `/api/v1/projects/{project_id}` | `projects.delete_project` | 作品回收/既有管理员规则 |
| GET | `/api/v1/projects/{project_id}` | `projects.get_project` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}` | `projects.update_project` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/agent/changes/{change_id}/confirm` | `agent.confirm_change` | 本人会话/AI/提案权限，动作需复核 |
| POST | `/api/v1/projects/{project_id}/agent/changes/{change_id}/reject` | `agent.reject_change` | 本人会话/AI/提案权限，动作需复核 |
| POST | `/api/v1/projects/{project_id}/agent/changes/{change_id}/undo` | `agent.undo_change` | 本人会话/AI/提案权限，动作需复核 |
| GET | `/api/v1/projects/{project_id}/agent/conversations` | `agent.list_conversations` | 本人会话/AI/提案权限，动作需复核 |
| POST | `/api/v1/projects/{project_id}/agent/conversations` | `agent.create_conversation` | 本人会话/AI/提案权限，动作需复核 |
| DELETE | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}` | `agent.delete_conversation` | 本人会话/AI/提案权限，动作需复核 |
| GET | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}` | `agent.get_conversation` | 本人会话/AI/提案权限，动作需复核 |
| PATCH | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}` | `agent.update_conversation` | 本人会话/AI/提案权限，动作需复核 |
| GET | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}/generation-logs` | `agent.list_generation_logs` | 本人会话/AI/提案权限，动作需复核 |
| POST | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}/messages` | `agent.send_message` | 本人会话/AI/提案权限，动作需复核 |
| POST | `/api/v1/projects/{project_id}/agent/turns/{task_id}/confirm-all` | `agent.confirm_turn` | 本人会话/AI/提案权限，动作需复核 |
| POST | `/api/v1/projects/{project_id}/agent/turns/{task_id}/reject-all` | `agent.reject_turn` | 本人会话/AI/提案权限，动作需复核 |
| POST | `/api/v1/projects/{project_id}/agent/turns/{task_id}/undo` | `agent.undo_turn` | 本人会话/AI/提案权限，动作需复核 |
| POST | `/api/v1/projects/{project_id}/analysis/analyze-pending` | `analysis.analyze_pending` | AI |
| GET | `/api/v1/projects/{project_id}/analysis/statuses` | `analysis.analysis_statuses` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/backup` | `transfer.export_backup` | 作者备份 |
| GET | `/api/v1/projects/{project_id}/backup/estimate` | `transfer.estimate_backup` | 作者备份 |
| POST | `/api/v1/projects/{project_id}/batch-generate` | `batch.start_batch` | AI（含任务预览） |
| POST | `/api/v1/projects/{project_id}/batch-generate/preview` | `batch.preview_batch` | AI（含任务预览） |
| POST | `/api/v1/projects/{project_id}/cards/export` | `cards.export_cards` | 卡片读/导出，能力需复核 |
| POST | `/api/v1/projects/{project_id}/cards/import` | `cards.import_cards` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/cards/import/preview` | `cards.preview_import` | 卡片读/导出，能力需复核 |
| GET | `/api/v1/projects/{project_id}/careers` | `careers.list_careers` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/careers` | `careers.create_career` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/careers/batch` | `careers.create_careers` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/careers/generate` | `careers.generate_careers` | AI |
| DELETE | `/api/v1/projects/{project_id}/careers/{career_id}` | `careers.delete_career` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/careers/{career_id}` | `careers.get_career` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/careers/{career_id}` | `careers.update_career` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/careers/{career_id}/holders` | `careers.list_holders` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/chapters` | `chapters.list_chapters` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/chapters` | `chapters.create_chapter` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/from-outlines` | `chapters.create_from_outlines` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | `chapters.delete_chapter` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | `chapters.get_chapter` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | `chapters.update_chapter` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/cast` | `chapters.get_cast` | 人工读（候选） |
| PUT | `/api/v1/projects/{project_id}/chapters/{chapter_id}/cast` | `chapters.set_cast` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/foreshadow-reminders` | `foreshadows.chapter_foreshadow_reminders` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/generation-logs` | `chapter_versions.list_logs` | 本人生成日志 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/generation-logs/{log_id}` | `chapter_versions.get_log` | 本人生成日志 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/plan` | `outline_pro.chapter_plan` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/rewrite/apply` | `chapter_versions.apply_rewrite` | AI + 人工写 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes` | `scenes.list_scenes` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes` | `scenes.create_scene` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/merge` | `scenes.merge_scenes` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/merge-preview` | `scenes.merge_preview` | 预览读（候选，需复核副作用） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/reorder` | `scenes.reorder_scenes` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/resolve-position` | `scenes.resolve_position` | 人工读（候选） |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | `scenes.delete_scene` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | `scenes.get_scene` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | `scenes.update_scene` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document` | `scenes.get_document` | 人工读（候选） |
| PUT | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document` | `scenes.save_document` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document/sections/{section_key}/convert` | `scenes.convert_section` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/generate` | `scene_ai.generate_scene` | AI |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/polish` | `scene_ai.polish_scene` | AI |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/revise` | `scene_ai.revise_scene` | AI |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/rewrite` | `scene_ai.rewrite_scene` | AI |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/rewrite/apply` | `scene_ai.apply_scene_rewrite` | AI + 人工写 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/sections/{section_key}/rewrite` | `scene_ai.rewrite_section` | AI |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/sections/{section_key}/rewrite/apply` | `scene_ai.apply_section_rewrite` | AI + 人工写 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/setting-context` | `scenes.setting_context` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/split` | `scenes.split_scene` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/setting-context` | `chapters.get_setting_context` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions` | `chapter_versions.list_versions` | 人工读（候选） |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | `chapter_versions.delete_version` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | `chapter_versions.get_version` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | `chapter_versions.update_version` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/extract` | `chapter_versions.extract_version` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/extract-preview` | `chapter_versions.extract_preview` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/restore` | `chapter_versions.restore_version` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/restore-preview` | `chapter_versions.restore_preview` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/character-careers` | `careers.list_project_assignments` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/characters` | `story.list_items` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/characters` | `story.create_item` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/characters/generate` | `character_actions.generate_character` | AI |
| POST | `/api/v1/projects/{project_id}/characters/with-relations` | `character_actions.create_with_relations` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/career-history` | `careers.list_history` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/careers` | `careers.list_assignments` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/characters/{character_id}/careers` | `careers.assign` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/characters/{character_id}/careers/{assignment_id}` | `careers.remove_assignment` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/characters/{character_id}/careers/{assignment_id}` | `careers.update_assignment` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/memberships` | `organizations.character_memberships` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/characters/{character_id}/parse-relations` | `character_actions.parse_relations` | AI |
| DELETE | `/api/v1/projects/{project_id}/characters/{item_id}` | `story.delete_item` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/characters/{item_id}` | `story.get_item` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/characters/{item_id}` | `story.update_item` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/cover` | `project_cover.delete_cover` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/cover` | `project_cover.get_cover` | 人工读（候选） |
| PUT | `/api/v1/projects/{project_id}/cover` | `project_cover.put_cover` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/cover/generate` | `project_cover.generate_cover` | AI |
| GET | `/api/v1/projects/{project_id}/cover/prompt` | `project_cover.default_prompt` | AI 提示词/私有风格，需复核 |
| GET | `/api/v1/projects/{project_id}/export` | `projects.export_project` | 作品导出 |
| GET | `/api/v1/projects/{project_id}/foreshadows` | `foreshadows.list_foreshadows` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/foreshadows` | `foreshadows.create_foreshadow` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/foreshadows/stats` | `foreshadows.foreshadow_stats` | 人工读（候选） |
| DELETE | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | `foreshadows.delete_foreshadow` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | `foreshadows.get_foreshadow` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | `foreshadows.update_foreshadow` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/abandon` | `foreshadows.abandon_foreshadow` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/history` | `foreshadows.foreshadow_history` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/plant` | `foreshadows.plant_foreshadow` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/resolve` | `foreshadows.resolve_foreshadow` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/kb/documents` | `kb.list_documents` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/kb/documents` | `kb.upload_document` | 人工写 + 按授权创建索引任务 |
| DELETE | `/api/v1/projects/{project_id}/kb/documents/{document_id}` | `kb.delete_document` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/kb/documents/{document_id}` | `kb.update_document` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/kb/documents/{document_id}/chunks` | `kb.list_chunks` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/kb/documents/{document_id}/reindex` | `kb.reindex_document` | AI/向量索引 |
| GET | `/api/v1/projects/{project_id}/kb/index-status` | `kb.index_status` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/kb/rebuild` | `kb.rebuild` | AI/向量索引 |
| POST | `/api/v1/projects/{project_id}/kb/search` | `kb.search` | 检索/向量出站，能力需复核 |
| GET | `/api/v1/projects/{project_id}/kb/settings` | `kb.get_settings` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/kb/settings` | `kb.update_settings` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/markers` | `scenes.list_markers` | 人工读（候选） |
| DELETE | `/api/v1/projects/{project_id}/markers/{scene_id}/{marker_key}` | `scenes.delete_stale_marker` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/markers/{scene_id}/{marker_key}/relink` | `scenes.relink_marker` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/memories` | `memories.list_memories` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/memories` | `memories.create_memory` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/memories/search` | `memories.search_memories` | 检索/向量出站，能力需复核 |
| DELETE | `/api/v1/projects/{project_id}/memories/{memory_id}` | `memories.delete_memory` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/memories/{memory_id}` | `memories.update_memory` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/mentions` | `mentions.list_mentions` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/mentions/catalog` | `mentions.catalog` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/mentions/preview` | `mentions.preview` | 预览读（候选，需复核副作用） |
| GET | `/api/v1/projects/{project_id}/mentions/sources/{source_type}/{source_id}` | `mentions.source_window` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/mentions/summary` | `mentions.summarize` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/notes` | `notes.list_notes` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/notes` | `notes.create_note` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/notes/{note_id}` | `notes.delete_note` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/notes/{note_id}` | `notes.get_note` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/notes/{note_id}` | `notes.update_note` | 人工写（候选） |
| PUT | `/api/v1/projects/{project_id}/notes/{note_id}/flags` | `notes.set_note_flags` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/organizations` | `organizations.list_orgs` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/organizations` | `organizations.create_org` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/organizations/generate` | `organizations.generate_org` | AI |
| POST | `/api/v1/projects/{project_id}/organizations/with-members` | `organizations.create_with_members` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}` | `organizations.delete_org` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/organizations/{org_id}` | `organizations.get_org` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/organizations/{org_id}` | `organizations.update_org` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/organizations/{org_id}/members` | `organizations.list_members` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/organizations/{org_id}/members` | `organizations.add_member` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}/members/{member_id}` | `organizations.remove_member` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/organizations/{org_id}/members/{member_id}` | `organizations.update_member` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}/membership-records/{character_id}` | `organizations.delete_membership_record` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/outline-snapshots` | `outline_pro.list_snapshots` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/outline-snapshots` | `outline_pro.create_snapshot` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/outline-snapshots/{snapshot_id}` | `outline_pro.delete_snapshot` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/outline-snapshots/{snapshot_id}` | `outline_pro.get_snapshot` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/outline-snapshots/{snapshot_id}/restore` | `outline_pro.restore_snapshot` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/outlines` | `story.list_items` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/outlines` | `story.create_item` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/outlines/append` | `outline_pro.append_outlines` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/outlines/continue` | `outline_pro.continue_outline` | AI |
| GET | `/api/v1/projects/{project_id}/outlines/export` | `outline_pro.export_outline` | 大纲读/导出，能力需复核 |
| POST | `/api/v1/projects/{project_id}/outlines/import` | `outline_pro.import_outline` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/outlines/import/preview` | `outline_pro.preview_import` | 大纲读/导出，能力需复核 |
| GET | `/api/v1/projects/{project_id}/outlines/tree` | `outline_pro.outline_tree` | 人工读（候选） |
| DELETE | `/api/v1/projects/{project_id}/outlines/{item_id}` | `story.delete_item` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/outlines/{item_id}` | `story.get_item` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/outlines/{item_id}` | `story.update_item` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/outlines/{outline_id}/expand` | `outline_pro.expand_outline` | AI |
| POST | `/api/v1/projects/{project_id}/outlines/{outline_id}/move` | `outline_pro.move_outline` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/outlines/{outline_id}/plans` | `outline_pro.list_plans` | 人工读（候选） |
| PUT | `/api/v1/projects/{project_id}/outlines/{outline_id}/plans` | `outline_pro.sync_plans` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/outlines/{outline_id}/plans/build` | `outline_pro.build_chapters` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/outlines/{outline_id}/plans/{plan_id}` | `outline_pro.update_plan` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/planning/crossings` | `planning.crossings` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/planning/crossings` | `planning.create_crossing` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/planning/crossings/{crossing_id}` | `planning.delete_crossing` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/planning/crossings/{crossing_id}` | `planning.update_crossing` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/planning/detached` | `planning.detached` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/planning/entries` | `planning.entries` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/planning/matrix` | `planning.matrix` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/planning/subplots` | `planning.subplots` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/planning/subplots` | `planning.create_subplot` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/planning/subplots/{subplot_id}` | `planning.delete_subplot` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/planning/subplots/{subplot_id}` | `planning.update_subplot` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/planning/subplots/{subplot_id}/nodes/{source_kind}/{source_id}` | `planning.delete_node` | 人工写（候选） |
| PUT | `/api/v1/projects/{project_id}/planning/subplots/{subplot_id}/nodes/{source_kind}/{source_id}` | `planning.put_node` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/planning/tags` | `planning.tags` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/planning/tags` | `planning.create_tag` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/planning/tags/{tag_id}` | `planning.delete_tag` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/planning/tags/{tag_id}` | `planning.update_tag` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/planning/units/batch` | `planning.batch` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/planning/units/context-preview` | `planning.context_preview` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/planning/units/merge` | `planning.merge` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/planning/units/{unit_id}/binding` | `planning.binding` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/planning/weave` | `planning.weave` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/reader/chapters/{chapter_id}` | `reader.reader_chapter` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/reader/toc` | `reader.reader_toc` | 人工读（候选） |
| PUT | `/api/v1/projects/{project_id}/reading-progress` | `reader.save_reading_progress` | 作者阅读进度 |
| GET | `/api/v1/projects/{project_id}/relationship-graph` | `relationships.graph` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/relationship-history` | `relationships.list_pair_history` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/relationship-types` | `relationships.list_types` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/relationship-types` | `relationships.create_type` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/relationship-types/{type_id}` | `relationships.delete_type` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/relationships` | `relationships.list_relationships` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/relationships` | `relationships.create_relationship` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/relationships/batch` | `relationships.create_relationships` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/relationships/generate` | `relationships.generate_relationships` | AI |
| DELETE | `/api/v1/projects/{project_id}/relationships/{rel_id}` | `relationships.delete_relationship` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/relationships/{rel_id}` | `relationships.update_relationship` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/relationships/{rel_id}/history` | `relationships.list_history` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/scenes` | `scenes.project_scenes` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/setting-categories` | `setting_categories.categories` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/setting-categories` | `setting_categories.create_category` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/setting-categories/preview` | `setting_categories.preview` | 预览读（候选，需复核副作用） |
| DELETE | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | `setting_categories.delete_category` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | `setting_categories.category` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | `setting_categories.update_category` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-categories/{category_id}/entries` | `setting_categories.entries` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/setting-entries` | `setting_fields.entries` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/ai-settings` | `setting_fields.ai_settings` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/ai-settings` | `setting_fields.update_ai_settings` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields` | `setting_fields.fields` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields/{definition_id}/baseline` | `setting_fields.baseline` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields/{definition_id}/progressions` | `setting_fields.progression` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/legacy-fields/{field_id}/ai-visibility` | `setting_fields.legacy_ai_visibility` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/legacy-fields/{field_id}/binding` | `setting_fields.binding` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/tags` | `setting_categories.tags` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/tags` | `setting_categories.update_tags` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-entry-tags` | `setting_categories.suggestions` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/setting-field-definitions` | `setting_fields.definitions` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/setting-field-definitions` | `setting_fields.create_definition` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | `setting_fields.delete_definition` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | `setting_fields.definition` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | `setting_fields.update_definition` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options` | `setting_fields.create_option` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options/{option_id}` | `setting_fields.delete_option` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options/{option_id}` | `setting_fields.update_option` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-timelines` | `progressions.list_timelines` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/setting-timelines` | `progressions.create_timeline` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/baseline` | `progressions.read_baseline` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/baseline` | `progressions.update_baseline` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/effective` | `progressions.read_effective` | 人工读（候选） |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions` | `progressions.list_progressions` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions` | `progressions.create_progression` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | `progressions.delete_progression` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | `progressions.read_progression` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | `progressions.update_progression` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields` | `progressions.list_text_fields` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields` | `progressions.create_text_field` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields/{field_id}` | `progressions.delete_text_field` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields/{field_id}` | `progressions.update_text_field` | 人工写（候选） |
| PATCH | `/api/v1/projects/{project_id}/wizard-progress` | `projects.update_wizard_progress` | 人工写（候选） |
| POST | `/api/v1/projects/{project_id}/wizard/{kind}` | `wizard.run_wizard` | AI（含任务预览） |
| GET | `/api/v1/projects/{project_id}/world` | `story.list_world` | 人工读（候选） |
| POST | `/api/v1/projects/{project_id}/world` | `story.create_item` | 人工写（候选） |
| DELETE | `/api/v1/projects/{project_id}/world/{item_id}` | `story.delete_item` | 人工写（候选） |
| GET | `/api/v1/projects/{project_id}/world/{item_id}` | `story.get_item` | 人工读（候选） |
| PATCH | `/api/v1/projects/{project_id}/world/{item_id}` | `story.update_item` | 人工写（候选） |
| GET | `/api/v1/prompt-components` | `prompt_components.list_components` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/prompt-components` | `prompt_components.create_component` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/prompt-components/export` | `prompt_components.export_components` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/prompt-components/import` | `prompt_components.import_components` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/prompt-components/{component_id}` | `prompt_components.delete_component` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/prompt-components/{component_id}` | `prompt_components.get_component` | 账户/管理/开放 API，协作范围外 |
| PUT | `/api/v1/prompt-components/{component_id}` | `prompt_components.update_component` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/prompt-templates` | `prompt_templates.list_templates` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/prompt-templates/export` | `prompt_templates.export_templates` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/prompt-templates/import` | `prompt_templates.import_templates` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/prompt-templates/{key}` | `prompt_templates.reset_template` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/prompt-templates/{key}` | `prompt_templates.get_template` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/prompt-templates/{key}` | `prompt_templates.patch_template` | 账户/管理/开放 API，协作范围外 |
| PUT | `/api/v1/prompt-templates/{key}` | `prompt_templates.save_template` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/prompt-templates/{key}/preview` | `prompt_templates.preview_template` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/prompt-templates/{key}/versions` | `prompt_templates.list_versions` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/prompt-templates/{key}/versions/{version_id}/restore` | `prompt_templates.restore_version` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/series` | `series.list_series` | 系列读（书籍投影过滤） |
| POST | `/api/v1/series` | `series.create_series` | 系列作者专用 |
| DELETE | `/api/v1/series/{sid}` | `series.delete_series` | 系列作者专用 |
| GET | `/api/v1/series/{sid}` | `series.read_series` | 系列读（书籍投影过滤） |
| PATCH | `/api/v1/series/{sid}` | `series.rename_series` | 系列作者专用 |
| POST | `/api/v1/series/{sid}/books` | `series.join_book` | 系列作者专用 |
| DELETE | `/api/v1/series/{sid}/books/{pid}` | `series.leave_book` | 系列作者专用 |
| GET | `/api/v1/series/{sid}/entries` | `series.list_entries` | 系列读 |
| POST | `/api/v1/series/{sid}/entries` | `series.create_entry` | 系列编辑（删除需校验绑定） |
| POST | `/api/v1/series/{sid}/entries/promote` | `series.promote_entry` | 系列+受影响作品双重权限 |
| DELETE | `/api/v1/series/{sid}/entries/{eid}` | `series.delete_entry` | 系列编辑（删除需校验绑定） |
| GET | `/api/v1/series/{sid}/entries/{eid}` | `series.read_entry` | 系列读 |
| PATCH | `/api/v1/series/{sid}/entries/{eid}` | `series.patch_entry` | 系列编辑（删除需校验绑定） |
| POST | `/api/v1/series/{sid}/entries/{eid}/move-to-book` | `series.move_entry` | 系列+受影响作品双重权限 |
| POST | `/api/v1/series/{sid}/entries/{eid}/uses` | `series.use_entry` | 系列+受影响作品双重权限 |
| DELETE | `/api/v1/series/{sid}/entries/{eid}/uses/{pid}` | `series.unuse_entry` | 系列+受影响作品双重权限 |
| GET | `/api/v1/series/{sid}/impact` | `series.impact` | 系列+受影响作品双重权限 |
| GET | `/api/v1/skills` | `skills.list_skills` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/skills` | `skills.create_skill` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/skills/import` | `skills.import_skill` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/skills/{skill_id}` | `skills.delete_skill` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/skills/{skill_id}` | `skills.get_skill` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/skills/{skill_id}` | `skills.update_skill` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/skills/{skill_id}/copy` | `skills.copy_skill` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/skills/{skill_id}/export` | `skills.export_skill` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/skills/{skill_id}/preview` | `skills.preview_skill` | 账户/管理/开放 API，协作范围外 |
| PUT | `/api/v1/skills/{skill_id}/references` | `skills.replace_references` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/skills/{skill_id}/restore-builtin` | `skills.restore_builtin` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/styles` | `styles.list_styles` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/styles` | `styles.create_style` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/styles/export` | `styles.export_styles` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/styles/extract` | `styles.extract_style` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/styles/import` | `styles.import_styles` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/styles/trial` | `styles.trial_style` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/styles/{style_id}` | `styles.delete_style` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/styles/{style_id}` | `styles.get_style` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/styles/{style_id}` | `styles.update_style` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/styles/{style_id}/copy` | `styles.copy_style` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/tasks` | `tasks.clear_finished` | 本人任务/当前协作；活动 AI 需授权 |
| GET | `/api/v1/tasks` | `tasks.list_tasks` | 本人任务/当前协作；活动 AI 需授权 |
| DELETE | `/api/v1/tasks/{task_id}` | `tasks.delete_task` | 本人任务/当前协作；活动 AI 需授权 |
| GET | `/api/v1/tasks/{task_id}` | `tasks.get_task` | 本人任务/当前协作；活动 AI 需授权 |
| POST | `/api/v1/tasks/{task_id}/cancel` | `tasks.cancel_task` | 本人任务/当前协作；活动 AI 需授权 |
| POST | `/api/v1/tasks/{task_id}/finish` | `tasks.finish_task` | 本人任务/当前协作；活动 AI 需授权 |
| GET | `/api/v1/tasks/{task_id}/output` | `tasks.get_output` | 本人任务/当前协作；活动 AI 需授权 |
| POST | `/api/v1/tasks/{task_id}/resume` | `tasks.resume_task` | 本人任务/当前协作；活动 AI 需授权 |
| POST | `/api/v1/tasks/{task_id}/retry` | `tasks.retry_task` | 本人任务/当前协作；活动 AI 需授权 |
| GET | `/api/v1/tasks/{task_id}/rich-proposals/{token}` | `tasks.get_rich_proposal` | 本人任务/当前协作；活动 AI 需授权 |
| POST | `/api/v1/tasks/{task_id}/rich-proposals/{token}/accept` | `tasks.accept_rich_proposal` | 本人任务/当前协作；活动 AI 需授权 |
| GET | `/api/v1/tasks/{task_id}/stream` | `tasks.stream_task` | 本人任务/当前协作；活动 AI 需授权 |
| POST | `/api/v1/workshop/admin/items` | `workshop.publish` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/workshop/admin/items/{item_id}` | `workshop.delete_item` | 账户/管理/开放 API，协作范围外 |
| PATCH | `/api/v1/workshop/admin/items/{item_id}` | `workshop.patch_metadata` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/workshop/admin/submissions` | `workshop.review_list` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/workshop/admin/submissions/{submission_id}/approve` | `workshop.approve` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/workshop/admin/submissions/{submission_id}/reject` | `workshop.reject` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/workshop/items` | `workshop.list_items` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/workshop/items/{item_id}` | `workshop.detail` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/workshop/items/{item_id}/hide` | `workshop.hide` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/workshop/items/{item_id}/import` | `workshop.import_item` | 账户/管理/开放 API，协作范围外 |
| DELETE | `/api/v1/workshop/items/{item_id}/like` | `workshop.unlike` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/workshop/items/{item_id}/like` | `workshop.like` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/workshop/items/{item_id}/unhide` | `workshop.unhide` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/workshop/mine/items` | `workshop.my_items` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/workshop/submissions` | `workshop.my_submissions` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/workshop/submissions` | `workshop.submit` | 账户/管理/开放 API，协作范围外 |
| GET | `/api/v1/workshop/submissions/{submission_id}` | `workshop.get_submission` | 账户/管理/开放 API，协作范围外 |
| POST | `/api/v1/workshop/submissions/{submission_id}/withdraw` | `workshop.withdraw` | 账户/管理/开放 API，协作范围外 |
| GET | `/healthz` | `main.healthz` | 账户/管理/开放 API，协作范围外 |
