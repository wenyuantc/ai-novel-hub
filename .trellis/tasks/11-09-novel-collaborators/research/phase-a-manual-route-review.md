# 阶段 A1：人工内容路由复核

本表按 `phase-a-route-inventory.md` 中指定的 16 个模块逐方法复核（不是整个 469 操作的 A1 审计），以 API 函数及其实际 service 调用为索引。通过应用工厂递归展开当前 FastAPI `_IncludedRouter` 的 `APIRoute`，上述模块实际有 167 个方法/路径，均包含在原清单，未出现新增未列路由。章节/场景 AI、个人日志、规划/分析、知识库/记忆、Agent、任务、系列及账户级/开放 API 由相应代理或后续阶段负责；不以此报告宣称完整 Gate A/D。卡片由 `cards_access`、AI 可见性及 `ai_hidden`/legacy binding 由 `visibility_access` 并行修改，表内只标进行中，必须以其最终审查和测试为准。源码行号记录生成时快照，其他代理可能继续修改，使用者应重新核对。

## 判读约定

- **R**：实际人工 `content.read`。作者、accepted viewer/editor 可读；既有拥有 `projects.read` 的管理员可读。非成员无管理员授权为 404；身份重叠按各自来源求并集。
- **W**：实际人工 `content.edit`，写入路径须持项目父行锁并在锁后重读身份；作者、accepted editor 或同时拥有 `projects.read`/`projects.edit` 的管理员可写；纯 viewer 为 403，非成员无管理员授权为 404。管理员既有审计和各入口的专用限制仍适用；管理员身份本身不授予导出、AI、个人日志或备份。`project_access.py` 的 `access_for`/`resolve_access`/`require_project` 分别是 capability、锁后重读、错误入口（`backend/app/services/project_access.py:41,64,117`）。
- **X**：站内文本导出明确走 `export`，作者和 accepted 协作者可用，纯管理员不因此获得权限；过滤章节属于目标项目，缺席/外部 ID 不被读取。默认 EPUB 署书作者。**作者** 为严格作者资源，**回收** 为专用原管理员权限，**列表/创建** 不视为普通人工 R/W。
- **旧确认入口**：当前 service 仍用 `get_manageable_project(..., permission="projects.edit")`，作者及符合旧 RBAC 的管理员可用，单纯协作者尚未接入。它们是 AI 草案/批量确认等路径，不能依据 POST 断定已迁移；按主会话任务分工继续处理。
- **上下文进行中**：本次复核中规划代理正在实现这两个入口：当前章节入口改用 `get_manual_chapter(..., "content.read")`，场景入口传 `manual=True`，但仍使用 `include_protected=True`。需由 `check_planning` 对真实 actor 的受保护内容投影及版本检查完成独立测试；不要将中途代码视为已经通过。见 `backend/app/api/v1/chapters.py:62`、`backend/app/services/planning_context_service.py:733`。
- **AI/E**、**个人日志/E**：不是普通人工读写。沿实际代码检查由 E 和相应代理完成；仅列出边界，不作协作者 AI 或 viewer 403 已完成的声明。无项目 ID 的章节 AI 入口需由章节查回父作品再鉴权。
- **策略进行中** / **W/策略进行中**：字段普通部分与 `ai_hidden` 的实际分支、隐藏设置、旧字段绑定和旧 AI 可见性由 `visibility_access` 修改中；不从现存代码推断已完成。
- **卡片进行中**：三条卡片路由携带卡片模型、策略和系列冲突，角色矩阵、权限与脱敏由 `cards_access` 实施/核验中，不能把 POST 导出或预览简单归为内容写。

## 父子资源限定代号

- **P**：校验项目及生命周期；文本导出按 `Chapter.project_id` 限定并过滤给定 `chapter_ids`，见 `backend/app/services/rich_document_render.py:155`。
- **C**：章节受项目约束（`chapter_service._chapter_in_project:208`）；创建引用大纲额外用 `_get_outline:231`，cast 限定章节及角色所属项目；reader 使用目标项目过滤。
- **V**：先 `get_manual_chapter` 验项目和章节，再 `_get_version` 约束 `ChapterVersion.chapter_id`；日志 ID 按真实 actor 与当前作者/accepted 成员过滤，见 `chapter_version_service.py:22,41,53`。恢复和提取持发送门禁及版本校验。
- **S**：`get_manual_chapter`/`get_manual_write_chapter` 约束项目与章，`require_scene` 限定场景属于该章；项目范围场景列表按项目过滤；标记先 `_source_chapter_id` 用 `Scene → Chapter.project_id` 回查，再限定目标章、景及逻辑键，见 `scene_service.py:104,398,442`、`marker_lifecycle_service.py:17,37`。
- **ST**：角色/世界/大纲 `_get_item` 用 `(project_id,item_id)`；角色/世界投影 `series_read` 根据系列身份屏蔽 ID，单书成员使用通用共享设定冲突，见 `story_service.py:118,124,158`、`series_read.py:14,28`、`series_policy.py:54`。
- **FS**：伏笔读写按 `Foreshadow.project_id`；相关角色 ID 用 `_validate_characters` 限定同项目；章节提醒先校验父章，见 `foreshadow_service.py:117,275,589`。
- **CA**：职业先限定 `Career.project_id`；阶段限定所属职业；人物挂接先确认人物所属项目，再由 assignment 限定人物且职业限本项目，见 `career_service.py:173,188,207,879,892`。
- **OR**：组织按项目，成员按组织；引用人物和父组织均限定项目，见 `organization_service.py:181,212,385,499`。
- **RL**：关系及自定义类型限定项目（预置类型为公共类型）；人物两端先限定项目才查询全局 pair；历史从该关系或同项目人物对读取。新发现的外书 pair 存在性泄露及修复见下节和 `relationship_service.py:632,759,947`。
- **SF**：目录条目按 `(project_id,kind,id)`；字段定义按项目、option 按定义；字段值、事件依所查时间线绑定，见 `setting_catalog_service.py:124`、`setting_field_service.py:119,770,1177`。AI 配置相关分支由 `visibility_access` 接手。
- **SC**：分类独立一致快照同时验证 actor、项目与版本；分类/字段/选项/标签候选按项目，见 `setting_category_query_service.py:102,170`、`setting_category_service.py:49`。预览不提交，必须按 service 行为分类为 R，而非按 POST 分类。
- **TL**：先 `get_timeline(project_id,timeline_id)`，事件/文本字段再限定 timeline；剧情位置限定项目的章/景，见 `setting_progression_service.py:116,301,1197,1477`。

## 发现与验证边界

- 关系新增曾先全局 `find_pair_record` 再校验角色所属项目：真实 HTTP 对外书已存在关系返回 409，而不存在返回 400，可探测外书关系。复核中已按主代理授权修复：在任何类型解析和 pair 查询前，于 `no_autoflush` 下确认两端属于当前作品；外书角色统一 400，合法同书冲突仍为 409。由于关系与角色是独立外键，还拒绝复用归属其他项目的异常旧关系载体，返回普通 pair 冲突；测试覆盖隐藏和活动载体。角色矩阵及外层事务不提前 flush 见 `backend/tests/test_collaboration_relationship_scope.py`。
- `LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_collaboration_relationship_scope.py tests/test_relationships.py tests/test_relationship_progressions.py`：50 passed；含关系卡片导入 `tests/test_cards.py::test_cards_roundtrip_with_rename` 及系列身份反例各单独 1 passed。本次修改范围 Ruff 与 `git diff --check` 通过。
- 写入口错误优先级交 `check_stage_b`：HTTP 实测 viewer 转换区块提交错误 revision 时，`rich_document_conversion.convert_section` 先做读取和 revision 检查，返回 409 `stale_scene_revision` 及当前代次；正确 revision 才由 `save_document` 返回 403。测试中无持久写入，但需按 viewer 写入 403 的契约决定是否提前鉴权；最终结果以负责代理复核为准。
- 前端关系草稿风险已交 `frontend_project`：早先普通 refresh 曾无条件变更 scope，`RelationEditModal.tsx:90-98,127` 的 effect 随 `canReadNow` 变化会调用 `form.resetFields()`；基础层现已改为 `stores/project.ts:135-136` 在 `sameAccess` 时保留 scope，普通未变更 refresh 不再触发该条件。授权轮次确实变化时的草稿处理仍需该代理验证；本次未修改前端。
- POST `scenes/merge-preview` 实际为 `no_autoflush` 只读预览；POST 分类 `preview` 在独立事务一致快照读取后 rollback；不以方法判断为 W。`rich_document_conversion.convert_section` 先读/纯计算，再由 `save_document` 在写锁内检查 W 和 revision。
- 原严格 `story_service.get_owned/get_manageable`、`chapter_service.get_owned_chapter/get_project_chapter` 仍存在；本表记录 HTTP 显式人工入口，不代表 Agent、开放 API、任务可借此取得共享能力。风格绑定明确非作者禁止提交（包括显式 null）；章节和项目输出需做 actor 过滤。
- 已有代表 HTTP/服务证据：`test_collaboration_content_access.py::test_chapter_reader_version_and_export_access_matrix`、`::test_scene_document_and_structure_respect_editor_and_viewer`、`::test_open_api_key_does_not_inherit_web_collaboration`；`test_collaboration_story_access.py::test_story_manual_access_and_strict_agent_helper`、`::test_manual_entity_families_enforce_role_and_project_boundaries`、`::test_admin_viewer_union_preserves_anonymous_series_projection`；真实项目行锁竞争见 `test_collaboration_story_postgresql.py`。这些是代表证据，不是对每个路由和所有数据分支的独立 HTTP 测试。

## 操作清单

“路由 → service”附可追溯的项目内相对链接；动态 story 路由同一函数注册多个资源路径。状态是本次静态核对结论及进行中标识。

本范围：167 个方法和路径，生成于 2026-11-09 17:49:59 本地时间。

### projects（7）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects` | [projects.list_projects](../../../../backend/app/api/v1/projects.py#L14) → [project_service.list_projects](../../../../backend/app/services/project_service.py#L212) | 列表/创建 | P |
| POST | `/api/v1/projects` | [projects.create_project](../../../../backend/app/api/v1/projects.py#L19) → [project_service.create_project](../../../../backend/app/services/project_service.py#L298)、[project_service.project_out](../../../../backend/app/services/project_service.py#L254) | 列表/创建 | P |
| DELETE | `/api/v1/projects/{project_id}` | [projects.delete_project](../../../../backend/app/api/v1/projects.py#L45) → [project_service.delete_project](../../../../backend/app/services/project_service.py#L356) | 回收 | P |
| GET | `/api/v1/projects/{project_id}` | [projects.get_project](../../../../backend/app/api/v1/projects.py#L25) → [project_access.require_project](../../../../backend/app/services/project_access.py#L122)、[project_service.project_out](../../../../backend/app/services/project_service.py#L254) | R | P |
| PATCH | `/api/v1/projects/{project_id}` | [projects.update_project](../../../../backend/app/api/v1/projects.py#L31) → [project_service.update_project](../../../../backend/app/services/project_service.py#L327)、[project_service.project_out](../../../../backend/app/services/project_service.py#L254) | W | P |
| GET | `/api/v1/projects/{project_id}/export` | [projects.export_project](../../../../backend/app/api/v1/projects.py#L51) → [export_service.export_manual_project](../../../../backend/app/services/export_service.py#L116) | X | P |
| PATCH | `/api/v1/projects/{project_id}/wizard-progress` | [projects.update_wizard_progress](../../../../backend/app/api/v1/projects.py#L37) → [project_service.update_wizard_progress](../../../../backend/app/services/project_service.py#L373)、[project_service.project_out](../../../../backend/app/services/project_service.py#L254) | 作者 | P |

### chapters（11）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| POST | `/api/v1/chapters/{chapter_id}/generate` | [chapters.generate_chapter](../../../../backend/app/api/v1/chapters.py#L117) → [task_service.create_task](../../../../backend/app/services/task_service.py#L210) | AI/E | 待 AI/日志代理核验 |
| POST | `/api/v1/chapters/{chapter_id}/polish` | [chapters.polish_chapter](../../../../backend/app/api/v1/chapters.py#L129) → [task_service.create_task](../../../../backend/app/services/task_service.py#L210) | AI/E | 待 AI/日志代理核验 |
| GET | `/api/v1/projects/{project_id}/chapters` | [chapters.list_chapters](../../../../backend/app/api/v1/chapters.py#L32) → [chapter_service.list_manual_chapters](../../../../backend/app/services/chapter_service.py#L223)、[chapter_service.manual_chapter_views](../../../../backend/app/services/chapter_service.py#L157) | R | C |
| POST | `/api/v1/projects/{project_id}/chapters` | [chapters.create_chapter](../../../../backend/app/api/v1/chapters.py#L38) → [chapter_service.create_chapter](../../../../backend/app/services/chapter_service.py#L243)、[chapter_service.manual_chapter_views](../../../../backend/app/services/chapter_service.py#L157) | W | C |
| POST | `/api/v1/projects/{project_id}/chapters/from-outlines` | [chapters.create_from_outlines](../../../../backend/app/api/v1/chapters.py#L48) → [chapter_service.create_from_outlines](../../../../backend/app/services/chapter_service.py#L282)、[chapter_service.manual_chapter_views](../../../../backend/app/services/chapter_service.py#L157) | W | C |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | [chapters.delete_chapter](../../../../backend/app/api/v1/chapters.py#L98) → [chapter_service.delete_chapter](../../../../backend/app/services/chapter_service.py#L490) | W | C |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | [chapters.get_chapter](../../../../backend/app/api/v1/chapters.py#L56) → [chapter_service.get_manual_chapter](../../../../backend/app/services/chapter_service.py#L111)、[chapter_service.manual_chapter_views](../../../../backend/app/services/chapter_service.py#L157) | R | C |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | [chapters.update_chapter](../../../../backend/app/api/v1/chapters.py#L90) → [chapter_service.update_chapter](../../../../backend/app/services/chapter_service.py#L334)、[chapter_service.manual_chapter_views](../../../../backend/app/services/chapter_service.py#L157) | W | C |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/cast` | [chapters.get_cast](../../../../backend/app/api/v1/chapters.py#L104) → [cast_service.get_cast](../../../../backend/app/services/cast_service.py#L142) | R | C |
| PUT | `/api/v1/projects/{project_id}/chapters/{chapter_id}/cast` | [chapters.set_cast](../../../../backend/app/api/v1/chapters.py#L109) → [cast_service.set_cast](../../../../backend/app/services/cast_service.py#L152) | W | C |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/setting-context` | [chapters.get_setting_context](../../../../backend/app/api/v1/chapters.py#L62) → [chapter_service.get_manual_chapter](../../../../backend/app/services/chapter_service.py#L111)、[project_access.require_project](../../../../backend/app/services/project_access.py#L122) | 上下文进行中 | C（规划代理核验中） |

### chapter_versions（12）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| POST | `/api/v1/chapters/{chapter_id}/rewrite` | [chapter_versions.rewrite_preview](../../../../backend/app/api/v1/chapter_versions.py#L80) → [rewrite_service.prepare_rewrite_job](../../../../backend/app/services/rewrite_service.py#L81) | AI/E | 待 AI/日志代理核验 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/generation-logs` | [chapter_versions.list_logs](../../../../backend/app/api/v1/chapter_versions.py#L65) → [generation_log_service.list_logs](../../../../backend/app/services/generation_log_service.py#L137) | 个人日志/E | 待 AI/日志代理核验 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/generation-logs/{log_id}` | [chapter_versions.get_log](../../../../backend/app/api/v1/chapter_versions.py#L70) → [generation_log_service.get_log](../../../../backend/app/services/generation_log_service.py#L149) | 个人日志/E | 待 AI/日志代理核验 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/rewrite/apply` | [chapter_versions.apply_rewrite](../../../../backend/app/api/v1/chapter_versions.py#L75) → [rewrite_service.apply_rewrite](../../../../backend/app/services/rewrite_service.py#L384) | AI/E | 待 AI/日志代理核验 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions` | [chapter_versions.list_versions](../../../../backend/app/api/v1/chapter_versions.py#L31) → [chapter_version_service.list_versions](../../../../backend/app/services/chapter_version_service.py#L41) | R | V |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | [chapter_versions.delete_version](../../../../backend/app/api/v1/chapter_versions.py#L50) → [chapter_version_service.delete_version](../../../../backend/app/services/chapter_version_service.py#L129) | W | V |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | [chapter_versions.get_version](../../../../backend/app/api/v1/chapter_versions.py#L36) → [chapter_version_service.get_version](../../../../backend/app/services/chapter_version_service.py#L93) | R | V |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | [chapter_versions.update_version](../../../../backend/app/api/v1/chapter_versions.py#L43) → [chapter_version_service.update_version](../../../../backend/app/services/chapter_version_service.py#L106) | W | V |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/extract` | [chapter_versions.extract_version](../../../../backend/app/api/v1/chapter_versions.py#L123) → [chapter_version_service.extract_version](../../../../backend/app/services/chapter_version_service.py#L275) | W | V |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/extract-preview` | [chapter_versions.extract_preview](../../../../backend/app/api/v1/chapter_versions.py#L114) → [chapter_version_service.extract_preview](../../../../backend/app/services/chapter_version_service.py#L243) | R | V |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/restore` | [chapter_versions.restore_version](../../../../backend/app/api/v1/chapter_versions.py#L56) → [chapter_version_service.restore_version](../../../../backend/app/services/chapter_version_service.py#L191)、[chapter_service.manual_chapter_views](../../../../backend/app/services/chapter_service.py#L157) | W | V |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/restore-preview` | [chapter_versions.restore_preview](../../../../backend/app/api/v1/chapter_versions.py#L106) → [chapter_version_service.restore_preview](../../../../backend/app/services/chapter_version_service.py#L144) | R | V |

### scenes（18）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes` | [scenes.list_scenes](../../../../backend/app/api/v1/scenes.py#L82) → [scene_service.list_scenes](../../../../backend/app/services/scene_service.py#L442) | R | S |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes` | [scenes.create_scene](../../../../backend/app/api/v1/scenes.py#L90) → [scene_service.create_scene](../../../../backend/app/services/scene_service.py#L510) | W | S |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/merge` | [scenes.merge_scenes](../../../../backend/app/api/v1/scenes.py#L120) → [scene_service.merge_scenes](../../../../backend/app/services/scene_service.py#L688) | W | S |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/merge-preview` | [scenes.merge_preview](../../../../backend/app/api/v1/scenes.py#L113) → [scene_service.merge_preview](../../../../backend/app/services/scene_service.py#L680) | R | S |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/reorder` | [scenes.reorder_scenes](../../../../backend/app/api/v1/scenes.py#L106) → [scene_service.reorder_scenes](../../../../backend/app/services/scene_service.py#L575) | W | S |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/resolve-position` | [scenes.resolve_position](../../../../backend/app/api/v1/scenes.py#L97) → [scene_service.resolve_position](../../../../backend/app/services/scene_service.py#L769) | R | S |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | [scenes.delete_scene](../../../../backend/app/api/v1/scenes.py#L146) → [scene_service.delete_scene](../../../../backend/app/services/scene_service.py#L737) | W | S |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | [scenes.get_scene](../../../../backend/app/api/v1/scenes.py#L127) → [scene_service.get_scene](../../../../backend/app/services/scene_service.py#L484) | R | S |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | [scenes.update_scene](../../../../backend/app/api/v1/scenes.py#L132) → [scene_service.update_scene](../../../../backend/app/services/scene_service.py#L542) | W | S |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document` | [scenes.get_document](../../../../backend/app/api/v1/scenes.py#L163) → [rich_document_service.get_document](../../../../backend/app/services/rich_document_service.py#L30) | R | S |
| PUT | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document` | [scenes.save_document](../../../../backend/app/api/v1/scenes.py#L170) → [rich_document_service.save_document](../../../../backend/app/services/rich_document_service.py#L295) | W | S |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document/sections/{section_key}/convert` | [scenes.convert_section](../../../../backend/app/api/v1/scenes.py#L177) → [rich_document_conversion.convert_section](../../../../backend/app/services/rich_document_conversion.py#L95) | W | S |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/setting-context` | [scenes.setting_context](../../../../backend/app/api/v1/scenes.py#L154) → [planning_context_service.scene_setting_context](../../../../backend/app/services/planning_context_service.py#L733) | 上下文进行中 | S（规划代理核验中） |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/split` | [scenes.split_scene](../../../../backend/app/api/v1/scenes.py#L139) → [scene_service.split_scene](../../../../backend/app/services/scene_service.py#L594) | W | S |
| GET | `/api/v1/projects/{project_id}/markers` | [scenes.list_markers](../../../../backend/app/api/v1/scenes.py#L46) → [marker_timeline_service.list_markers](../../../../backend/app/services/marker_timeline_service.py#L29) | R | S |
| DELETE | `/api/v1/projects/{project_id}/markers/{scene_id}/{marker_key}` | [scenes.delete_stale_marker](../../../../backend/app/api/v1/scenes.py#L64) → [marker_lifecycle_service.delete_stale](../../../../backend/app/services/marker_lifecycle_service.py#L100) | W | S |
| POST | `/api/v1/projects/{project_id}/markers/{scene_id}/{marker_key}/relink` | [scenes.relink_marker](../../../../backend/app/api/v1/scenes.py#L57) → [marker_lifecycle_service.relink](../../../../backend/app/services/marker_lifecycle_service.py#L37) | W | S |
| GET | `/api/v1/projects/{project_id}/scenes` | [scenes.project_scenes](../../../../backend/app/api/v1/scenes.py#L74) → [scene_service.list_scenes](../../../../backend/app/services/scene_service.py#L442) | R | S |

### story（15）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/characters` | [story.list_items](../../../../backend/app/api/v1/story.py#L46) → [series_read.read_book_settings](../../../../backend/app/services/series_read.py#L29) | R | ST |
| POST | `/api/v1/projects/{project_id}/characters` | [story.create_item](../../../../backend/app/api/v1/story.py#L52) → [story_service.create_owned](../../../../backend/app/services/story_service.py#L131) | W | ST |
| DELETE | `/api/v1/projects/{project_id}/characters/{item_id}` | [story.delete_item](../../../../backend/app/api/v1/story.py#L75) → [story_service.delete_owned](../../../../backend/app/services/story_service.py#L244) | W | ST |
| GET | `/api/v1/projects/{project_id}/characters/{item_id}` | [story.get_item](../../../../backend/app/api/v1/story.py#L56) → [series_read.read_book_settings](../../../../backend/app/services/series_read.py#L29) | R | ST |
| PATCH | `/api/v1/projects/{project_id}/characters/{item_id}` | [story.update_item](../../../../backend/app/api/v1/story.py#L62) → [story_service.update_owned](../../../../backend/app/services/story_service.py#L158)、[series_read.read_book_settings](../../../../backend/app/services/series_read.py#L29) | W | ST |
| GET | `/api/v1/projects/{project_id}/outlines` | [story.list_items](../../../../backend/app/api/v1/story.py#L46) → [story_service.list_owned](../../../../backend/app/services/story_service.py#L56) | R | ST |
| POST | `/api/v1/projects/{project_id}/outlines` | [story.create_item](../../../../backend/app/api/v1/story.py#L52) → [story_service.create_owned](../../../../backend/app/services/story_service.py#L131) | W | ST |
| DELETE | `/api/v1/projects/{project_id}/outlines/{item_id}` | [story.delete_item](../../../../backend/app/api/v1/story.py#L75) → [story_service.delete_owned](../../../../backend/app/services/story_service.py#L244) | W | ST |
| GET | `/api/v1/projects/{project_id}/outlines/{item_id}` | [story.get_item](../../../../backend/app/api/v1/story.py#L56) → [story_service.get_collaborative](../../../../backend/app/services/story_service.py#L118) | R | ST |
| PATCH | `/api/v1/projects/{project_id}/outlines/{item_id}` | [story.update_item](../../../../backend/app/api/v1/story.py#L62) → [story_service.update_owned](../../../../backend/app/services/story_service.py#L158) | W | ST |
| GET | `/api/v1/projects/{project_id}/world` | [story.list_world](../../../../backend/app/api/v1/story.py#L38) → [series_read.read_book_settings](../../../../backend/app/services/series_read.py#L29) | R | ST |
| POST | `/api/v1/projects/{project_id}/world` | [story.create_item](../../../../backend/app/api/v1/story.py#L52) → [story_service.create_owned](../../../../backend/app/services/story_service.py#L131) | W | ST |
| DELETE | `/api/v1/projects/{project_id}/world/{item_id}` | [story.delete_item](../../../../backend/app/api/v1/story.py#L75) → [story_service.delete_owned](../../../../backend/app/services/story_service.py#L244) | W | ST |
| GET | `/api/v1/projects/{project_id}/world/{item_id}` | [story.get_item](../../../../backend/app/api/v1/story.py#L56) → [series_read.read_book_settings](../../../../backend/app/services/series_read.py#L29) | R | ST |
| PATCH | `/api/v1/projects/{project_id}/world/{item_id}` | [story.update_item](../../../../backend/app/api/v1/story.py#L62) → [story_service.update_owned](../../../../backend/app/services/story_service.py#L158)、[series_read.read_book_settings](../../../../backend/app/services/series_read.py#L29) | W | ST |

### foreshadows（11）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/foreshadow-reminders` | [foreshadows.chapter_foreshadow_reminders](../../../../backend/app/api/v1/foreshadows.py#L103) → [foreshadow_service.chapter_reminders](../../../../backend/app/services/foreshadow_service.py#L589) | R | FS |
| GET | `/api/v1/projects/{project_id}/foreshadows` | [foreshadows.list_foreshadows](../../../../backend/app/api/v1/foreshadows.py#L24) → [foreshadow_service.list_foreshadows](../../../../backend/app/services/foreshadow_service.py#L296) | R | FS |
| POST | `/api/v1/projects/{project_id}/foreshadows` | [foreshadows.create_foreshadow](../../../../backend/app/api/v1/foreshadows.py#L48) → [foreshadow_service.create_foreshadow](../../../../backend/app/services/foreshadow_service.py#L353) | W | FS |
| GET | `/api/v1/projects/{project_id}/foreshadows/stats` | [foreshadows.foreshadow_stats](../../../../backend/app/api/v1/foreshadows.py#L54) → [foreshadow_service.stats](../../../../backend/app/services/foreshadow_service.py#L457) | R | FS |
| DELETE | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | [foreshadows.delete_foreshadow](../../../../backend/app/api/v1/foreshadows.py#L71) → [foreshadow_service.delete_foreshadow](../../../../backend/app/services/foreshadow_service.py#L391) | W | FS |
| GET | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | [foreshadows.get_foreshadow](../../../../backend/app/api/v1/foreshadows.py#L59) → [foreshadow_service.get_foreshadow](../../../../backend/app/services/foreshadow_service.py#L339) | R | FS |
| PATCH | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | [foreshadows.update_foreshadow](../../../../backend/app/api/v1/foreshadows.py#L64) → [foreshadow_service.update_foreshadow](../../../../backend/app/services/foreshadow_service.py#L363) | W | FS |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/abandon` | [foreshadows.abandon_foreshadow](../../../../backend/app/api/v1/foreshadows.py#L91) → [foreshadow_service.abandon](../../../../backend/app/services/foreshadow_service.py#L426) | W | FS |
| GET | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/history` | [foreshadows.foreshadow_history](../../../../backend/app/api/v1/foreshadows.py#L96) → [foreshadow_service.list_history](../../../../backend/app/services/foreshadow_service.py#L436) | R | FS |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/plant` | [foreshadows.plant_foreshadow](../../../../backend/app/api/v1/foreshadows.py#L77) → [foreshadow_service.plant](../../../../backend/app/services/foreshadow_service.py#L400) | W | FS |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/resolve` | [foreshadows.resolve_foreshadow](../../../../backend/app/api/v1/foreshadows.py#L84) → [foreshadow_service.resolve](../../../../backend/app/services/foreshadow_service.py#L413) | W | FS |

### careers（14）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/careers` | [careers.list_careers](../../../../backend/app/api/v1/careers.py#L28) → [career_service.list_careers](../../../../backend/app/services/career_service.py#L289) | R | CA |
| POST | `/api/v1/projects/{project_id}/careers` | [careers.create_career](../../../../backend/app/api/v1/careers.py#L33) → [career_service.create_career](../../../../backend/app/services/career_service.py#L305) | W | CA |
| POST | `/api/v1/projects/{project_id}/careers/batch` | [careers.create_careers](../../../../backend/app/api/v1/careers.py#L38) → [career_service.create_careers](../../../../backend/app/services/career_service.py#L319) | 旧确认入口 | CA |
| POST | `/api/v1/projects/{project_id}/careers/generate` | [careers.generate_careers](../../../../backend/app/api/v1/careers.py#L43) → [task_service.create_task](../../../../backend/app/services/task_service.py#L210) | AI/E | 待 AI/日志代理核验 |
| DELETE | `/api/v1/projects/{project_id}/careers/{career_id}` | [careers.delete_career](../../../../backend/app/api/v1/careers.py#L65) → [career_service.delete_career](../../../../backend/app/services/career_service.py#L425) | W | CA |
| GET | `/api/v1/projects/{project_id}/careers/{career_id}` | [careers.get_career](../../../../backend/app/api/v1/careers.py#L55) → [career_service.get_career](../../../../backend/app/services/career_service.py#L294) | R | CA |
| PATCH | `/api/v1/projects/{project_id}/careers/{career_id}` | [careers.update_career](../../../../backend/app/api/v1/careers.py#L60) → [career_service.update_career](../../../../backend/app/services/career_service.py#L327) | W | CA |
| GET | `/api/v1/projects/{project_id}/careers/{career_id}/holders` | [careers.list_holders](../../../../backend/app/api/v1/careers.py#L71) → [career_service.list_holders](../../../../backend/app/services/career_service.py#L446) | R | CA |
| GET | `/api/v1/projects/{project_id}/character-careers` | [careers.list_project_assignments](../../../../backend/app/api/v1/careers.py#L76) → [career_service.list_project_assignments](../../../../backend/app/services/career_service.py#L926) | R | CA |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/career-history` | [careers.list_history](../../../../backend/app/api/v1/careers.py#L132) → [career_service.list_history](../../../../backend/app/services/career_service.py#L1077) | R | CA |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/careers` | [careers.list_assignments](../../../../backend/app/api/v1/careers.py#L81) → [career_service.list_assignments](../../../../backend/app/services/career_service.py#L914) | R | CA |
| POST | `/api/v1/projects/{project_id}/characters/{character_id}/careers` | [careers.assign](../../../../backend/app/api/v1/careers.py#L86) → [career_service.assign](../../../../backend/app/services/career_service.py#L944) | W | CA |
| DELETE | `/api/v1/projects/{project_id}/characters/{character_id}/careers/{assignment_id}` | [careers.remove_assignment](../../../../backend/app/api/v1/careers.py#L100) → [career_service.remove_assignment](../../../../backend/app/services/career_service.py#L1011) | W | CA |
| PATCH | `/api/v1/projects/{project_id}/characters/{character_id}/careers/{assignment_id}` | [careers.update_assignment](../../../../backend/app/api/v1/careers.py#L93) → [career_service.update_assignment](../../../../backend/app/services/career_service.py#L977) | W | CA |

### organizations（13）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/memberships` | [organizations.character_memberships](../../../../backend/app/api/v1/organizations.py#L139) → [organization_service.character_memberships](../../../../backend/app/services/organization_service.py#L677) | R | OR |
| GET | `/api/v1/projects/{project_id}/organizations` | [organizations.list_orgs](../../../../backend/app/api/v1/organizations.py#L27) → [organization_service.list_orgs](../../../../backend/app/services/organization_service.py#L270) | R | OR |
| POST | `/api/v1/projects/{project_id}/organizations` | [organizations.create_org](../../../../backend/app/api/v1/organizations.py#L32) → [organization_service.create_org](../../../../backend/app/services/organization_service.py#L293) | W | OR |
| POST | `/api/v1/projects/{project_id}/organizations/generate` | [organizations.generate_org](../../../../backend/app/api/v1/organizations.py#L46) → [task_service.create_task](../../../../backend/app/services/task_service.py#L210) | AI/E | 待 AI/日志代理核验 |
| POST | `/api/v1/projects/{project_id}/organizations/with-members` | [organizations.create_with_members](../../../../backend/app/api/v1/organizations.py#L39) → [organization_service.create_with_members](../../../../backend/app/services/organization_service.py#L748) | 旧确认入口 | OR |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}` | [organizations.delete_org](../../../../backend/app/api/v1/organizations.py#L70) → [organization_service.delete_org](../../../../backend/app/services/organization_service.py#L343) | W | OR |
| GET | `/api/v1/projects/{project_id}/organizations/{org_id}` | [organizations.get_org](../../../../backend/app/api/v1/organizations.py#L58) → [organization_service.get_org](../../../../backend/app/services/organization_service.py#L282) | R | OR |
| PATCH | `/api/v1/projects/{project_id}/organizations/{org_id}` | [organizations.update_org](../../../../backend/app/api/v1/organizations.py#L63) → [organization_service.update_org](../../../../backend/app/services/organization_service.py#L308) | W | OR |
| GET | `/api/v1/projects/{project_id}/organizations/{org_id}/members` | [organizations.list_members](../../../../backend/app/api/v1/organizations.py#L76) → [organization_service.list_members](../../../../backend/app/services/organization_service.py#L471) | R | OR |
| POST | `/api/v1/projects/{project_id}/organizations/{org_id}/members` | [organizations.add_member](../../../../backend/app/api/v1/organizations.py#L81) → [organization_service.add_member](../../../../backend/app/services/organization_service.py#L483) | W | OR |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}/members/{member_id}` | [organizations.remove_member](../../../../backend/app/api/v1/organizations.py#L93) → [organization_service.remove_member](../../../../backend/app/services/organization_service.py#L597) | W | OR |
| PATCH | `/api/v1/projects/{project_id}/organizations/{org_id}/members/{member_id}` | [organizations.update_member](../../../../backend/app/api/v1/organizations.py#L86) → [organization_service.update_member](../../../../backend/app/services/organization_service.py#L510) | W | OR |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}/membership-records/{character_id}` | [organizations.delete_membership_record](../../../../backend/app/api/v1/organizations.py#L125) → [organization_service.delete_membership_record](../../../../backend/app/services/organization_service.py#L694) | W | OR |

### relationships（12）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/relationship-graph` | [relationships.graph](../../../../backend/app/api/v1/relationships.py#L107) → [relationship_service.graph](../../../../backend/app/services/relationship_service.py#L1034) | R | RL |
| GET | `/api/v1/projects/{project_id}/relationship-history` | [relationships.list_pair_history](../../../../backend/app/api/v1/relationships.py#L96) → [relationship_service.list_pair_history](../../../../backend/app/services/relationship_service.py#L948) | R | RL |
| GET | `/api/v1/projects/{project_id}/relationship-types` | [relationships.list_types](../../../../backend/app/api/v1/relationships.py#L25) → [relationship_service.list_types](../../../../backend/app/services/relationship_service.py#L195) | R | RL |
| POST | `/api/v1/projects/{project_id}/relationship-types` | [relationships.create_type](../../../../backend/app/api/v1/relationships.py#L30) → [relationship_service.create_type](../../../../backend/app/services/relationship_service.py#L211) | W | RL |
| DELETE | `/api/v1/projects/{project_id}/relationship-types/{type_id}` | [relationships.delete_type](../../../../backend/app/api/v1/relationships.py#L37) → [relationship_service.delete_type](../../../../backend/app/services/relationship_service.py#L236) | W | RL |
| GET | `/api/v1/projects/{project_id}/relationships` | [relationships.list_relationships](../../../../backend/app/api/v1/relationships.py#L43) → [relationship_service.list_relationships](../../../../backend/app/services/relationship_service.py#L779) | R | RL |
| POST | `/api/v1/projects/{project_id}/relationships` | [relationships.create_relationship](../../../../backend/app/api/v1/relationships.py#L50) → [relationship_service.create_relationship](../../../../backend/app/services/relationship_service.py#L822) | W | RL |
| POST | `/api/v1/projects/{project_id}/relationships/batch` | [relationships.create_relationships](../../../../backend/app/api/v1/relationships.py#L57) → [relationship_service.create_relationships](../../../../backend/app/services/relationship_service.py#L840) | 旧确认入口 | RL |
| POST | `/api/v1/projects/{project_id}/relationships/generate` | [relationships.generate_relationships](../../../../backend/app/api/v1/relationships.py#L64) → [task_service.create_task](../../../../backend/app/services/task_service.py#L210) | AI/E | 待 AI/日志代理核验 |
| DELETE | `/api/v1/projects/{project_id}/relationships/{rel_id}` | [relationships.delete_relationship](../../../../backend/app/api/v1/relationships.py#L79) → [relationship_service.delete_relationship](../../../../backend/app/services/relationship_service.py#L893) | W | RL |
| PATCH | `/api/v1/projects/{project_id}/relationships/{rel_id}` | [relationships.update_relationship](../../../../backend/app/api/v1/relationships.py#L72) → [relationship_service.update_relationship](../../../../backend/app/services/relationship_service.py#L854) | W | RL |
| GET | `/api/v1/projects/{project_id}/relationships/{rel_id}/history` | [relationships.list_history](../../../../backend/app/api/v1/relationships.py#L91) → [relationship_service.list_history](../../../../backend/app/services/relationship_service.py#L936) | R | RL |

### setting_fields（16）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/setting-entries` | [setting_fields.entries](../../../../backend/app/api/v1/setting_fields.py#L91) → [setting_catalog_service.list_entries](../../../../backend/app/services/setting_catalog_service.py#L146) | R | SF |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/ai-settings` | [setting_fields.ai_settings](../../../../backend/app/api/v1/setting_fields.py#L106) → [setting_ai_visibility.read_settings](../../../../backend/app/services/setting_ai_visibility.py#L177) | 策略进行中 | 待可见性代理核验 |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/ai-settings` | [setting_fields.update_ai_settings](../../../../backend/app/api/v1/setting_fields.py#L111) → [setting_ai_visibility.write_settings](../../../../backend/app/services/setting_ai_visibility.py#L270) | 策略进行中 | 待可见性代理核验 |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields` | [setting_fields.fields](../../../../backend/app/api/v1/setting_fields.py#L118) → [setting_field_service.read_entry_fields](../../../../backend/app/services/setting_field_service.py#L1177) | R | SF |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields/{definition_id}/baseline` | [setting_fields.baseline](../../../../backend/app/api/v1/setting_fields.py#L136) → [setting_field_service.write_baseline](../../../../backend/app/services/setting_field_service.py#L804) | W | SF |
| POST | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields/{definition_id}/progressions` | [setting_fields.progression](../../../../backend/app/api/v1/setting_fields.py#L153) → [setting_field_service.create_field_progression](../../../../backend/app/services/setting_field_service.py#L833) | W | SF |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/legacy-fields/{field_id}/ai-visibility` | [setting_fields.legacy_ai_visibility](../../../../backend/app/api/v1/setting_fields.py#L181) → [setting_field_service.write_legacy_visibility](../../../../backend/app/services/setting_field_service.py#L950) | 策略进行中 | 待可见性代理核验 |
| POST | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/legacy-fields/{field_id}/binding` | [setting_fields.binding](../../../../backend/app/api/v1/setting_fields.py#L166) → [setting_field_service.bind_legacy](../../../../backend/app/services/setting_field_service.py#L879) | 策略进行中 | 待可见性代理核验 |
| GET | `/api/v1/projects/{project_id}/setting-field-definitions` | [setting_fields.definitions](../../../../backend/app/api/v1/setting_fields.py#L21) → [setting_field_service.list_definitions](../../../../backend/app/services/setting_field_service.py#L173) | R | SF |
| POST | `/api/v1/projects/{project_id}/setting-field-definitions` | [setting_fields.create_definition](../../../../backend/app/api/v1/setting_fields.py#L34) → [setting_field_service.save_definition](../../../../backend/app/services/setting_field_service.py#L268) | W/策略进行中 | SF |
| DELETE | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | [setting_fields.delete_definition](../../../../backend/app/api/v1/setting_fields.py#L49) → [setting_field_service.delete_definition](../../../../backend/app/services/setting_field_service.py#L582) | W | SF |
| GET | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | [setting_fields.definition](../../../../backend/app/api/v1/setting_fields.py#L39) → [setting_field_service.read_definition](../../../../backend/app/services/setting_field_service.py#L200) | R | SF |
| PATCH | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | [setting_fields.update_definition](../../../../backend/app/api/v1/setting_fields.py#L44) → [setting_field_service.save_definition](../../../../backend/app/services/setting_field_service.py#L268) | W/策略进行中 | SF |
| POST | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options` | [setting_fields.create_option](../../../../backend/app/api/v1/setting_fields.py#L60) → [setting_field_service.save_option](../../../../backend/app/services/setting_field_service.py#L597) | W | SF |
| DELETE | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options/{option_id}` | [setting_fields.delete_option](../../../../backend/app/api/v1/setting_fields.py#L77) → [setting_field_service.delete_option](../../../../backend/app/services/setting_field_service.py#L646) | W | SF |
| PATCH | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options/{option_id}` | [setting_fields.update_option](../../../../backend/app/api/v1/setting_fields.py#L65) → [setting_field_service.save_option](../../../../backend/app/services/setting_field_service.py#L597) | W | SF |

### setting_categories（10）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/setting-categories` | [setting_categories.categories](../../../../backend/app/api/v1/setting_categories.py#L30) → [setting_category_service.list_categories](../../../../backend/app/services/setting_category_service.py#L74) | R | SC |
| POST | `/api/v1/projects/{project_id}/setting-categories` | [setting_categories.create_category](../../../../backend/app/api/v1/setting_categories.py#L46) → [setting_category_service.save_category](../../../../backend/app/services/setting_category_service.py#L173) | W | SC |
| POST | `/api/v1/projects/{project_id}/setting-categories/preview` | [setting_categories.preview](../../../../backend/app/api/v1/setting_categories.py#L52) → [setting_category_query_service.query_category_entries](../../../../backend/app/services/setting_category_query_service.py#L170) | R | SC |
| DELETE | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | [setting_categories.delete_category](../../../../backend/app/api/v1/setting_categories.py#L75) → [setting_category_service.delete_category](../../../../backend/app/services/setting_category_service.py#L190) | W | SC |
| GET | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | [setting_categories.category](../../../../backend/app/api/v1/setting_categories.py#L65) → [setting_category_service.read_category](../../../../backend/app/services/setting_category_service.py#L117) | R | SC |
| PATCH | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | [setting_categories.update_category](../../../../backend/app/api/v1/setting_categories.py#L70) → [setting_category_service.save_category](../../../../backend/app/services/setting_category_service.py#L173) | W | SC |
| GET | `/api/v1/projects/{project_id}/setting-categories/{category_id}/entries` | [setting_categories.entries](../../../../backend/app/api/v1/setting_categories.py#L88) → [setting_category_query_service.query_category_entries](../../../../backend/app/services/setting_category_query_service.py#L170) | R | SC |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/tags` | [setting_categories.tags](../../../../backend/app/api/v1/setting_categories.py#L122) → [setting_category_service.read_tags](../../../../backend/app/services/setting_category_service.py#L218) | R | SC |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/tags` | [setting_categories.update_tags](../../../../backend/app/api/v1/setting_categories.py#L129) → [setting_category_service.save_tags](../../../../backend/app/services/setting_category_service.py#L256) | W | SC |
| GET | `/api/v1/projects/{project_id}/setting-entry-tags` | [setting_categories.suggestions](../../../../backend/app/api/v1/setting_categories.py#L136) → [setting_category_service.suggest_tags](../../../../backend/app/services/setting_category_service.py#L274) | R | SC |

### progressions（14）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/setting-timelines` | [progressions.list_timelines](../../../../backend/app/api/v1/progressions.py#L35) → [setting_progression_service.list_timelines](../../../../backend/app/services/setting_progression_service.py#L683) | R | TL |
| POST | `/api/v1/projects/{project_id}/setting-timelines` | [progressions.create_timeline](../../../../backend/app/api/v1/progressions.py#L61) → [setting_progression_service.create_timeline](../../../../backend/app/services/setting_progression_service.py#L726) | W | TL |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/baseline` | [progressions.read_baseline](../../../../backend/app/api/v1/progressions.py#L66) → [setting_progression_service.read_baseline](../../../../backend/app/services/setting_progression_service.py#L772) | R | TL |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/baseline` | [progressions.update_baseline](../../../../backend/app/api/v1/progressions.py#L71) → [setting_progression_service.update_baseline](../../../../backend/app/services/setting_progression_service.py#L983) | W | TL |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/effective` | [progressions.read_effective](../../../../backend/app/api/v1/progressions.py#L146) → [setting_progression_service.read_effective](../../../../backend/app/services/setting_progression_service.py#L1421) | R | TL |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions` | [progressions.list_progressions](../../../../backend/app/api/v1/progressions.py#L78) → [setting_progression_service.list_progressions](../../../../backend/app/services/setting_progression_service.py#L1213) | R | TL |
| POST | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions` | [progressions.create_progression](../../../../backend/app/api/v1/progressions.py#L103) → [setting_progression_service.create_progression](../../../../backend/app/services/setting_progression_service.py#L1279) | W | TL |
| DELETE | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | [progressions.delete_progression](../../../../backend/app/api/v1/progressions.py#L124) → [setting_progression_service.delete_progression](../../../../backend/app/services/setting_progression_service.py#L1394) | W | TL |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | [progressions.read_progression](../../../../backend/app/api/v1/progressions.py#L110) → [setting_progression_service.read_progression](../../../../backend/app/services/setting_progression_service.py#L1295) | R | TL |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | [progressions.update_progression](../../../../backend/app/api/v1/progressions.py#L117) → [setting_progression_service.update_progression](../../../../backend/app/services/setting_progression_service.py#L1317) | W | TL |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields` | [progressions.list_text_fields](../../../../backend/app/api/v1/progressions.py#L163) → [setting_progression_service.list_text_fields](../../../../backend/app/services/setting_progression_service.py#L1501) | R | TL |
| POST | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields` | [progressions.create_text_field](../../../../backend/app/api/v1/progressions.py#L168) → [setting_progression_service.create_text_field](../../../../backend/app/services/setting_progression_service.py#L1522) | W | TL |
| DELETE | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields/{field_id}` | [progressions.delete_text_field](../../../../backend/app/api/v1/progressions.py#L182) → [setting_progression_service.delete_text_field](../../../../backend/app/services/setting_progression_service.py#L1565) | W | TL |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields/{field_id}` | [progressions.update_text_field](../../../../backend/app/api/v1/progressions.py#L175) → [setting_progression_service.update_text_field](../../../../backend/app/services/setting_progression_service.py#L1540) | W | TL |

### project_cover（5）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| DELETE | `/api/v1/projects/{project_id}/cover` | [project_cover.delete_cover](../../../../backend/app/api/v1/project_cover.py#L36) → [cover_service.delete_cover](../../../../backend/app/services/cover_service.py#L59) | W | P |
| GET | `/api/v1/projects/{project_id}/cover` | [project_cover.get_cover](../../../../backend/app/api/v1/project_cover.py#L15) → [cover_service.get_manual_cover](../../../../backend/app/services/cover_service.py#L27) | R | P |
| PUT | `/api/v1/projects/{project_id}/cover` | [project_cover.put_cover](../../../../backend/app/api/v1/project_cover.py#L25) → [cover_service.put_cover](../../../../backend/app/services/cover_service.py#L39) | W | P |
| POST | `/api/v1/projects/{project_id}/cover/generate` | [project_cover.generate_cover](../../../../backend/app/api/v1/project_cover.py#L46) → [cover_service.generate_cover](../../../../backend/app/services/cover_service.py#L76) | AI/E | 待 AI/日志代理核验 |
| GET | `/api/v1/projects/{project_id}/cover/prompt` | [project_cover.default_prompt](../../../../backend/app/api/v1/project_cover.py#L41) → [cover_service.default_prompt](../../../../backend/app/services/cover_service.py#L68) | AI/E | 待 AI/日志代理核验 |

### reader（3）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| GET | `/api/v1/projects/{project_id}/reader/chapters/{chapter_id}` | [reader.reader_chapter](../../../../backend/app/api/v1/reader.py#L22) → [reader_service.chapter](../../../../backend/app/services/reader_service.py#L61) | R | C |
| GET | `/api/v1/projects/{project_id}/reader/toc` | [reader.reader_toc](../../../../backend/app/api/v1/reader.py#L17) → [reader_service.toc](../../../../backend/app/services/reader_service.py#L39) | R | C |
| PUT | `/api/v1/projects/{project_id}/reading-progress` | [reader.save_reading_progress](../../../../backend/app/api/v1/reader.py#L11) → [reader_service.save_progress](../../../../backend/app/services/reader_service.py#L16) | 作者 | C |

### cards（3）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| POST | `/api/v1/projects/{project_id}/cards/export` | [cards.export_cards](../../../../backend/app/api/v1/cards.py#L15) → [card_service.export_cards](../../../../backend/app/services/card_service.py#L108) | 卡片进行中 | 待卡片代理 |
| POST | `/api/v1/projects/{project_id}/cards/import` | [cards.import_cards](../../../../backend/app/api/v1/cards.py#L25) → [card_service.import_cards](../../../../backend/app/services/card_service.py#L765) | 卡片进行中 | 待卡片代理 |
| POST | `/api/v1/projects/{project_id}/cards/import/preview` | [cards.preview_import](../../../../backend/app/api/v1/cards.py#L20) → [card_service.preview_cards](../../../../backend/app/services/card_service.py#L651) | 卡片进行中 | 待卡片代理 |

### character_actions（3）

| 方法 | 路径 | 路由 → 实际 service | 能力/状态 | 父子限定 |
| --- | --- | --- | --- | --- |
| POST | `/api/v1/projects/{project_id}/characters/generate` | [character_actions.generate_character](../../../../backend/app/api/v1/character_actions.py#L24) → [task_service.create_task](../../../../backend/app/services/task_service.py#L210) | AI/E | 待 AI/日志代理核验 |
| POST | `/api/v1/projects/{project_id}/characters/with-relations` | [character_actions.create_with_relations](../../../../backend/app/api/v1/character_actions.py#L17) → [relationship_service.create_character_with_relations](../../../../backend/app/services/relationship_service.py#L975) | 旧确认入口 | RL |
| POST | `/api/v1/projects/{project_id}/characters/{character_id}/parse-relations` | [character_actions.parse_relations](../../../../backend/app/api/v1/character_actions.py#L36) → [task_service.create_task](../../../../backend/app/services/task_service.py#L210) | AI/E | 待 AI/日志代理核验 |
