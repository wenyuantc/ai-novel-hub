# 阶段 D：章节、场景、版本、阅读与导出

## 迁移的调用链

| 调用 | 人工能力与安全边界 |
| --- | --- |
| 项目下章节列表、详情、创建、由大纲建章、编辑、删除 | 列表与详情 `content.read`；人工写 `content.edit`，锁项目并重读后锁定属于该项目的章节。Agent 的原 `list_chapters` / `update_chapter` 调用保留严格行为，HTTP 显式走人工路径。 |
| 章节版本列表、详情、标星/备注、删除、恢复与提取及其预览 | 阅读/预览 `content.read`；修改 `content.edit`。恢复与提取先取发送写门禁，再锁父项目和章节；版本的日志引用仅对真实日志 actor 且当前为作者或 accepted 成员可见。 |
| 场景列表、详情、创建、更新、排序、拆分、合并、删除、位置定位 | 阅读与纯预览 `content.read`；写操作 `content.edit` 且锁后复查。拆分、合并涉及富文档策略时保留发送写门禁。 |
| 富文档读取、保存、区块转正文及标记目录、重绑、删除 | 阅读 `content.read`；保存、转换及标记写入 `content.edit`，保持发送写门禁→项目→章节的顺序及版本校验。 |
| 阅读目录、阅读器章节 | `content.read`；章节风格按 actor 单独投影；阅读进度写入保持严格作者。 |
| 作品 TXT / Markdown / EPUB 导出 | 站内入口显式 `export`，允许作者及 accepted 协作者；默认 EPUB 署作品作者，显式署名可覆盖；开放 API 仍调用原严格作者导出。 |
| 封面读、上传、删除 | 站内人工读 `content.read`、写 `content.edit`；开放 API 原 helper 和封面 AI 提示/生成保持旧限制。 |

章节列表、详情、创建、更新、版本恢复及阅读器响应在独立 Pydantic 模型中遮蔽不可见私人 `style_id`，不修改 ORM 数据；内置风格仍可见，原管理员可见行为保留。协作者显式提交任何章节 `style_id`（包括 null）仍被拒绝。

`get_manual_chapter(db, user_id, project_id, chapter_id, capability, *, for_update=False, visibility_write=False)` 仅供人工路径。`content.edit` 必须持锁；全程禁止自动 flush，锁后刷新授权、项目及章节。保留 session 里待写的普通章节字段；若待写正文依据的内容版本已变，返回 `stale_scene_revision`，避免覆盖新正文。身份、生成状态和内容版本不允许以待写值覆盖锁后数据库事实。

## 保持原限制或需其他阶段处理

- 章节与场景 `setting-context` 的人工读取已接入 `content.read`；出场人物 HTTP 调用 `cast_service` 的人工模式，由并行 story 任务实现与测试。
- `generation-logs`、仅有章节 ID 的生成/润色/改写、改写采纳、封面提示/图片生成、AI 与任务仍保留原严格作者语义，待阶段 E 逐条迁移；人工 helper 未替换这些 AI helper。
- 章节 `/rewrite/apply` 当前返回原始 `ChapterOut`，仅因严格作者语义而可保留现状；阶段 E 如向协作者开放，必须同时按真实 actor 脱敏其 `style_id` 并校验 AI 和日志权限。
- 项目备份、阅读进度和回收未扩权；编辑者不可取消作者任务。测试中真实作者任务运行时，协作者回收与取消请求均被拒绝，任务未中断且继续完成。
- 本文件只记录所分配的内容切片，其他 story、规划、知识库、系列和后台任务仍需各自按阶段 A 清单验证；阶段 D/E/F 兼容单元尚未完成，不能独立部署。

## 验证概况

新增 `test_collaboration_content_access.py` 覆盖 HTTP 身份矩阵、个人/全局风格、跨作品章节与场景 ID、日志引用、版本恢复、标记与富文档、三种导出和 EPUB 署名、协作者开放 API key 不能读取共享作品、运行中的作者任务和真实发送 lease；新增 `test_collaboration_content_postgresql.py` 用独立 schema 验证六类内容写在项目父行上等待撤权、写先提交后的持久结果和最新内容版本冲突。

此前发现审计上下文授权前可能自动 flush，以及并行规划改动导致纯管理员的 `analysis-status` 返回 200。对应负责人修复后，章节真实服务反例和原管理员反例分别通过；下面的组合复测包含这两处修复。

在 `backend/` 下执行：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test' uv run pytest -q tests/test_collaboration_content_access.py tests/test_collaboration_content_postgresql.py
# 22 passed in 51.92s；含 10 个隔离 PostgreSQL 用例，启动后增加的日志专属路径断言已单独通过

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_scenes.py tests/test_rich_document_api.py tests/test_rich_document_dispatch.py tests/test_rich_document_history.py tests/test_marker_lifecycle.py tests/test_reader.py tests/test_project_cover.py tests/test_export.py tests/test_epub.py tests/test_chapter_versions.py tests/test_open_api.py tests/test_admin_projects.py tests/test_collaboration_log_privacy.py tests/test_cast_context.py
# 151 passed in 147.27s；开始后收紧了 EPUB 署名，相关导出单独重测如下

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_content_access.py::test_chapter_reader_version_and_export_access_matrix tests/test_epub.py tests/test_export.py
# 10 passed in 7.70s

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_cast_context.py tests/test_collaboration_story_access.py
# 12 passed in 12.91s

uv run ruff check .
# All checks passed!
```

还执行了 `git diff --check`，无空白错误。未执行完整后端或前端测试集。

## 章节删除与历史隔离的独立复核

- `chapter_service.manual_chapter_views` 批量确认 `outline_id`、`plan_id` 的作品及双向绑定后才投影；阅读目录 JOIN 和 EPUB 卷名查询也限制同书。改挂大纲前必须先验证旧细纲与本章的双向关系，损坏时返回匿名 `chapter_plan_invalid`，不能修改他书细纲。
- 普通章节删除通过 `chapter_delete_guard.ensure_chapter_delete_isolation` 递归反查 ORM metadata 的外键，并检查无 FK 的自动记忆检索引用及审核收据；跨书、归属不明或新增未审核外键返回匿名 `chapter_reference_invalid`。细纲同步的直接删章调用同一外键预检。合法同书引用不应妨碍原有删除；章重排还要保护相关他书记忆和索引。
- 纯协作者直接删除章节时，先取得发送写门禁再锁父项目并重读身份，保护其他人的排队、运行、暂停／待审核和批量任务；范围不可确认时匿名 `chapter_in_use`。作者和具有原有项目读写管理能力者保留原有任务策略。真实生成中、同步 HTTP 发送门禁、权限来源变化和 PostgreSQL 锁后重读均有回归。
- 结构历史详情、恢复预览及实际恢复先批量校验场景、规划单元、POV、标签、各类实体和关系时间线的归属，以及同景富文档区块／标记的持久身份。历史损坏、跨书、缺失或不能确认归属均匿名 `scene_reference_invalid`；绑定代次改变仍是 `scene_reference_conflict`。严格恢复预检发生在基线及场景修改前，即使调用方捕获异常后提交也不能保存部分恢复；可确认的同书历史保持原样读取和恢复。

**已知边界**：发送门禁在模型 HTTP 响应头到达后释放，不持续到响应体／SSE 完毕；同步 AI 出站若无任务记录，此后删除不受该门禁保护。外键图来自 ORM metadata，不扫描数据库中未映射的物理外键。任务或历史引用因记录缺失而无法归属时按失败关闭处理，可能阻止操作直至数据修复。本切片发现细纲同步间接删章还需具备与直接删除相同的其他人任务及发送门禁保护，由规划复核负责修复和验证；不能据此宣称所有 Gate D 已完成。

以下在 `backend/` 执行，PG URL 是本机隔离专用测试库；所有 PG 用例使用临时 schema：

```sh
LC_ALL=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test' uv run pytest -q tests/test_collaboration_content_review.py tests/test_collaboration_chapter_fk_guard.py tests/test_collaboration_content_review_postgresql.py tests/test_collaboration_content_access.py tests/test_collaboration_content_postgresql.py --tb=short
# 67 passed；这是历史快照后续加固前的章节、隔离 FK 和真实 PG 组合基线。

LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_outline_pro.py tests/test_reader.py tests/test_epub.py tests/test_planning_lifecycle.py tests/test_project_cover.py --tb=short
# 71 passed；这是章节删除预检后的旧功能回归。

LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_collaboration_version_snapshot_isolation.py --tb=short
# 15 passed，含九类跨书规划／场景、跨书富文档、无效字段及 401 个合法引用跨分批测试。

LC_ALL=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test' uv run pytest -q tests/test_collaboration_content_review_postgresql.py -k version_snapshot --tb=short
# 3 passed, 6 deselected；含跨书 POV、跨书区块和合法同书恢复。

LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_scene_versions.py tests/test_rich_document_history.py tests/test_rich_document_legacy_guard.py tests/test_scene_planning.py --tb=short
# 54 passed；历史身份校验最终版本。

LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_chapter_versions.py tests/test_collaboration_content_access.py --tb=short
# 28 passed；历史身份校验最终版本。

LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_collaboration_content_review.py -k nested_sqlite_visibility_timeout_restores_original
# 2 passed；嵌套发送门禁成功／失败均恢复 SQLite 连接原 timeout。
```

本次修改文件的 Ruff 全通过；最后一次全后端 `ruff check .` 时，并行任务的卡片、日志、系列、任务及可见性相关文件仍有 F401／F811／E501，已告知对应负责人，须以最终集成检查为准。Python `compileall` 检查通过；后端没有配置本次可运行的独立类型检查器。上述结果不是完整后端、前端或阶段 D/E 的总验收。

## 章节与场景设定预览补充

两个 HTTP `setting-context` 均显式走人工 `content.read`；`scene_setting_context` 和
`count_unverified_timelines` 默认仍使用原 manageable 路径，仅人工调用传 `manual=True`。
预览继续通过作品级 AI 可见范围、字段过滤和保护块处理，仅查询当前作品，且不调用模型、嵌入或知识检索。
渲染后再次确认规划、字段、可见性和章节内容版本；同时计数查询会重新确认当前 actor 的读取能力。
非成员及错章／跨书场景仍返回 404，原作者与管理员行为保留。

```sh
LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_collaboration_setting_context.py --tb=short
# 9 passed；包含角色矩阵、跨书引用和渲染后规划/可见性/内容版本变化。

LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_setting_context_preview.py tests/test_scene_progressions.py tests/test_setting_ai_context.py --tb=short
# 24 passed, 1 skipped；跳过的既有 fields_verify PostgreSQL 用例未配置其专用数据库。

LC_ALL=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_content' uv run pytest -q tests/test_collaboration_setting_context_postgresql.py --tb=short
# 2 passed；随机 schema，分别在章和景预览渲染后真实并发提交规划、可见性、内容版本变化和撤权。

LC_ALL=en_US.UTF-8 uv run ruff check app/api/v1/chapters.py app/api/v1/scenes.py app/services/planning_context_service.py app/services/setting_progression_service.py tests/test_collaboration_setting_context.py tests/test_collaboration_setting_context_postgresql.py
# All checks passed!
```
