# 现有代码基础盘点（2026-11-17）

只读调研结论，行号为调研时位置；实施前须对照代码复核。

## 章节分析
- `ChapterAnalysis` / `chapter_analyses`（backend/app/models/entities.py:857）：每次分析一行，取 `max(id)` 为最新；含 `content_hash`、`summary`、`plot_points`、`hooks`、`changes_raw`、`foreshadow_candidates` 等。`AnalysisChange` / `analysis_changes`（:890）记录识别出的设定变化及其写入状态。
- 服务 backend/app/services/analysis_service.py：`content_hash`:129、`normalize_analysis`:251、`latest_analyses`:406、`project_states`:587、`request_analyze`:762、`analyze_pending`:825、`enqueue_auto`:943、`analysis_bindings`:1186、`build_analysis_job`:2034、`run_analysis_job`:2243。
- 状态 none/analyzing/analyzed/stale（backend/app/schemas/analysis.py:8）；过期由 `analysis_fingerprints`（backend/app/services/rich_document_ai.py:61）判定：无开启区块时为正文 sha256，开启 AI 可见的作者区块计入指纹。
- 任务 `chapter.analyze`（backend/app/services/task_engine/handlers/analysis.py:18），`serial_key=analysis:{project_id}`，不写生成记录、不记 token。
- 提示词键 `analysis.chapter`（backend/app/ai/prompts/analysis.py:15，templates/analysis.chapter.{system,user}.j2，formats.py:3 的 `ANALYSIS_SCHEMA_WITH_FORESHADOW_REFS` 演示了「引用已有条目 id」的输出约定）。
- 结构化抽取只有 `character_changes`（current_state / life_status）；没有人物经历、物品去向、时间顺序。

## 诊断技能
- backend/app/skills/builtin/chapter-diagnose.md、foreshadow-check.md 为 Markdown 工作流，输出自由文本；仅保存聊天消息与 `AgentMessage.skill_snapshot`（backend/app/models/agent.py:86），无问题表、无状态。

## 剧情记忆
- `StoryMemory` / `story_memories`（entities.py:1134）：`chapter_id`、`chapter_no`、`kind`、`analysis_id`、`origin`、`pinned`、`muted`、`content_key`。
- backend/app/services/memory_service.py：`extract_items`:95、`sync_from_analysis`:192、`on_chapters_deleted`:272、`on_chapter_renumbered`:285、`_memory_outputs`:309（计算只读 `source_stale`：来源分析章号或 `content_hash` 与当前不同）、`recall`:490。前端 frontend/src/components/kb/MemorySourceBadge.tsx。

## 伏笔
- `Foreshadow` / `foreshadows`（entities.py:914）：状态 pending/planted/resolved/partial/abandoned；四个章号字段（非外键）；`include_in_context`。历史 `ForeshadowHistory`（:955）。
- backend/app/services/foreshadow_service.py：`overdue_by`:80、`apply_foreshadow`:160、`chapter_reminders`:589、`analysis_candidates`:615。

## 章节摘要
- `Chapter` 无 summary 列；章级摘要 = 最新 `ChapterAnalysis.summary`。场景摘要 `Scene.summary` 为作者手写。
- 「可能过期」判定在 backend/app/services/document_export/story.py:153–179，与分析过期同一套指纹。

## 正文版本与场景
- `Chapter.content_revision`（entities.py:583）每次正文写入 +1，`scene_service.touch_revision`:124 CAS，冲突 409 `stale_scene_revision`。`Chapter.content` 是活动场景的有序投影（`refresh_projection`:137）。
- `ChapterVersion` / `chapter_versions`（entities.py:768）。`Scene` / `scenes`，`SceneSection`（`include_in_ai`）、`SceneMarker`（backend/app/models/rich_document.py:35、:64）。
- 章内偏移转场景偏移：`scene_service.resolve_position`:769，`GET .../resolve-position`（backend/app/api/v1/scenes.py:96），指纹不一致返回 `reason="stale"`。
- 没有统一的「正文已变更」事件；结构变化钩子 `setting_progression_service.on_story_positions_changed`（chapter_service.py:416、:521）。

## 设定演变与提及追踪
- `SettingProgression`（backend/app/models/progressions.py:124）：`anchor_kind` chapter/scene/planned、`source`、`source_change_id`、`source_content_hash`（analysis_progression_service.py:135 写入，目前只存不查）。
- 提及无持久表，实时字面匹配（backend/app/services/mentions/matcher.py、sources.py）。锚点 `{start,end,content_hash}`，码点偏移，`text_hash` 为来源全文 sha256（matcher.py:69，schemas/mentions.py:46–59）。失效判定 `mentions/window.py:17`：哈希不同即 `stale=True`、不定位。接口 `GET /projects/{pid}/mentions/sources/{type}/{id}`（api/v1/mentions.py:90）。

## 后台任务
- `Task` / `tasks`（entities.py:793）：`step_cursor`、`checkpoint`、`resource_key`、`serial_key`。引擎 backend/app/services/task_engine/engine.py:224；Handler 协议 registry.py:52；多步参考 handlers/batch.py（`execution_identity_aware`、`admission_preflight_aware`、`collaboration_execution_ready` 均为 True）。
- task_service.py：`create_task`:471、`cancel_task`:1001、`retry_task`:657。SSE `GET /tasks/{id}/stream`（api/v1/tasks.py:60）。前端 frontend/src/api/tasks.ts:345 `subscribeTask`、stores/tasks.ts、hooks/useTaskStream.ts、components/TaskPanel.tsx。
- 模型：project_ai_config.py `preflight_project_model`:548、`resolve_project_model`:832、`revalidate_project_model`:915；出站门禁 `ai_dispatch.guard_provider`（ai_dispatch.py:708），协作者 `collaboration_ai_guard.for_task`:1474。
- `GenerationLog`（entities.py:664）有 token 字段，但仅智能体填写。

## 权限
- `project_access.require_project(db, actor_id, project_id, capability, *, for_update, visibility_write, grant_identity)`（backend/app/services/project_access.py:122）；能力 `content.read`、`content.edit`、`export`、`ai.use`（:16）；`can_use_ai = owner or editor`，查看者 403 `project_readonly`，非协作者 404。前端 frontend/src/hooks/useProjectAccess.ts。

## 备份
- backend/app/services/project_transfer/exporter.py:34 `VERSION = 11`、:36 `APP_SCHEMA = "0057"`。registry.py `RULES`:177 / `NOT_EXPORTED`:90；importer.py:510–535 `migrate`；JSON 内的 id 需在 remap.py 重映射。覆盖测试 backend/tests/test_project_transfer.py:72。

## 前端路由与跳转
- 路由 frontend/src/router.tsx:106–127（`projects/:pid` 子路由懒加载）；导航 frontend/src/components/StudioNavigation.tsx:31–54 `PROJECT_GROUPS`。
- 跳转：frontend/src/utils/mentions.ts:26 `mentionHref` → `/projects/{pid}/read/{chapterId}?mention_start&mention_end&mention_hash`；消费方 ReaderPage.tsx:255、ChapterEditorPage.tsx:183，经 components/scenes/SceneWritingPanel.tsx:243–246 调 `resolvePosition`。

## 迁移与门禁
- 迁移目录 backend/alembic/versions/，最新 `0057_novel_templates.py`，下一个 0058，并同步 `exporter.APP_SCHEMA`。
- 后端：`cd backend && uv run ruff check . && uv run pytest`（macOS locale 问题时加 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`）；PG 测试需 `AINOVEL_COLLAB_POSTGRES_TEST_URL`。
- 前端：`cd frontend && npm run lint && npm run build && npm test`。
