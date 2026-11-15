# Research: A1 路由清单对账与缺口

- Query: 对实际递归 APIRoute 和 OpenAPI 逐项对照原 469 项清单、人工／规划复核及系列、私人任务、账户、管理员、开放 API，定位能力、service、父作品与 E 阶段缺口。
- Scope: internal；当前树的静态调查与无 lifespan 路由扫描。
- Date: 2026-11-10

## Findings

### 文件和基线

- `.trellis/workflow.md`、`prd.md`、`design.md`、`implement.md`：任务范围、角色合同和 A–H 门禁。`.trellis/spec/backend/index.md`、`rbac-quotas.md`：保留原管理权限、目标保护、配额与单进程假设。
- `phase-a-route-inventory.md`：基线 469 个 OpenAPI 操作，分类多数仅为候选；`phase-a-manual-route-review.md`：167 条人工专项；`phase-a-planning-route-review.md`：110 条规划／私人任务专项。两表重叠 7 条，合计覆盖 270 个不同操作；本文件逐键对齐，无未匹配项。
- `backend/tests/test_collaboration_route_inventory.py:5`：递归 `_IncludedRouter.original_router` 拼接前缀，比较 OpenAPI；测试设置 469 为下限，不能证明分类和鉴权。`backend/app/main.py:create_app`：不运行 lifespan 即可获得路由。
- `phase-d-cards.md`、`phase-d-content.md`、`phase-d-planning-preview.md`、`phase-d-visibility.md`、`phase-f-series.md`、`phase-f-series-review.md`、`phase-e-task-review.md`、`phase-e-index-core.md`、`phase-e-chapter-ai-map.md`：较新切片的实现及证据，覆盖仅限各文档写明范围。
- 外部参考：无。本次源码是准绳；文档与代码不一致按本节纠正，而非改写原复核文件。

2026-11-10 本次用临时测试值构造现有 app，不触发 lifespan、不连接数据库、不启动任务／模型；递归得到 **481 个实际方法＋路径**，OpenAPI **479 个操作／353 条路径**，OpenAPI 全被扫描捕获。旧 469 全部仍在，新增 10 个协作成员／邀请操作。实际未计入主 OpenAPI 的是 GET `/api/open/v1/openapi.json`、GET `/api/open/v1/docs`；旧记录是 472／3 个隐藏项，原因未溯源，不能继续当作当前统计。`HEAD` 管理员备份下载也是一个独立的 OpenAPI 操作。构造 app 时只读取仓库根上下文，设置测试用内存数据库与临时密钥值，不使用后端 `.env`。

### 分类含义与证据边界

- R / W：分别是人工 `content.read` / `content.edit`；作者和 accepted 成员适用其角色，原管理员依原 `projects.read/edit` 和目标保护。W 需锁后重验；管理员与协作者身份独立取并集。X：明确的项目文本／卡片导出能力，不默认扩大纯管理员权限。项目自身创建／书架、回收、备份、阅读进度和成员管理各有专用规则。
- O：严格作品作者或旧作者／既有管理员规则，具体以每行和原复核为准；M：本人私人数据且保持项目关联及当前协作身份；T：本人任务，活动任务另校验有效角色、原邀请和运行轮次。系列权限独立于单书权限。开放 API 由密钥及其小说范围决定，不因网页协作自动扩大。
- 表中“直接调用”由当前 API endpoint 源码实际调用表达式提取；服务内部依赖、动态调用与 FastAPI 权限依赖不可能由此自动穷尽。M / P 是两份逐条复核的来源及其具体行，行号反映那份文档，不是当前代码行号；其中历史备注与后续实现冲突时以下文及对应最新专项为准。非两表覆盖的条目来自本次路由源码与 service 接口静态分拣，未逐一验证每个行为或角色。表中 E／待核是待完成事项，绝非已开放／已测试结论。
- API 文件行号和直接 service 为扫描快照；并行修改仍在进行，请以函数名及最终树复核。`services.*` 前缀指 `backend/app/services/`，`api/v1` 等指 `backend/app/api/`；个别 `Manager` 为依赖注入的服务对象。没有直接调用时明确写 API 内／依赖，而不是猜测 service。

### 父资源只有子 ID 的定位

| 入口 | 实际定位／现状 | 待核验 |
| --- | --- | --- |
| `/chapters/{chapter_id}/analysis-status`、`/analyses` | `backend/app/services/analysis_service.py:539` 的 `_manual_chapter` 查 `Chapter.project_id`，再 `get_manual_chapter`；`_task_actor` 还要求本人作者／accepted 成员。 | `analysis_review` 仍在更新原 task UUID、入队时间及 grant 最终复核，见其最终证据。 |
| `/analyses/{analysis_id}` | `backend/app/services/analysis_service.py:624` 的 `get_analysis` 从 `ChapterAnalysis` 找 project，再核 `ChapterAnalysis.project_id == Chapter.project_id`；本人任务引用过滤。 | 旧任务 UUID 缺失、安全失败和响应中授权变化按最新实现与测试复验。 |
| `/analysis-changes/{change_id}/confirm\|reject\|revert` | `backend/app/services/writeback_service.py:601` 的 `get_manual_change` 先查 change → analysis → project/chapter，再锁父作品、限定相关行同书；W + M。 | 跨书脏数据、撤权交错最终综合验证。 |
| `/chapters/{chapter_id}/generate\|polish\|revise\|analyze\|rewrite`、`/chapters/{chapter_id}/context-preview` | `backend/app/services/chapter_service.py:63` 的 `get_owned_chapter` 联结 Chapter → Project；生成经 `task_service` 和 chapter handler，检索预览经 `context_builder`，现行严格作者链不能冒称已支持 editor。 | E 先用 ID 回查父书，再以真实 actor／原 grant、模型绑定和执行身份在受理、发送、保存／清理处复验；不能放宽严格 helper。 |
| `/tasks/{task_id}` 及输出／流／操作／`rich-proposals/{token}` | `backend/app/services/task_service.py:405` 的 `get_owned_task` / `:416` 的 `_lock_user_task_project` 由 task.user_id 与 task.project_id 校验；proposal 经 task UUID、run key、token 关联。 | E 还须对执行身份、入队轮次、原 grant 与每次流／写操作复验；无项目任务保留本人语义。 |
| `/book-imports/{import_id}`、`/book-imports/tasks/{task_id}/skip` | 前者 `backend/app/services/book_import/service.py:81` 的 `get_owned_import` 按记录本人；后者 `book_import.reverse.skip_paused` 按本人任务；开始导入创建新本人作品。 | 与任务引擎最新轮次隔离联测；不把个人解析 ID 当作现存作品 ID。 |
| `/inspiration/sessions/{session_id}/...` | `backend/app/services/inspiration_service.py:162` 的 `get_owned_session`；`apply` / `apply_preview` 再用请求的 project_id 严格查作者；`prepare_generate` 的 `session.project_id` 如有值可读取本人知识库。 | 需对关联作品时的模型／向量出站单独纳入 E，不能凭“私人会话”遗漏。 |
| `/collaboration/invitations/{scope}/{invitation_id}/accept\|decline` | `backend/app/services/collaboration_service.py:349` 的 `respond` 以 scope + invitation UUID + actor 查成员行，取其 project_id 或 series_id；锁父行后复核 UUID／revision。 | 成员通知仅本进程唤醒，实际执行体停止与跨进程数据库复核仍需 E 集成。 |

### 差异与风险，按优先级

1. **P0，尚未开放的 AI／任务执行与索引链**：`generation_service.validate_and_claim`、`task_service.create_task`、`task_engine/handlers/chapter.py`、`rewrite_service.prepare_rewrite_job` / `_apply`、`rich_document_section_ai.prepare_section_rewrite` / `apply_section_rewrite`、`batch_service.preview` / `validate_start`、`analysis_service.request_analyze`、`kb_service.upload_document` / `reindex_document` / `search_debug`、`memory_service.search_debug` 和 `context_builder.retrieval_preview` 需要受理、出站、回写、重试、清理逐环接线。尤其 0045 KB claim、0046 chapter claim、`project_ai_config` 和 AI dispatch 目前是独立基础切片，不是协作 AI 已开放的证明；真实 actor、grant、模型／费用来源、执行 UUID 和受限清理不得隐式替换为作者。`design.md` 还要求无作者向量授权时上传走关键词模式；当前上传链仍需 E 核验。
2. **P0，长任务与私人数据**：`task_service.get_owned_task` / `_lock_user_task_project`、`task_engine.engine._claim_and_run` / `hub` / `cancellation`、`rich_document_review.get_review` / `accept_review`、`personal_projection.project_payload`、`generation_log_service` 和 `agent/service.py` 的个人数据及原轮次不得由管理员人工权限旁路。阶段记录的任务 PostgreSQL 7 项是引擎接线前基线；最新代码须重新验证。`collaboration_service` 的通知已接线，但仅唤醒本进程订阅，不等于执行体停止或跨进程通知。
3. **P1，旧审查与当前代码偏差**：P:22 的规划 `units/context-preview` 原标 O，现 `planning.py:context_preview → planning_context_service.manual_context_preview` 是不调用模型／向量的本地 R；`phase-d-planning-preview.md` 记载 SQLite 14 项和隔离 PostgreSQL 2 项。M 中章／景 `setting-context` 的“进行中”现已接入人工 R，`phase-d-content.md` 记录 SQLite 9 项、PostgreSQL 2 项。M 中卡片“进行中”现以 `phase-d-cards.md` 为准：导出和预览 X，导入 X + W；该专项记载 SQLite 55 项及 PostgreSQL 8 项。M 的 AI 设置“策略进行中”现已有 `phase-d-visibility.md`：手动读写为 R/W，独立 PostgreSQL 4 项；但不代表 AI 发送链完成。所有数字是专项文档中的历史记录，本次未重跑。
4. **P1，人工确认和个人入口的语义缺口**：`career_service.create_careers`、`organization_service.create_with_members`、`relationship_service.create_relationships` / `create_character_with_relations` 的旧确认路径在 M 中仍用旧作者／管理员能力；是否属于 D/E 必须由主任务逐入口决定，不能按 POST 推断 editor 可用。`inspiration_service.from_project` / `apply_preview` / `apply` 仍严格作者，`prepare_generate` 可能附带项目知识库和模型／向量出站；`book_import.service.start` 会创建本人作品和 `book.import` 任务。它们原被粗分为“个人会话／导入，需复核”，不能据此排除相关 E 链。
5. **P1，系列跨书与投影**：`series_service.promote` / `use_entry` / `unuse` / `move_entry` / `impact` 的系列与全部实际受影响作品权限必须分开核验（系列作者保留原处理，非作者需对应每本书的 accepted editor）；`series_read.read_book_settings`、`series_service` 的最终投影不能泄露失权／隐藏书身份。最新 `phase-f-series-review.md` 首段记录隔离 PG 读竞争 14 项、写竞争 8 项；该文末的旧“尚未运行”段落已过期；仍不等于整体验收。
6. **P2，账户／网络出站分类**：`styles.extract_style` / `trial_style → style_service.prepare_extract/prepare_trial` 会调用模型；`ai_configs.list_models` / `test_config → ai_config_service`、`mcp_plugins.call_tool/refresh_plugin` 可能出网。它们属于用户个人资源，仍须保留端点／密钥和个人隔离，不能从旧“账户级”标签断言无外部调用。`prompt_templates.preview_template`、POST 卡片／规划预览则需按函数行为判断，不按方法推权限。
7. **P2，扫描完整性与稳定性**：10 条新邀请路由此前不在原 469 表；旧 472/3 的隐藏项与现在 481/2 不同，尚未定位哪个历史隐藏项变化。递归扫描识别静态路由而非间接 service、依赖或运行时模型请求；未来动态注册仍要重扫。返回前撤权竞争、未授权的副作用、异常旧关联和不同数据库隔离级别均需针对具体入口测试。

### 四条人工确认入口的接入清单

四条都接受现有草稿或组合内容并在本请求内写入，当前使用 `get_manageable_project(..., lock=True, permission="projects.edit")`：作者及符合原 RBAC 的管理员可用，单纯 accepted editor 不因此可写。目标合同 `prd.md` / `implement.md` D1 为普通人工内容写提供 editor `content.edit`；由主代理按具体入口确认边界并实现，不能由本对账直接视为完成。实际生成及任何模型调用仍属独立 E。

| HTTP 入口和服务签名 | 嵌套写入和必须复核的边界 |
| --- | --- |
| `POST /api/v1/projects/{project_id}/careers/batch` → `career_service.create_careers(db, user_id, project_id, data: CareerBatchCreate) -> list[CareerOut]`；`backend/app/services/career_service.py:319` | 多次 `new_career` flush 职业与阶段、`ensure_entry_timeline`；中后项的名称、阶段冲突或权限变化均须保持批次原子，已 flush 也不能留下。 |
| `POST /api/v1/projects/{project_id}/organizations/with-members` → `organization_service.create_with_members(db, user_id, project_id, data: OrganizationWithMembers) -> dict[str, Any]`；`backend/app/services/organization_service.py:748` | `new_org` 与多次 `new_member` 创建组织、成员及时间线；上级组织、角色、章／景位置必须同作品，校验 revision。成员失败应整体回滚，不能只回滚最后一个成员。 |
| `POST /api/v1/projects/{project_id}/relationships/batch` → `relationship_service.create_relationships(db, user_id, project_id, data: RelationshipBatchCreate) -> list[RelationshipOut]`；`backend/app/services/relationship_service.py:840` | 多次 `create_relation` 先按项目校验两端角色，再查全局 pair，验证项目／全局类型、剧情位置并写时间线；外书 ID 或异常旧关系载体不能从冲突差异泄漏，任一错误需整批原子回滚。 |
| `POST /api/v1/projects/{project_id}/characters/with-relations` → `relationship_service.create_character_with_relations(db, user_id, project_id, data: CharacterWithRelations) -> dict[str, Any]`；`backend/app/services/relationship_service.py:975` | 同事务写角色与设定时间线、多个关系和职业／阶段挂接；验证关联角色、职业、阶段同作品且阶段属该职业。`assign_new` 含 SAVEPOINT，后续错误不能残留外层对象。新角色可能改变 AI 上下文注入；与 `story_service.create_owned(..., manage=True)` 的 `visibility_write=True` 保持同一发送写屏障和锁序，并复核匹配候选变化、输出真实 actor 隐私投影。 |

上述关联和锁序为接入时的验证要求；没有在本次研究中执行相关改动或证明存在漏洞。

并行责任：`analysis_review` 负责分析任务引用／最终重验；`index_identity` 负责 KB、记忆、检索与共享索引调用链；`engine_fencing` 负责任务受理／领取／取消、运行身份与清理；`log_review` 负责生成日志、投影、Agent 隐私；`model_review` 负责模型绑定；主代理统一整合章节／同步 AI、角色矩阵、真实发送与回归。代理可能在本文件生成后继续修改代码；以其最终交付与重新测试为准。

### 当前 OpenAPI 全量逐操作索引

M = `phase-a-manual-route-review.md`，P = `phase-a-planning-route-review.md`；行号指对应文档。每行给出运行时 route endpoint、该 endpoint 直接调用的服务、能力和父资源／证据。**这是操作覆盖和静态分类清单，不等于全部权限已通过测试。** 本表按当前 479 个文档化操作排序；隐藏文档路由见上文。

| 方法 | 路径 | 当前 API 函数及直接调用 | 能力／状态 | 父资源与复核来源 |
| --- | --- | --- | --- | --- |
| GET | `/api/open/v1/me` | backend/app/api/open/__init__.py:45 `key_info` → `services.open_api.catalog.key_info` | 开放 API：密钥本人、自有且授权小说；只读 | 密钥身份与授权信息；不查询小说，见 open_api.catalog.key_info |
| GET | `/api/open/v1/novels` | backend/app/api/open/__init__.py:51 `list_novels` → `services.open_api.catalog.list_novels` | 开放 API：密钥本人、自有且授权小说；只读 | 按 principal.user_id 和密钥范围筛选作品，见 open_api.catalog.list_novels |
| GET | `/api/open/v1/novels/{novel_id}` | backend/app/api/open/__init__.py:66 `get_novel` → `services.open_api.catalog.get_novel` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/chapters` | backend/app/api/open/__init__.py:73 `list_chapters` → `services.open_api.catalog.list_chapters` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/chapters/{chapter_id}` | backend/app/api/open/__init__.py:94 `get_chapter` → `services.open_api.catalog.get_chapter` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/characters` | backend/app/api/open/__init__.py:166 `list_characters` → `services.open_api.entities.list_story` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/characters/{character_id}` | backend/app/api/open/__init__.py:186 `get_character` → `services.open_api.entities.get_story` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/cover` | backend/app/api/open/__init__.py:102 `get_cover` → `services.open_api.catalog.get_cover` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/export` | backend/app/api/open/__init__.py:125 `export_novel` → `services.open_api.catalog.export_novel` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/organizations` | backend/app/api/open/__init__.py:266 `list_organizations` → `services.open_api.entities.list_organizations` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/organizations/{organization_id}` | backend/app/api/open/__init__.py:284 `get_organization` → `services.open_api.entities.get_organization` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/organizations/{organization_id}/members` | backend/app/api/open/__init__.py:299 `list_members` → `services.open_api.entities.list_members` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/outlines` | backend/app/api/open/__init__.py:224 `list_outlines` → `services.open_api.entities.list_story` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/outlines/{outline_id}` | backend/app/api/open/__init__.py:241 `get_outline` → `services.open_api.entities.get_story` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/outlines/{outline_id}/plans` | backend/app/api/open/__init__.py:248 `list_plans` → `services.open_api.entities.list_plans` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/relationship-types` | backend/app/api/open/__init__.py:358 `list_relationship_types` → `services.open_api.entities.list_relationship_types` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/relationships` | backend/app/api/open/__init__.py:320 `list_relationships` → `services.open_api.entities.list_relationships` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/relationships/{relationship_id}` | backend/app/api/open/__init__.py:342 `get_relationship` → `services.open_api.entities.get_relationship` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/world` | backend/app/api/open/__init__.py:197 `list_world` → `services.open_api.entities.list_story` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/open/v1/novels/{novel_id}/world/{setting_id}` | backend/app/api/open/__init__.py:214 `get_world` → `services.open_api.entities.get_story` | 开放 API：密钥本人、自有且授权小说；只读 | novel_id 定位并按密钥约束；子项须同小说；见 open_api.catalog/entities |
| GET | `/api/v1/admin/audit-logs` | backend/app/api/v1/admin_audit.py:24 `list_logs` → `services.audit_service.list_logs` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/audit-logs/cleanup` | backend/app/api/v1/admin_audit.py:41 `cleanup` → `services.audit_service.cleanup` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/audit-logs/cleanup-preview` | backend/app/api/v1/admin_audit.py:31 `preview_cleanup` → `services.audit_service.preview_cleanup` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/audit-logs/{log_id}` | backend/app/api/v1/admin_audit.py:51 `get_log` → `services.audit_service.get_log` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/backups` | backend/app/api/v1/admin_backups.py:64 `overview` → `services.backups.service.BackupManager.overview` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/backups/runs` | backend/app/api/v1/admin_backups.py:88 `runs` → `services.backups.service.BackupManager.list_runs` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/backups/runs` | backend/app/api/v1/admin_backups.py:106 `run_manual` → `services.backups.service.BackupManager.accept_manual` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/backups/runs/{run_id}` | backend/app/api/v1/admin_backups.py:100 `run_detail` → `services.backups.service.BackupManager.get_run` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/backups/runs/{run_id}/download` | backend/app/api/v1/admin_backups.py:118 `download` → `services.backups.service.BackupManager.authorize_download + backup_runtime.prepare_download` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| HEAD | `/api/v1/admin/backups/runs/{run_id}/download` | backend/app/api/v1/admin_backups.py:118 `download` → `services.backups.service.BackupManager.authorize_download + backup_runtime.prepare_download` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/backups/settings` | backend/app/api/v1/admin_backups.py:70 `settings` → `services.backups.service.BackupManager.read_settings` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| PATCH | `/api/v1/admin/backups/settings` | backend/app/api/v1/admin_backups.py:76 `update_settings` → `services.backups.service.BackupManager.update` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/dashboard/history` | backend/app/api/v1/admin_dashboard.py:33 `history` → `services.admin_dashboard_service.history` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/dashboard/overview` | backend/app/api/v1/admin_dashboard.py:23 `overview` → `services.admin_dashboard_service.overview` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/dashboard/tasks` | backend/app/api/v1/admin_dashboard.py:28 `tasks` → `services.admin_dashboard_service.tasks` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/permissions` | backend/app/api/v1/admin_roles.py:13 `permissions` → `services.role_service.permission_catalog` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/projects` | backend/app/api/v1/admin.py:30 `list_projects` → `services.admin_project_service.list_projects` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/recycle-bin/projects` | backend/app/api/v1/admin_recycle_bin.py:17 `list_projects` → `services.recycle_bin_service.list_projects` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/recycle-bin/projects/{project_id}/purge` | backend/app/api/v1/admin_recycle_bin.py:37 `purge` → `services.recycle_bin_service.purge` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/recycle-bin/projects/{project_id}/restore` | backend/app/api/v1/admin_recycle_bin.py:29 `restore` → `services.recycle_bin_service.restore` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/roles` | backend/app/api/v1/admin_roles.py:19 `roles` → `services.role_service.list_roles` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/roles` | backend/app/api/v1/admin_roles.py:35 `create` → `services.role_service.create_role` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| DELETE | `/api/v1/admin/roles/{role_id}` | backend/app/api/v1/admin_roles.py:47 `delete` → `services.role_service.delete_role` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/roles/{role_id}` | backend/app/api/v1/admin_roles.py:29 `role` → `services.role_service.get_role` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| PATCH | `/api/v1/admin/roles/{role_id}` | backend/app/api/v1/admin_roles.py:41 `update` → `services.role_service.update_role` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/system-settings` | backend/app/api/v1/admin_system_settings.py:16 `read_settings` → `services.system_settings_service.SystemSettingsManager.load` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| PATCH | `/api/v1/admin/system-settings` | backend/app/api/v1/admin_system_settings.py:24 `update_settings` → `services.system_settings_service.SystemSettingsManager.update`（`services.authorization_service.AuthenticatedActor`） | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/tasks` | backend/app/api/v1/admin_tasks.py:23 `list_tasks` → `services.admin_task_service.list_tasks` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/tasks/{task_key}` | backend/app/api/v1/admin_tasks.py:29 `get_task` → `services.admin_task_service.get_task` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/tasks/{task_key}/cancel` | backend/app/api/v1/admin_tasks.py:34 `cancel_task` → `services.admin_task_service.cancel_task` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/users` | backend/app/api/v1/admin.py:45 `list_users` → `services.admin_service.list_users` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/users` | backend/app/api/v1/admin.py:56 `create_user` → `services.admin_service.create_user` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| DELETE | `/api/v1/admin/users/{user_id}` | backend/app/api/v1/admin.py:79 `delete_user` → `services.admin_service.delete_user` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/users/{user_id}` | backend/app/api/v1/admin.py:61 `get_user_detail` → `services.admin_service.get_user_detail` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| PATCH | `/api/v1/admin/users/{user_id}` | backend/app/api/v1/admin.py:66 `update_user` → `services.admin_service.update_user` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/admin/users/{user_id}/quotas` | backend/app/api/v1/admin_quotas.py:15 `read` → `services.quota_service.get_quotas` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| PATCH | `/api/v1/admin/users/{user_id}/quotas` | backend/app/api/v1/admin_quotas.py:23 `update` → `services.quota_service.update_quotas` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/admin/users/{user_id}/reset-password` | backend/app/api/v1/admin.py:71 `reset_password` → `services.admin_service.reset_password` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| PUT | `/api/v1/admin/users/{user_id}/roles` | backend/app/api/v1/admin_roles.py:56 `assign` → `services.role_service.replace_user_roles` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/ai-configs` | backend/app/api/v1/ai_configs.py:17 `list_configs` → `services.ai_config_service.to_out`, `services.ai_config_service.list_configs` | 本人模型配置 | config_id / 用户模型归属按 ai_config_service；共享模型 E 单独授权 |
| POST | `/api/v1/ai-configs` | backend/app/api/v1/ai_configs.py:22 `create_config` → `services.ai_config_service.to_out`, `services.ai_config_service.create_config` | 本人模型配置 | config_id / 用户模型归属按 ai_config_service；共享模型 E 单独授权 |
| POST | `/api/v1/ai-configs/models` | backend/app/api/v1/ai_configs.py:27 `list_models` → `services.ai_config_service.list_models` | 本人模型配置；模型枚举／测试会出站 | config_id / 用户模型归属按 ai_config_service；共享模型 E 单独授权 |
| DELETE | `/api/v1/ai-configs/{config_id}` | backend/app/api/v1/ai_configs.py:46 `delete_config` → `services.ai_config_service.delete_config` | 本人模型配置 | config_id / 用户模型归属按 ai_config_service；共享模型 E 单独授权 |
| GET | `/api/v1/ai-configs/{config_id}` | backend/app/api/v1/ai_configs.py:34 `get_config` → `services.ai_config_service.to_out`, `services.ai_config_service.get_config` | 本人模型配置 | config_id / 用户模型归属按 ai_config_service；共享模型 E 单独授权 |
| PATCH | `/api/v1/ai-configs/{config_id}` | backend/app/api/v1/ai_configs.py:39 `update_config` → `services.ai_config_service.to_out`, `services.ai_config_service.update_config` | 本人模型配置 | config_id / 用户模型归属按 ai_config_service；共享模型 E 单独授权 |
| POST | `/api/v1/ai-configs/{config_id}/default` | backend/app/api/v1/ai_configs.py:52 `set_default` → `services.ai_config_service.to_out`, `services.ai_config_service.set_default` | 本人模型配置 | config_id / 用户模型归属按 ai_config_service；共享模型 E 单独授权 |
| POST | `/api/v1/ai-configs/{config_id}/test` | backend/app/api/v1/ai_configs.py:57 `test_config` → `services.ai_config_service.test_config` | 本人模型配置；模型枚举／测试会出站 | config_id / 用户模型归属按 ai_config_service；共享模型 E 单独授权 |
| GET | `/api/v1/analyses/{analysis_id}` | backend/app/api/v1/analysis.py:60 `get_analysis` → `services.analysis_service.get_analysis` | R + M | 分析、章节和项目一致；Agent 默认非 manual 严格作者。 P:119 |
| POST | `/api/v1/analysis-changes/{change_id}/confirm` | backend/app/api/v1/analysis.py:70 `confirm_change` → `services.writeback_service.confirm_change` | W + M | 父锁和可见性写门禁；纯管理员无协作者身份则 404。 P:120 |
| POST | `/api/v1/analysis-changes/{change_id}/reject` | backend/app/api/v1/analysis.py:78 `reject_change` → `services.writeback_service.reject_change` | W + M | 父锁，同作品关联。 P:121 |
| POST | `/api/v1/analysis-changes/{change_id}/revert` | backend/app/api/v1/analysis.py:65 `revert_change` → `services.writeback_service.revert_change` | W + M | 父锁和可见性写门禁。 P:122 |
| GET | `/api/v1/api-keys` | backend/app/api/v1/api_keys.py:12 `list_keys` → `services.api_key_service.list_keys` | 本人开放 API 密钥管理 | key_id 按 api_key_service 用户归属；开放 API 另限小说授权 |
| POST | `/api/v1/api-keys` | backend/app/api/v1/api_keys.py:18 `create_key` → `services.api_key_service.create_key` | 本人开放 API 密钥管理 | key_id 按 api_key_service 用户归属；开放 API 另限小说授权 |
| PATCH | `/api/v1/api-keys/{key_id}` | backend/app/api/v1/api_keys.py:26 `update_key` → `services.api_key_service.update_key` | 本人开放 API 密钥管理 | key_id 按 api_key_service 用户归属；开放 API 另限小说授权 |
| POST | `/api/v1/api-keys/{key_id}/reveal` | backend/app/api/v1/api_keys.py:32 `reveal_key` → `services.api_key_service.reveal_key` | 本人开放 API 密钥管理 | key_id 按 api_key_service 用户归属；开放 API 另限小说授权 |
| POST | `/api/v1/api-keys/{key_id}/revoke` | backend/app/api/v1/api_keys.py:40 `revoke_key` → `services.api_key_service.revoke_key` | 本人开放 API 密钥管理 | key_id 按 api_key_service 用户归属；开放 API 另限小说授权 |
| POST | `/api/v1/auth/change-password` | backend/app/api/v1/auth.py:61 `change_password` → `services.auth_service.change_password` | 本人账号认证 | 无作品；auth_service / CurrentUser |
| GET | `/api/v1/auth/config` | backend/app/api/v1/auth.py:26 `auth_config` → `API 内读取 Settings` | 公开认证／配置 | 无作品；auth_service / CurrentUser |
| POST | `/api/v1/auth/login` | backend/app/api/v1/auth.py:40 `login` → `services.auth_service.authenticate` | 公开认证／配置 | 无作品；auth_service / CurrentUser |
| POST | `/api/v1/auth/logout` | backend/app/api/v1/auth.py:48 `logout` → `API 内清除 Cookie` | 公开认证／配置 | 无作品；auth_service / CurrentUser |
| GET | `/api/v1/auth/me` | backend/app/api/v1/auth.py:55 `me` → `API 内 _user_out / CurrentUser` | 本人账号认证 | 无作品；auth_service / CurrentUser |
| POST | `/api/v1/auth/register` | backend/app/api/v1/auth.py:32 `register` → `services.auth_service.register` | 公开认证／配置 | 无作品；auth_service / CurrentUser |
| GET | `/api/v1/book-imports` | backend/app/api/v1/book_imports.py:43 `list_imports` → `services.book_import.service.list_imports` | 本人拆书解析；预估为本地计算 | 按当前 user.id 列表／上传；无现存共享作品写入 |
| POST | `/api/v1/book-imports` | backend/app/api/v1/book_imports.py:30 `upload` → `services.book_import.service.upload` | 本人拆书解析；预估为本地计算 | 按当前 user.id 列表／上传；无现存共享作品写入 |
| POST | `/api/v1/book-imports/tasks/{task_id}/skip` | backend/app/api/v1/book_imports.py:48 `skip_task` → `services.book_import.service.skip_task` | 本人拆书任务续跑；任务边界待集成 | task_id → reverse.skip_paused / task_service；核任务 actor 与原轮次 |
| DELETE | `/api/v1/book-imports/{import_id}` | backend/app/api/v1/book_imports.py:83 `delete_import` → `services.book_import.service.delete_import` | 本人拆书解析；预估为本地计算 | import_id → book_import.service.get_owned_import；无现存共享作品写入 |
| GET | `/api/v1/book-imports/{import_id}` | backend/app/api/v1/book_imports.py:53 `get_import` → `services.book_import.service.get_import` | 本人拆书解析；预估为本地计算 | import_id → book_import.service.get_owned_import；无现存共享作品写入 |
| GET | `/api/v1/book-imports/{import_id}/chapters/{index}` | backend/app/api/v1/book_imports.py:58 `chapter_text` → `services.book_import.service.chapter_text` | 本人拆书解析；预估为本地计算 | import_id → book_import.service.get_owned_import；无现存共享作品写入 |
| POST | `/api/v1/book-imports/{import_id}/estimate` | backend/app/api/v1/book_imports.py:73 `estimate` → `services.book_import.service.estimate` | 本人拆书解析；预估为本地计算 | import_id → book_import.service.get_owned_import；无现存共享作品写入 |
| POST | `/api/v1/book-imports/{import_id}/ops` | backend/app/api/v1/book_imports.py:68 `apply_op` → `services.book_import.service.apply_op` | 本人拆书解析；预估为本地计算 | import_id → book_import.service.get_owned_import；无现存共享作品写入 |
| POST | `/api/v1/book-imports/{import_id}/resplit` | backend/app/api/v1/book_imports.py:63 `resplit` → `services.book_import.service.resplit` | 本人拆书解析；预估为本地计算 | import_id → book_import.service.get_owned_import；无现存共享作品写入 |
| POST | `/api/v1/book-imports/{import_id}/start` | backend/app/api/v1/book_imports.py:78 `start` → `services.book_import.service.start` | 本人拆书解析 → 新建本人作品 + AI 任务；E 集成待核 | import_id → get_owned_import；start 新建 Project；reverse/Task 保持本人 |
| GET | `/api/v1/chapters/{chapter_id}/analyses` | backend/app/api/v1/analysis.py:55 `list_analyses` → `services.analysis_service.list_analyses` | R + M | 章节及分析项目一致。 P:118 |
| GET | `/api/v1/chapters/{chapter_id}/analysis-status` | backend/app/api/v1/analysis.py:50 `chapter_analysis_status` → `services.analysis_service.chapter_state` | R + M | 先按章节定位并授权父作品。 P:117 |
| POST | `/api/v1/chapters/{chapter_id}/analyze` | backend/app/api/v1/analysis.py:22 `analyze_chapter` → `services.analysis_service.request_analyze` | O | 严格作者章节后排任务；不因 R/W 变成协作 AI。 P:123 |
| GET | `/api/v1/chapters/{chapter_id}/context-preview` | backend/app/api/v1/memories.py:99 `context_preview` → `services.context_builder.retrieval_preview` | O | 先严格查本人章节和项目，再预览知识与记忆检索；非上表纯本地规划预览，E 未迁移。 P:97 |
| POST | `/api/v1/chapters/{chapter_id}/generate` | backend/app/api/v1/chapters.py:116 `generate_chapter` → `services.task_service.create_task` | AI/E | 待 AI/日志代理核验 M:63 |
| POST | `/api/v1/chapters/{chapter_id}/polish` | backend/app/api/v1/chapters.py:128 `polish_chapter` → `services.task_service.create_task` | AI/E | 待 AI/日志代理核验 M:64 |
| POST | `/api/v1/chapters/{chapter_id}/revise` | backend/app/api/v1/analysis.py:83 `revise_chapter` → `services.task_service.create_task` | O，AI | 任务 handler 还校验章节；不是人工回写。 P:125 |
| POST | `/api/v1/chapters/{chapter_id}/rewrite` | backend/app/api/v1/chapter_versions.py:79 `rewrite_preview` → `services.rewrite_service.prepare_rewrite_job`, `services.rewrite_service.run_rewrite_job` | AI/E | 待 AI/日志代理核验 M:79 |
| GET | `/api/v1/collaboration/invitations` | backend/app/api/v1/collaboration.py:79 `invitations` → `services.collaboration_service.list_invitations` | 本人邀请；接受／拒绝须原 UUID 和 revision | 按本人待处理成员行分别关联 project_id / series_id；collaboration_service.list_invitations |
| POST | `/api/v1/collaboration/invitations/{scope}/{invitation_id}/accept` | backend/app/api/v1/collaboration.py:84 `accept_invitation` → `services.collaboration_service.respond` | 本人邀请；接受／拒绝须原 UUID 和 revision | scope + invitation_id → 当前用户成员行 → project_id / series_id；collaboration_service.respond |
| POST | `/api/v1/collaboration/invitations/{scope}/{invitation_id}/decline` | backend/app/api/v1/collaboration.py:93 `decline_invitation` → `services.collaboration_service.respond` | 本人邀请；接受／拒绝须原 UUID 和 revision | scope + invitation_id → 当前用户成员行 → project_id / series_id；collaboration_service.respond |
| GET | `/api/v1/inspiration/scale-suggest` | backend/app/api/v1/inspiration.py:30 `scale_suggest` → `services.inspiration_service.suggest_scale` | 登录用户的本地规模计算 | target_words → inspiration_service.suggest_scale；无会话或作品 |
| GET | `/api/v1/inspiration/sessions` | backend/app/api/v1/inspiration.py:38 `list_sessions` → `services.inspiration_service.list_sessions` | 本人灵感会话／参数计算 | 按当前 user.id 列表／新建；非共享会话 |
| POST | `/api/v1/inspiration/sessions` | backend/app/api/v1/inspiration.py:43 `create_session` → `services.inspiration_service.create_session` | 本人灵感会话／参数计算 | 按当前 user.id 列表／新建；非共享会话 |
| POST | `/api/v1/inspiration/sessions/from-project/{project_id}` | backend/app/api/v1/inspiration.py:48 `from_project` → `services.inspiration_service.from_project` | 本人会话 + 严格作者作品；协作入口未迁移 | project_id → project_service.get_owned_project；查找或新建本人会话 |
| DELETE | `/api/v1/inspiration/sessions/{session_id}` | backend/app/api/v1/inspiration.py:63 `delete_session` → `services.inspiration_service.delete_session` | 本人灵感会话／参数计算 | session_id → inspiration_service.get_owned_session；非共享会话 |
| GET | `/api/v1/inspiration/sessions/{session_id}` | backend/app/api/v1/inspiration.py:53 `get_session` → `services.inspiration_service.get_session` | 本人灵感会话／参数计算 | session_id → inspiration_service.get_owned_session；非共享会话 |
| PATCH | `/api/v1/inspiration/sessions/{session_id}` | backend/app/api/v1/inspiration.py:58 `update_session` → `services.inspiration_service.update_session` | 本人灵感会话／参数计算 | session_id → inspiration_service.get_owned_session；非共享会话 |
| POST | `/api/v1/inspiration/sessions/{session_id}/apply` | backend/app/api/v1/inspiration.py:123 `apply` → `services.inspiration_service.apply` | 本人会话 + 严格作者作品；协作入口未迁移 | session_id → get_owned_session；project_id → project_service.get_owned_project |
| GET | `/api/v1/inspiration/sessions/{session_id}/apply-preview` | backend/app/api/v1/inspiration.py:116 `apply_preview` → `services.inspiration_service.apply_preview` | 本人会话 + 严格作者作品；协作入口未迁移 | session_id → get_owned_session；project_id → project_service.get_owned_project |
| POST | `/api/v1/inspiration/sessions/{session_id}/convert` | backend/app/api/v1/inspiration.py:111 `convert` → `services.inspiration_service.convert` | 本人会话转换为新建本人作品 | session_id → get_owned_session；convert 新建 Project |
| POST | `/api/v1/inspiration/sessions/{session_id}/notes` | backend/app/api/v1/inspiration.py:74 `add_note` → `services.inspiration_service.add_note` | 本人灵感会话／参数计算 | session_id → inspiration_service.get_owned_session；非共享会话 |
| DELETE | `/api/v1/inspiration/sessions/{session_id}/notes/{index}` | backend/app/api/v1/inspiration.py:79 `delete_note` → `services.inspiration_service.delete_note` | 本人灵感会话／参数计算 | session_id → inspiration_service.get_owned_session；非共享会话 |
| PATCH | `/api/v1/inspiration/sessions/{session_id}/steps/{step}` | backend/app/api/v1/inspiration.py:69 `update_step` → `services.inspiration_service.update_step` | 本人灵感会话／参数计算 | session_id → inspiration_service.get_owned_session；非共享会话 |
| POST | `/api/v1/inspiration/sessions/{session_id}/steps/{step}/generate` | backend/app/api/v1/inspiration.py:84 `generate_options` → `services.inspiration_service.prepare_generate`, `services.inspiration_service.run_generate` | 本人会话 AI；关联本人作品时可读知识库并出站，E 待核 | session_id → get_owned_session；session.project_id → 本人活跃 Project；prepare_generate / run_generate |
| GET | `/api/v1/mcp-plugins` | backend/app/api/v1/mcp_plugins.py:34 `list_plugins` → `services.mcp.service.list_plugins` | 本人 MCP 配置 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| POST | `/api/v1/mcp-plugins` | backend/app/api/v1/mcp_plugins.py:39 `create_plugin` → `services.mcp.service.create_plugin` | 本人 MCP 配置 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| POST | `/api/v1/mcp-plugins/import` | backend/app/api/v1/mcp_plugins.py:46 `import_plugins` → `services.mcp.service.import_servers` | 本人 MCP 配置 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| DELETE | `/api/v1/mcp-plugins/{plugin_id}` | backend/app/api/v1/mcp_plugins.py:65 `delete_plugin` → `services.mcp.service.delete_plugin` | 本人 MCP 配置 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| GET | `/api/v1/mcp-plugins/{plugin_id}` | backend/app/api/v1/mcp_plugins.py:53 `get_plugin` → `services.mcp.service.to_out`, `services.mcp.service.get_owned_plugin` | 本人 MCP 配置 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| PATCH | `/api/v1/mcp-plugins/{plugin_id}` | backend/app/api/v1/mcp_plugins.py:58 `update_plugin` → `services.mcp.service.update_plugin` | 本人 MCP 配置 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| POST | `/api/v1/mcp-plugins/{plugin_id}/call` | backend/app/api/v1/mcp_plugins.py:86 `call_tool` → `services.mcp.service.call_tool` | 本人 MCP 配置；工具调用／刷新可能外部网络出站 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| POST | `/api/v1/mcp-plugins/{plugin_id}/refresh` | backend/app/api/v1/mcp_plugins.py:71 `refresh_plugin` → `services.mcp.service.refresh_tools` | 本人 MCP 配置；工具调用／刷新可能外部网络出站 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| PATCH | `/api/v1/mcp-plugins/{plugin_id}/tools` | backend/app/api/v1/mcp_plugins.py:78 `update_tool` → `services.mcp.service.update_tool_pref` | 本人 MCP 配置 | plugin_id 及工具按 MCP service 的用户归属；Agent 联动 E 待核 |
| GET | `/api/v1/projects` | backend/app/api/v1/projects.py:13 `list_projects` → `services.project_service.list_projects` | 列表/创建 | P M:51 |
| POST | `/api/v1/projects` | backend/app/api/v1/projects.py:18 `create_project` → `services.project_service.create_project`, `services.project_service.project_out` | 列表/创建 | P M:52 |
| POST | `/api/v1/projects/import` | backend/app/api/v1/transfer.py:37 `import_project` → `services.project_transfer.importer.get_token`, `services.project_transfer.importer.import_backup`, `services.project_transfer.importer.drop_token` | 本人备份解析／导入新作品 | 导入 token / 文件归当前用户；importer.preview / import_backup |
| POST | `/api/v1/projects/import/preview` | backend/app/api/v1/transfer.py:27 `preview_import` → `services.project_transfer.importer.preview` | 本人备份解析／导入新作品 | 导入 token / 文件归当前用户；importer.preview / import_backup |
| DELETE | `/api/v1/projects/{project_id}` | backend/app/api/v1/projects.py:44 `delete_project` → `services.project_service.delete_project` | 回收 | P M:53 |
| GET | `/api/v1/projects/{project_id}` | backend/app/api/v1/projects.py:24 `get_project` → `services.project_access.require_project`, `services.project_service.project_out` | R | P M:54 |
| PATCH | `/api/v1/projects/{project_id}` | backend/app/api/v1/projects.py:30 `update_project` → `services.project_service.update_project`, `services.project_service.project_out` | W | P M:55 |
| POST | `/api/v1/projects/{project_id}/agent/changes/{change_id}/confirm` | backend/app/api/v1/agent.py:96 `confirm_change` → `services.agent.service.decide_change` | O | 按本人会话和项目校验，可能执行提案。 P:144 |
| POST | `/api/v1/projects/{project_id}/agent/changes/{change_id}/reject` | backend/app/api/v1/agent.py:101 `reject_change` → `services.agent.service.decide_change` | O | 同作品本人会话，可能恢复轮次。 P:145 |
| POST | `/api/v1/projects/{project_id}/agent/changes/{change_id}/undo` | backend/app/api/v1/agent.py:116 `undo_change` → `services.agent.service.undo_change` | O | 执行效果先预检；记忆变化可由作者排向量任务。 P:146 |
| GET | `/api/v1/projects/{project_id}/agent/conversations` | backend/app/api/v1/agent.py:50 `list_conversations` → `services.agent.service.list_conversations` | O | 项目及 `conversation.user_id` 均匹配本人。 P:137 |
| POST | `/api/v1/projects/{project_id}/agent/conversations` | backend/app/api/v1/agent.py:55 `create_conversation` → `services.agent.service.create_conversation` | O | 锁作者项目，建立本人会话。 P:138 |
| DELETE | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}` | backend/app/api/v1/agent.py:81 `delete_conversation` → `services.agent.service.delete_conversation` | O | 活动轮次冲突。 P:141 |
| GET | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}` | backend/app/api/v1/agent.py:62 `get_conversation` → `services.agent.service.get_conversation` | O | 同作品且本人会话，消息/提案再做私人引用投影。 P:139 |
| PATCH | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}` | backend/app/api/v1/agent.py:69 `update_conversation` → `services.agent.service.update_conversation` | O | 锁作者项目和本人会话，插件仅本人。 P:140 |
| GET | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}/generation-logs` | backend/app/api/v1/agent.py:76 `list_generation_logs` → `services.agent.service.list_generation_logs` | M | 例外：只读本人会话的本人工程记录；不连带开放会话读写或发送。 P:142 |
| POST | `/api/v1/projects/{project_id}/agent/conversations/{conversation_id}/messages` | backend/app/api/v1/agent.py:87 `send_message` → `services.agent.service.send_message` | O，AI | 真实本人会话、父作品，历史 epoch 复验。 P:143 |
| POST | `/api/v1/projects/{project_id}/agent/turns/{task_id}/confirm-all` | backend/app/api/v1/agent.py:106 `confirm_turn` → `services.agent.service.decide_turn` | O | 本人 `agent.turn`、作品与会话关联。 P:147 |
| POST | `/api/v1/projects/{project_id}/agent/turns/{task_id}/reject-all` | backend/app/api/v1/agent.py:111 `reject_turn` → `services.agent.service.decide_turn` | O | 本人轮次，可能恢复任务。 P:148 |
| POST | `/api/v1/projects/{project_id}/agent/turns/{task_id}/undo` | backend/app/api/v1/agent.py:121 `undo_turn` → `services.agent.service.undo_turn` | O | 本人轮次与作品，效果预检。 P:149 |
| POST | `/api/v1/projects/{project_id}/analysis/analyze-pending` | backend/app/api/v1/analysis.py:38 `analyze_pending` → `services.analysis_service.analyze_pending` | O | 严格作者项目后排队。 P:124 |
| GET | `/api/v1/projects/{project_id}/analysis/statuses` | backend/app/api/v1/analysis.py:45 `analysis_statuses` → `services.analysis_service.project_states` | R + M | 查询本作品章节；纯管理员无分析可见性，任务 ID 仅本人。 P:116 |
| GET | `/api/v1/projects/{project_id}/backup` | backend/app/api/v1/transfer.py:62 `export_backup` → `services.project_transfer.exporter.prepare_export`, `services.project_transfer.exporter.stream_export` | 作品作者专属备份 | project_id → exporter.prepare_export / estimate；过滤日志 |
| GET | `/api/v1/projects/{project_id}/backup/estimate` | backend/app/api/v1/transfer.py:84 `estimate_backup` → `services.project_transfer.exporter.estimate` | 作品作者专属备份 | project_id → exporter.prepare_export / estimate；过滤日志 |
| POST | `/api/v1/projects/{project_id}/batch-generate` | backend/app/api/v1/batch.py:20 `start_batch` → `services.task_service.create_task` | 批量生成预览／AI 任务；严格作者，E 待接通 | project_id → batch_service.preview / validate_start；活动任务身份须复核 |
| POST | `/api/v1/projects/{project_id}/batch-generate/preview` | backend/app/api/v1/batch.py:13 `preview_batch` → `services.batch_service.preview` | 批量生成预览／AI 任务；严格作者，E 待接通 | project_id → batch_service.preview / validate_start；活动任务身份须复核 |
| POST | `/api/v1/projects/{project_id}/cards/export` | backend/app/api/v1/cards.py:14 `export_cards` → `services.card_service.export_cards` | X，卡片导出 | M:282；project_id + 条目归属；见 phase-d-cards.md，旧“进行中”已更新 |
| POST | `/api/v1/projects/{project_id}/cards/import` | backend/app/api/v1/cards.py:24 `import_cards` → `services.card_service.import_cards` | X + W，卡片导入 | M:283；project_id + 条目归属；见 phase-d-cards.md，旧“进行中”已更新 |
| POST | `/api/v1/projects/{project_id}/cards/import/preview` | backend/app/api/v1/cards.py:19 `preview_import` → `services.card_service.preview_cards` | X，本地导入预览 | M:284；project_id + 条目归属；见 phase-d-cards.md，旧“进行中”已更新 |
| GET | `/api/v1/projects/{project_id}/careers` | backend/app/api/v1/careers.py:27 `list_careers` → `services.career_service.list_careers` | R | CA M:155 |
| POST | `/api/v1/projects/{project_id}/careers` | backend/app/api/v1/careers.py:32 `create_career` → `services.career_service.create_career` | W | CA M:156 |
| POST | `/api/v1/projects/{project_id}/careers/batch` | backend/app/api/v1/careers.py:37 `create_careers` → `services.career_service.create_careers` | 旧作者／管理员确认入口，editor 待核 | CA M:157 |
| POST | `/api/v1/projects/{project_id}/careers/generate` | backend/app/api/v1/careers.py:42 `generate_careers` → `services.task_service.create_task` | AI/E | 待 AI/日志代理核验 M:158 |
| DELETE | `/api/v1/projects/{project_id}/careers/{career_id}` | backend/app/api/v1/careers.py:64 `delete_career` → `services.career_service.delete_career` | W | CA M:159 |
| GET | `/api/v1/projects/{project_id}/careers/{career_id}` | backend/app/api/v1/careers.py:54 `get_career` → `services.career_service.get_career` | R | CA M:160 |
| PATCH | `/api/v1/projects/{project_id}/careers/{career_id}` | backend/app/api/v1/careers.py:59 `update_career` → `services.career_service.update_career` | W | CA M:161 |
| GET | `/api/v1/projects/{project_id}/careers/{career_id}/holders` | backend/app/api/v1/careers.py:70 `list_holders` → `services.career_service.list_holders` | R | CA M:162 |
| GET | `/api/v1/projects/{project_id}/chapters` | backend/app/api/v1/chapters.py:31 `list_chapters` → `services.chapter_service.list_manual_chapters`, `services.chapter_service.manual_chapter_views` | R | C M:65 |
| POST | `/api/v1/projects/{project_id}/chapters` | backend/app/api/v1/chapters.py:37 `create_chapter` → `services.chapter_service.create_chapter`, `services.chapter_service.manual_chapter_views` | W | C M:66 |
| POST | `/api/v1/projects/{project_id}/chapters/from-outlines` | backend/app/api/v1/chapters.py:43 `create_from_outlines` → `services.chapter_service.create_from_outlines`, `services.chapter_service.manual_chapter_views` | W | C M:67 |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | backend/app/api/v1/chapters.py:97 `delete_chapter` → `services.chapter_service.delete_chapter` | W | C M:68 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | backend/app/api/v1/chapters.py:55 `get_chapter` → `services.chapter_service.get_manual_chapter`, `services.chapter_service.manual_chapter_views` | R | C M:69 |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}` | backend/app/api/v1/chapters.py:89 `update_chapter` → `services.chapter_service.update_chapter`, `services.chapter_service.manual_chapter_views` | W | C M:70 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/cast` | backend/app/api/v1/chapters.py:103 `get_cast` → `services.cast_service.get_cast` | R | C M:71 |
| PUT | `/api/v1/projects/{project_id}/chapters/{chapter_id}/cast` | backend/app/api/v1/chapters.py:108 `set_cast` → `services.cast_service.set_cast` | W | C M:72 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/foreshadow-reminders` | backend/app/api/v1/foreshadows.py:102 `chapter_foreshadow_reminders` → `services.foreshadow_service.chapter_reminders` | R | FS M:139 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/generation-logs` | backend/app/api/v1/chapter_versions.py:64 `list_logs` → `services.generation_log_service.list_logs` | M | 只返回当前 actor、当前项目、当前章节的记录。 P:135 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/generation-logs/{log_id}` | backend/app/api/v1/chapter_versions.py:69 `get_log` → `services.generation_log_service.get_log` | M | 额外匹配 log ID，嵌套私人引用投影。 P:136 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/plan` | backend/app/api/v1/outline_pro.py:131 `chapter_plan` → `services.plan_service.chapter_plan_out` | R | 章节、计划与大纲均限定项目。 P:63 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/rewrite/apply` | backend/app/api/v1/chapter_versions.py:74 `apply_rewrite` → `services.rewrite_service.apply_rewrite` | AI/E | 待 AI/日志代理核验 M:82 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes` | backend/app/api/v1/scenes.py:81 `list_scenes` → `services.scene_service.list_scenes` | R | S M:96 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes` | backend/app/api/v1/scenes.py:89 `create_scene` → `services.scene_service.create_scene` | W | S M:97 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/merge` | backend/app/api/v1/scenes.py:119 `merge_scenes` → `services.scene_service.merge_scenes` | W | S M:98 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/merge-preview` | backend/app/api/v1/scenes.py:112 `merge_preview` → `services.scene_service.merge_preview` | R | S M:99 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/reorder` | backend/app/api/v1/scenes.py:105 `reorder_scenes` → `services.scene_service.reorder_scenes` | W | S M:100 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/resolve-position` | backend/app/api/v1/scenes.py:96 `resolve_position` → `services.scene_service.resolve_position` | R | S M:101 |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | backend/app/api/v1/scenes.py:145 `delete_scene` → `services.scene_service.delete_scene` | W | S M:102 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | backend/app/api/v1/scenes.py:126 `get_scene` → `services.scene_service.get_scene` | R | S M:103 |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}` | backend/app/api/v1/scenes.py:131 `update_scene` → `services.scene_service.update_scene` | W | S M:104 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document` | backend/app/api/v1/scenes.py:162 `get_document` → `services.rich_document_service.get_document` | R | S M:105 |
| PUT | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document` | backend/app/api/v1/scenes.py:169 `save_document` → `services.rich_document_service.save_document` | W | S M:106 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/document/sections/{section_key}/convert` | backend/app/api/v1/scenes.py:176 `convert_section` → `services.rich_document_conversion.convert_section` | W | S M:107 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/generate` | backend/app/api/v1/scene_ai.py:43 `generate_scene` → `services.task_service.create_task` | 场景生成／改写或 AI 草稿采纳，E 未接通 | project_id + chapter_id + scene_id 逐级限定；section_key 同富文档；见 phase-e-chapter-ai-map；旧作者链，协作 AI 未完成 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/polish` | backend/app/api/v1/scene_ai.py:50 `polish_scene` → `services.task_service.create_task` | 场景生成／改写或 AI 草稿采纳，E 未接通 | project_id + chapter_id + scene_id 逐级限定；section_key 同富文档；见 phase-e-chapter-ai-map；旧作者链，协作 AI 未完成 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/revise` | backend/app/api/v1/scene_ai.py:57 `revise_scene` → `services.task_service.create_task` | 场景生成／改写或 AI 草稿采纳，E 未接通 | project_id + chapter_id + scene_id 逐级限定；section_key 同富文档；见 phase-e-chapter-ai-map；旧作者链，协作 AI 未完成 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/rewrite` | backend/app/api/v1/scene_ai.py:64 `rewrite_scene` → `services.rewrite_service.prepare_rewrite_job`, `services.rewrite_service.run_rewrite_job` | 场景生成／改写或 AI 草稿采纳，E 未接通 | project_id + chapter_id + scene_id 逐级限定；section_key 同富文档；见 phase-e-chapter-ai-map；旧作者链，协作 AI 未完成 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/rewrite/apply` | backend/app/api/v1/scene_ai.py:77 `apply_scene_rewrite` → `services.rewrite_service.apply_scene_rewrite` | 场景生成／改写或 AI 草稿采纳，E 未接通 | project_id + chapter_id + scene_id 逐级限定；section_key 同富文档；见 phase-e-chapter-ai-map；旧作者链，协作 AI 未完成 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/sections/{section_key}/rewrite` | backend/app/api/v1/scene_ai.py:83 `rewrite_section` → `services.rich_document_section_ai.prepare_section_rewrite`, `services.rewrite_service.run_rewrite_job` | 场景生成／改写或 AI 草稿采纳，E 未接通 | project_id + chapter_id + scene_id 逐级限定；section_key 同富文档；见 phase-e-chapter-ai-map；旧作者链，协作 AI 未完成 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/sections/{section_key}/rewrite/apply` | backend/app/api/v1/scene_ai.py:98 `apply_section_rewrite` → `services.rich_document_section_ai.apply_section_rewrite` | 场景生成／改写或 AI 草稿采纳，E 未接通 | project_id + chapter_id + scene_id 逐级限定；section_key 同富文档；见 phase-e-chapter-ai-map；旧作者链，协作 AI 未完成 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/setting-context` | backend/app/api/v1/scenes.py:153 `setting_context` → `services.planning_context_service.scene_setting_context` | R，本地场景设定上下文 | S M:108；见 phase-d-content.md，旧“进行中”已更新 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/split` | backend/app/api/v1/scenes.py:138 `split_scene` → `services.scene_service.split_scene` | W | S M:109 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/setting-context` | backend/app/api/v1/chapters.py:61 `get_setting_context` → `services.chapter_service.get_manual_chapter`, `services.project_access.require_project`, `services.planning_context_service.load_planning_context`, `services.context_builder.build_setting_context`, `services.setting_progression_service.count_unverified_timelines`, `services.planning_context_service.check_planning_revision`, `services.scene_service.check_revision`, `services.setting_field_service.check_fields_revision` | R，本地章节设定上下文 | C M:73；见 phase-d-content.md，旧“进行中”已更新 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions` | backend/app/api/v1/chapter_versions.py:30 `list_versions` → `services.chapter_version_service.list_versions` | R | V M:83 |
| DELETE | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | backend/app/api/v1/chapter_versions.py:49 `delete_version` → `services.chapter_version_service.delete_version` | W | V M:84 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | backend/app/api/v1/chapter_versions.py:35 `get_version` → `services.chapter_version_service.get_version` | R | V M:85 |
| PATCH | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}` | backend/app/api/v1/chapter_versions.py:42 `update_version` → `services.chapter_version_service.update_version` | W | V M:86 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/extract` | backend/app/api/v1/chapter_versions.py:122 `extract_version` → `services.chapter_version_service.extract_version` | W | V M:87 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/extract-preview` | backend/app/api/v1/chapter_versions.py:113 `extract_preview` → `services.chapter_version_service.extract_preview` | R | V M:88 |
| POST | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/restore` | backend/app/api/v1/chapter_versions.py:55 `restore_version` → `services.chapter_version_service.restore_version`, `services.chapter_service.manual_chapter_views` | W | V M:89 |
| GET | `/api/v1/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/restore-preview` | backend/app/api/v1/chapter_versions.py:105 `restore_preview` → `services.chapter_version_service.restore_preview` | R | V M:90 |
| GET | `/api/v1/projects/{project_id}/character-careers` | backend/app/api/v1/careers.py:75 `list_project_assignments` → `services.career_service.list_project_assignments` | R | CA M:163 |
| GET | `/api/v1/projects/{project_id}/characters` | backend/app/api/v1/story.py:45 `list_items` → `services.series_read.read_book_settings` | R | ST M:119 |
| POST | `/api/v1/projects/{project_id}/characters` | backend/app/api/v1/story.py:51 `create_item` → `services.story_service.create_owned` | W | ST M:120 |
| POST | `/api/v1/projects/{project_id}/characters/generate` | backend/app/api/v1/character_actions.py:23 `generate_character` → `services.task_service.create_task` | AI/E | 待 AI/日志代理核验 M:290 |
| POST | `/api/v1/projects/{project_id}/characters/with-relations` | backend/app/api/v1/character_actions.py:16 `create_with_relations` → `services.relationship_service.create_character_with_relations` | 旧作者／管理员确认入口，editor 待核 | RL M:291 |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/career-history` | backend/app/api/v1/careers.py:131 `list_history` → `services.career_service.list_history` | R | CA M:164 |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/careers` | backend/app/api/v1/careers.py:80 `list_assignments` → `services.career_service.list_assignments` | R | CA M:165 |
| POST | `/api/v1/projects/{project_id}/characters/{character_id}/careers` | backend/app/api/v1/careers.py:85 `assign` → `services.career_service.assign` | W | CA M:166 |
| DELETE | `/api/v1/projects/{project_id}/characters/{character_id}/careers/{assignment_id}` | backend/app/api/v1/careers.py:99 `remove_assignment` → `services.career_service.remove_assignment` | W | CA M:167 |
| PATCH | `/api/v1/projects/{project_id}/characters/{character_id}/careers/{assignment_id}` | backend/app/api/v1/careers.py:92 `update_assignment` → `services.career_service.update_assignment` | W | CA M:168 |
| GET | `/api/v1/projects/{project_id}/characters/{character_id}/memberships` | backend/app/api/v1/organizations.py:138 `character_memberships` → `services.organization_service.character_memberships` | R | OR M:174 |
| POST | `/api/v1/projects/{project_id}/characters/{character_id}/parse-relations` | backend/app/api/v1/character_actions.py:35 `parse_relations` → `services.task_service.create_task` | AI/E | 待 AI/日志代理核验 M:292 |
| DELETE | `/api/v1/projects/{project_id}/characters/{item_id}` | backend/app/api/v1/story.py:74 `delete_item` → `services.story_service.delete_owned` | W | ST M:121 |
| GET | `/api/v1/projects/{project_id}/characters/{item_id}` | backend/app/api/v1/story.py:55 `get_item` → `services.series_read.read_book_settings` | R | ST M:122 |
| PATCH | `/api/v1/projects/{project_id}/characters/{item_id}` | backend/app/api/v1/story.py:61 `update_item` → `services.story_service.update_owned`, `services.series_read.read_book_settings` | W | ST M:123 |
| GET | `/api/v1/projects/{project_id}/collaborators` | backend/app/api/v1/collaboration.py:25 `project_members` → `services.collaboration_service.list_members` | 作品作者成员管理 | project_id / sid → 父资源；成员由 user_id 和父资源共同限定 |
| POST | `/api/v1/projects/{project_id}/collaborators` | backend/app/api/v1/collaboration.py:30 `invite_project` → `services.collaboration_service.invite` | 作品作者成员管理 | project_id / sid → 父资源；成员由 user_id 和父资源共同限定 |
| DELETE | `/api/v1/projects/{project_id}/collaborators/{user_id}` | backend/app/api/v1/collaboration.py:49 `revoke_project_member` → `services.collaboration_service.change_member` | 作品作者成员管理 | project_id / sid → 父资源；成员由 user_id 和父资源共同限定 |
| PATCH | `/api/v1/projects/{project_id}/collaborators/{user_id}` | backend/app/api/v1/collaboration.py:38 `modify_project_member` → `services.collaboration_service.change_member` | 作品作者成员管理 | project_id / sid → 父资源；成员由 user_id 和父资源共同限定 |
| DELETE | `/api/v1/projects/{project_id}/cover` | backend/app/api/v1/project_cover.py:35 `delete_cover` → `services.cover_service.delete_cover` | W | P M:264 |
| GET | `/api/v1/projects/{project_id}/cover` | backend/app/api/v1/project_cover.py:14 `get_cover` → `services.cover_service.get_manual_cover` | R | P M:265 |
| PUT | `/api/v1/projects/{project_id}/cover` | backend/app/api/v1/project_cover.py:24 `put_cover` → `services.cover_service.put_cover` | W | P M:266 |
| POST | `/api/v1/projects/{project_id}/cover/generate` | backend/app/api/v1/project_cover.py:45 `generate_cover` → `services.cover_service.generate_cover` | AI/E | 待 AI/日志代理核验 M:267 |
| GET | `/api/v1/projects/{project_id}/cover/prompt` | backend/app/api/v1/project_cover.py:40 `default_prompt` → `services.cover_service.default_prompt` | AI/E | 待 AI/日志代理核验 M:268 |
| GET | `/api/v1/projects/{project_id}/export` | backend/app/api/v1/projects.py:50 `export_project` → `services.export_service.parse_chapter_ids`, `services.export_service.export_manual_project` | X | P M:56 |
| GET | `/api/v1/projects/{project_id}/foreshadows` | backend/app/api/v1/foreshadows.py:23 `list_foreshadows` → `services.foreshadow_service.list_foreshadows` | R | FS M:140 |
| POST | `/api/v1/projects/{project_id}/foreshadows` | backend/app/api/v1/foreshadows.py:47 `create_foreshadow` → `services.foreshadow_service.create_foreshadow` | W | FS M:141 |
| GET | `/api/v1/projects/{project_id}/foreshadows/stats` | backend/app/api/v1/foreshadows.py:53 `foreshadow_stats` → `services.foreshadow_service.stats` | R | FS M:142 |
| DELETE | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | backend/app/api/v1/foreshadows.py:70 `delete_foreshadow` → `services.foreshadow_service.delete_foreshadow` | W | FS M:143 |
| GET | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | backend/app/api/v1/foreshadows.py:58 `get_foreshadow` → `services.foreshadow_service.get_foreshadow` | R | FS M:144 |
| PATCH | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}` | backend/app/api/v1/foreshadows.py:63 `update_foreshadow` → `services.foreshadow_service.update_foreshadow` | W | FS M:145 |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/abandon` | backend/app/api/v1/foreshadows.py:90 `abandon_foreshadow` → `services.foreshadow_service.abandon` | W | FS M:146 |
| GET | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/history` | backend/app/api/v1/foreshadows.py:95 `foreshadow_history` → `services.foreshadow_service.list_history` | R | FS M:147 |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/plant` | backend/app/api/v1/foreshadows.py:76 `plant_foreshadow` → `services.foreshadow_service.plant` | W | FS M:148 |
| POST | `/api/v1/projects/{project_id}/foreshadows/{foreshadow_id}/resolve` | backend/app/api/v1/foreshadows.py:83 `resolve_foreshadow` → `services.foreshadow_service.resolve` | W | FS M:149 |
| GET | `/api/v1/projects/{project_id}/kb/documents` | backend/app/api/v1/kb.py:38 `list_documents` → `services.kb_service.list_documents` | R | 按作品过滤文档。 P:100 |
| POST | `/api/v1/projects/{project_id}/kb/documents` | backend/app/api/v1/kb.py:43 `upload_document` → `services.project_access.require_project`, `services.kb_service.upload_document` | O | 后续严格作者并创建索引任务；编辑者未接通上传。 P:101 |
| DELETE | `/api/v1/projects/{project_id}/kb/documents/{document_id}` | backend/app/api/v1/kb.py:68 `delete_document` → `services.kb_service.delete_document` | W | 父锁、同作品文档；索引活动中冲突。 P:103 |
| PATCH | `/api/v1/projects/{project_id}/kb/documents/{document_id}` | backend/app/api/v1/kb.py:61 `update_document` → `services.kb_service.set_inject` | W | 父锁，`get_project_document:92` 同时限定作品和文档；同步本地检索开关。 P:102 |
| GET | `/api/v1/projects/{project_id}/kb/documents/{document_id}/chunks` | backend/app/api/v1/kb.py:82 `list_chunks` → `services.kb_service.list_chunks` | R | 先限定同作品文档，再按该文档列分块。 P:104 |
| POST | `/api/v1/projects/{project_id}/kb/documents/{document_id}/reindex` | backend/app/api/v1/kb.py:74 `reindex_document` → `services.project_access.require_project`, `services.kb_service.reindex_document` | O | 作者锁及同作品文档；E 未迁移。 P:105 |
| GET | `/api/v1/projects/{project_id}/kb/index-status` | backend/app/api/v1/kb.py:102 `index_status` → `services.kb_service.index_status` | O | 旧作者索引状态，非一般 R。 P:107 |
| POST | `/api/v1/projects/{project_id}/kb/rebuild` | backend/app/api/v1/kb.py:107 `rebuild` → `services.project_access.require_project`, `services.kb_service.start_rebuild` | O | 创建作者索引任务；E 未迁移。 P:108 |
| POST | `/api/v1/projects/{project_id}/kb/search` | backend/app/api/v1/kb.py:94 `search` → `services.project_access.require_project`, `services.kb_service.search_debug` | O | 模型/向量检索尚未为协作者接通。 P:106 |
| GET | `/api/v1/projects/{project_id}/kb/settings` | backend/app/api/v1/kb.py:28 `get_settings` → `services.kb_service.get_settings` | R | 项目设置输出不含 embedding 配置 ID。 P:98 |
| PATCH | `/api/v1/projects/{project_id}/kb/settings` | backend/app/api/v1/kb.py:33 `update_settings` → `services.kb_service.update_settings` | W | 父锁后修改项目设置。 P:99 |
| GET | `/api/v1/projects/{project_id}/markers` | backend/app/api/v1/scenes.py:45 `list_markers` → `services.marker_timeline_service.list_markers` | R | S M:110 |
| DELETE | `/api/v1/projects/{project_id}/markers/{scene_id}/{marker_key}` | backend/app/api/v1/scenes.py:63 `delete_stale_marker` → `services.marker_lifecycle_service.delete_stale` | W | S M:111 |
| POST | `/api/v1/projects/{project_id}/markers/{scene_id}/{marker_key}/relink` | backend/app/api/v1/scenes.py:56 `relink_marker` → `services.marker_lifecycle_service.relink` | W | S M:112 |
| GET | `/api/v1/projects/{project_id}/memories` | backend/app/api/v1/memories.py:27 `list_memories` → `services.memory_service.list_memories` | R | 记忆按项目筛选，自动来源的一致性检查不跨书计算。 P:92 |
| POST | `/api/v1/projects/{project_id}/memories` | backend/app/api/v1/memories.py:66 `create_memory` → `services.memory_service.create_memory` | W | 父锁、本地索引；仅作者可排向量任务。 P:93 |
| POST | `/api/v1/projects/{project_id}/memories/search` | backend/app/api/v1/memories.py:75 `search_memories` → `services.project_access.require_project`, `services.memory_service.search_debug` | O | 会准备 embedding；编辑者仍被后续严格作者检查拒绝，E 未迁移。 P:96 |
| DELETE | `/api/v1/projects/{project_id}/memories/{memory_id}` | backend/app/api/v1/memories.py:93 `delete_memory` → `services.memory_service.delete_memory` | W | 父锁、项目和人工记忆限定，不排新任务。 P:95 |
| PATCH | `/api/v1/projects/{project_id}/memories/{memory_id}` | backend/app/api/v1/memories.py:84 `update_memory` → `services.memory_service.update_memory` | W | 父锁及 `get_project_memory:245` 项目限定；自动记忆仅可标记。 P:94 |
| GET | `/api/v1/projects/{project_id}/mentions` | backend/app/api/v1/mentions.py:34 `list_mentions` → `services.mentions.query.list_mentions` | R | 四来源按作品筛选；聊天额外按本人会话筛选。 P:82 |
| GET | `/api/v1/projects/{project_id}/mentions/catalog` | backend/app/api/v1/mentions.py:24 `catalog` → `services.mentions.query.catalog` | R | 作品内候选。 P:80 |
| POST | `/api/v1/projects/{project_id}/mentions/preview` | backend/app/api/v1/mentions.py:29 `preview` → `services.mentions.query.preview` | R | 用户文本本地字面匹配，不排队或调用模型。 P:81 |
| GET | `/api/v1/projects/{project_id}/mentions/sources/{source_type}/{source_id}` | backend/app/api/v1/mentions.py:90 `source_window` → `services.mentions.window.source_window` | R | 来源窗口经 `mentions/sources._statement:19` 限定项目和 source ID；聊天还要求 `AgentConversation.user_id == actor`，纯管理员不提供 chat 来源（`context.py:83`）。 P:84 |
| GET | `/api/v1/projects/{project_id}/mentions/summary` | backend/app/api/v1/mentions.py:61 `summarize` → `services.mentions.summary.summarize` | R | 项目范围及本人聊天限制同上。 P:83 |
| GET | `/api/v1/projects/{project_id}/notes` | backend/app/api/v1/notes.py:15 `list_notes` → `services.note_service.list_notes` | R | 查询项目笔记。 P:74 |
| POST | `/api/v1/projects/{project_id}/notes` | backend/app/api/v1/notes.py:31 `create_note` → `services.note_service.create_note` | W | 父锁后创建，不触发模型。 P:75 |
| DELETE | `/api/v1/projects/{project_id}/notes/{note_id}` | backend/app/api/v1/notes.py:51 `delete_note` → `services.note_service.delete_note` | W | 父锁、项目限定、revision CAS。 P:79 |
| GET | `/api/v1/projects/{project_id}/notes/{note_id}` | backend/app/api/v1/notes.py:36 `get_note` → `services.note_service.get_note` | R | `_note:13` 同时匹配作品和 ID。 P:76 |
| PATCH | `/api/v1/projects/{project_id}/notes/{note_id}` | backend/app/api/v1/notes.py:41 `update_note` → `services.note_service.update_note` | W | 父锁、项目限定、revision CAS。 P:77 |
| PUT | `/api/v1/projects/{project_id}/notes/{note_id}/flags` | backend/app/api/v1/notes.py:46 `set_note_flags` → `services.note_service.set_note_flags` | W | 父锁、项目限定；标记无 revision CAS。 P:78 |
| GET | `/api/v1/projects/{project_id}/organizations` | backend/app/api/v1/organizations.py:26 `list_orgs` → `services.organization_service.list_orgs` | R | OR M:175 |
| POST | `/api/v1/projects/{project_id}/organizations` | backend/app/api/v1/organizations.py:31 `create_org` → `services.organization_service.create_org` | W | OR M:176 |
| POST | `/api/v1/projects/{project_id}/organizations/generate` | backend/app/api/v1/organizations.py:45 `generate_org` → `services.task_service.create_task` | AI/E | 待 AI/日志代理核验 M:177 |
| POST | `/api/v1/projects/{project_id}/organizations/with-members` | backend/app/api/v1/organizations.py:36 `create_with_members` → `services.organization_service.create_with_members` | 旧作者／管理员确认入口，editor 待核 | OR M:178 |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}` | backend/app/api/v1/organizations.py:69 `delete_org` → `services.organization_service.delete_org` | W | OR M:179 |
| GET | `/api/v1/projects/{project_id}/organizations/{org_id}` | backend/app/api/v1/organizations.py:57 `get_org` → `services.organization_service.get_org` | R | OR M:180 |
| PATCH | `/api/v1/projects/{project_id}/organizations/{org_id}` | backend/app/api/v1/organizations.py:62 `update_org` → `services.organization_service.update_org` | W | OR M:181 |
| GET | `/api/v1/projects/{project_id}/organizations/{org_id}/members` | backend/app/api/v1/organizations.py:75 `list_members` → `services.organization_service.list_members` | R | OR M:182 |
| POST | `/api/v1/projects/{project_id}/organizations/{org_id}/members` | backend/app/api/v1/organizations.py:80 `add_member` → `services.organization_service.add_member` | W | OR M:183 |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}/members/{member_id}` | backend/app/api/v1/organizations.py:92 `remove_member` → `services.organization_service.remove_member` | W | OR M:184 |
| PATCH | `/api/v1/projects/{project_id}/organizations/{org_id}/members/{member_id}` | backend/app/api/v1/organizations.py:85 `update_member` → `services.organization_service.update_member` | W | OR M:185 |
| DELETE | `/api/v1/projects/{project_id}/organizations/{org_id}/membership-records/{character_id}` | backend/app/api/v1/organizations.py:124 `delete_membership_record` → `services.organization_service.delete_membership_record` | W | OR M:186 |
| GET | `/api/v1/projects/{project_id}/outline-snapshots` | backend/app/api/v1/outline_pro.py:136 `list_snapshots` → `services.outline_snapshot_service.list_snapshots` | R | 按项目列快照。 P:64 |
| POST | `/api/v1/projects/{project_id}/outline-snapshots` | backend/app/api/v1/outline_pro.py:141 `create_snapshot` → `services.outline_snapshot_service.create_manual` | W | 父锁后保存项目快照。 P:65 |
| DELETE | `/api/v1/projects/{project_id}/outline-snapshots/{snapshot_id}` | backend/app/api/v1/outline_pro.py:158 `delete_snapshot` → `services.outline_snapshot_service.delete_snapshot` | W | 父锁后按项目校验快照。 P:68 |
| GET | `/api/v1/projects/{project_id}/outline-snapshots/{snapshot_id}` | backend/app/api/v1/outline_pro.py:148 `get_snapshot` → `services.outline_snapshot_service.get_detail` | R | 快照 ID 与项目共同限定。 P:66 |
| POST | `/api/v1/projects/{project_id}/outline-snapshots/{snapshot_id}/restore` | backend/app/api/v1/outline_pro.py:153 `restore_snapshot` → `services.outline_snapshot_service.restore_snapshot` | W | 父锁、快照归属和级联预检；冲突回滚。 P:67 |
| GET | `/api/v1/projects/{project_id}/outlines` | backend/app/api/v1/story.py:45 `list_items` → `services.story_service.list_owned` | R | `list_items` 限定项目。 P:46 |
| POST | `/api/v1/projects/{project_id}/outlines` | backend/app/api/v1/story.py:51 `create_item` → `services.story_service.create_owned` | W | 新大纲属于项目并更新规划 revision。 P:47 |
| POST | `/api/v1/projects/{project_id}/outlines/append` | backend/app/api/v1/outline_pro.py:68 `append_outlines` → `services.outline_service.append_outlines` | W | 新大纲属于项目。 P:55 |
| POST | `/api/v1/projects/{project_id}/outlines/continue` | backend/app/api/v1/outline_pro.py:73 `continue_outline` → `services.task_service.create_task` | O，AI 任务 | 尚未接入协作者真实 actor；非人工 W。 P:56 |
| GET | `/api/v1/projects/{project_id}/outlines/export` | backend/app/api/v1/outline_pro.py:49 `export_outline` → `services.outline_transfer_service.export_outline` | X | 作品数据；纯管理员非协作者不获得导出。 P:52 |
| POST | `/api/v1/projects/{project_id}/outlines/import` | backend/app/api/v1/outline_pro.py:61 `import_outline` → `services.outline_transfer_service.import_outline` | W | 父锁及跨作品级联预检；拒绝时同事务回滚。 P:54 |
| POST | `/api/v1/projects/{project_id}/outlines/import/preview` | backend/app/api/v1/outline_pro.py:54 `preview_import` → `services.outline_transfer_service.preview_import` | R | 本地解析和项目角色匹配；不代表写权限。 P:53 |
| GET | `/api/v1/projects/{project_id}/outlines/tree` | backend/app/api/v1/outline_pro.py:44 `outline_tree` → `services.outline_service.outline_tree` | R | 输出当前项目大纲与细纲；默认 `manual=False` 不扩权。 P:51 |
| DELETE | `/api/v1/projects/{project_id}/outlines/{item_id}` | backend/app/api/v1/story.py:74 `delete_item` → `services.story_service.delete_owned` | W | 父锁后校验子资源，`planning_lifecycle.detach_outlines:169` 异作品反向引用匿名 409。 P:50 |
| GET | `/api/v1/projects/{project_id}/outlines/{item_id}` | backend/app/api/v1/story.py:55 `get_item` → `services.story_service.get_collaborative` | R | `_get_item:125` 同时匹配 ID 与项目。 P:48 |
| PATCH | `/api/v1/projects/{project_id}/outlines/{item_id}` | backend/app/api/v1/story.py:61 `update_item` → `services.story_service.update_owned` | W | 父锁后 `_get_item`。 P:49 |
| POST | `/api/v1/projects/{project_id}/outlines/{outline_id}/expand` | backend/app/api/v1/outline_pro.py:92 `expand_outline` → `services.task_service.create_task` | O，AI 任务 | 不能凭 W 推断可代作者创建任务。 P:58 |
| POST | `/api/v1/projects/{project_id}/outlines/{outline_id}/move` | backend/app/api/v1/outline_pro.py:85 `move_outline` → `services.outline_service.move_outline` | W | 父锁后人工大纲校验。 P:57 |
| GET | `/api/v1/projects/{project_id}/outlines/{outline_id}/plans` | backend/app/api/v1/outline_pro.py:109 `list_plans` → `services.plan_service.list_plans` | R | 大纲和计划同作品。 P:59 |
| PUT | `/api/v1/projects/{project_id}/outlines/{outline_id}/plans` | backend/app/api/v1/outline_pro.py:114 `sync_plans` → `services.plan_service.sync_plans` | W | 锁后大纲、细纲、章节和反向引用预检；可能隐式删空章时纯协作者先取发送写门禁并检查他人任务。 P:60 |
| POST | `/api/v1/projects/{project_id}/outlines/{outline_id}/plans/build` | backend/app/api/v1/outline_pro.py:126 `build_chapters` → `services.plan_service.build_chapters` | W | 父锁、大纲与绑定目标校验。 P:62 |
| PATCH | `/api/v1/projects/{project_id}/outlines/{outline_id}/plans/{plan_id}` | backend/app/api/v1/outline_pro.py:119 `update_plan` → `services.plan_service.update_plan` | W | 大纲、计划和人物限定项目。 P:61 |
| GET | `/api/v1/projects/{project_id}/planning/crossings` | backend/app/api/v1/planning.py:224 `crossings` → `services.planning_service.list_crossings` | R | 可选支线及来源校验项目。 P:34 |
| POST | `/api/v1/projects/{project_id}/planning/crossings` | backend/app/api/v1/planning.py:243 `create_crossing` → `services.planning_service.save_crossing` | W | 来源和参与支线校验项目。 P:35 |
| DELETE | `/api/v1/projects/{project_id}/planning/crossings/{crossing_id}` | backend/app/api/v1/planning.py:255 `delete_crossing` → `services.planning_service.delete_crossing` | W | `_crossing` 限定项目。 P:37 |
| PATCH | `/api/v1/projects/{project_id}/planning/crossings/{crossing_id}` | backend/app/api/v1/planning.py:248 `update_crossing` → `services.planning_service.save_crossing` | W | 交汇与来源归项目。 P:36 |
| GET | `/api/v1/projects/{project_id}/planning/detached` | backend/app/api/v1/planning.py:122 `detached` → `services.planning_service.list_detached` | R | 仅项目下游离规划。 P:23 |
| GET | `/api/v1/projects/{project_id}/planning/entries` | backend/app/api/v1/planning.py:76 `entries` → `services.planning_service.list_entries` | R | 作品条目；单书协作者无系列读取权时不显示 `series_shared`，只保留匿名标志，参见 `:366`。 P:18 |
| GET | `/api/v1/projects/{project_id}/planning/matrix` | backend/app/api/v1/planning.py:37 `matrix` → `services.planning_service.matrix` | R | `query.rows/entries` 与候选限定作品；一致性快照。 P:17 |
| GET | `/api/v1/projects/{project_id}/planning/subplots` | backend/app/api/v1/planning.py:163 `subplots` → `services.planning_service.list_subplots` | R | 项目支线。 P:28 |
| POST | `/api/v1/projects/{project_id}/planning/subplots` | backend/app/api/v1/planning.py:175 `create_subplot` → `services.planning_service.save_subplot` | W | 新支线绑定项目。 P:29 |
| DELETE | `/api/v1/projects/{project_id}/planning/subplots/{subplot_id}` | backend/app/api/v1/planning.py:187 `delete_subplot` → `services.planning_service.delete_subplot` | W | `_subplot` 限定项目。 P:31 |
| PATCH | `/api/v1/projects/{project_id}/planning/subplots/{subplot_id}` | backend/app/api/v1/planning.py:180 `update_subplot` → `services.planning_service.save_subplot` | W | `_subplot` 限定项目。 P:30 |
| DELETE | `/api/v1/projects/{project_id}/planning/subplots/{subplot_id}/nodes/{source_kind}/{source_id}` | backend/app/api/v1/planning.py:209 `delete_node` → `services.planning_service.delete_node` | W | 支线和来源均校验项目。 P:33 |
| PUT | `/api/v1/projects/{project_id}/planning/subplots/{subplot_id}/nodes/{source_kind}/{source_id}` | backend/app/api/v1/planning.py:194 `put_node` → `services.planning_service.put_node` | W | 支线和来源均校验项目。 P:32 |
| GET | `/api/v1/projects/{project_id}/planning/tags` | backend/app/api/v1/planning.py:134 `tags` → `services.planning_service.list_tags` | R | 项目标签。 P:24 |
| POST | `/api/v1/projects/{project_id}/planning/tags` | backend/app/api/v1/planning.py:146 `create_tag` → `services.planning_service.save_tag` | W | 新标签绑定项目。 P:25 |
| DELETE | `/api/v1/projects/{project_id}/planning/tags/{tag_id}` | backend/app/api/v1/planning.py:156 `delete_tag` → `services.planning_service.delete_tag` | W | `_tag` 限定项目；revision。 P:27 |
| PATCH | `/api/v1/projects/{project_id}/planning/tags/{tag_id}` | backend/app/api/v1/planning.py:151 `update_tag` → `services.planning_service.save_tag` | W | `_tag` 限定项目。 P:26 |
| PATCH | `/api/v1/projects/{project_id}/planning/units/batch` | backend/app/api/v1/planning.py:92 `batch` → `services.planning_service.batch_update` | W | 来源与引用先在父作品中解析和验证。 P:19 |
| GET | `/api/v1/projects/{project_id}/planning/units/context-preview` | backend/app/api/v1/planning.py:107 `context_preview` → `services.planning_context_service.manual_context_preview` | R，本地规划预览；已由 D 专项验证 | project_id + source_kind/source_id；P:22 旧 O 过期，见 phase-d-planning-preview.md |
| POST | `/api/v1/projects/{project_id}/planning/units/merge` | backend/app/api/v1/planning.py:97 `merge` → `services.planning_service.merge_units` | W | 两个单元及其来源同作品。 P:20 |
| POST | `/api/v1/projects/{project_id}/planning/units/{unit_id}/binding` | backend/app/api/v1/planning.py:102 `binding` → `services.planning_service.bind_unit` | W | 单元和目标来源同作品。 P:21 |
| GET | `/api/v1/projects/{project_id}/planning/weave` | backend/app/api/v1/planning.py:262 `weave` → `services.planning_service.weave` | R | 项目行、支线与关联位置。 P:38 |
| GET | `/api/v1/projects/{project_id}/reader/chapters/{chapter_id}` | backend/app/api/v1/reader.py:21 `reader_chapter` → `services.reader_service.chapter` | R | C M:274 |
| GET | `/api/v1/projects/{project_id}/reader/toc` | backend/app/api/v1/reader.py:16 `reader_toc` → `services.reader_service.toc` | R | C M:275 |
| PUT | `/api/v1/projects/{project_id}/reading-progress` | backend/app/api/v1/reader.py:10 `save_reading_progress` → `services.reader_service.save_progress` | 作者 | C M:276 |
| GET | `/api/v1/projects/{project_id}/relationship-graph` | backend/app/api/v1/relationships.py:106 `graph` → `services.relationship_service.graph` | R | RL M:192 |
| GET | `/api/v1/projects/{project_id}/relationship-history` | backend/app/api/v1/relationships.py:95 `list_pair_history` → `services.relationship_service.list_pair_history` | R | RL M:193 |
| GET | `/api/v1/projects/{project_id}/relationship-types` | backend/app/api/v1/relationships.py:24 `list_types` → `services.relationship_service.list_types` | R | RL M:194 |
| POST | `/api/v1/projects/{project_id}/relationship-types` | backend/app/api/v1/relationships.py:29 `create_type` → `services.relationship_service.create_type` | W | RL M:195 |
| DELETE | `/api/v1/projects/{project_id}/relationship-types/{type_id}` | backend/app/api/v1/relationships.py:36 `delete_type` → `services.relationship_service.delete_type` | W | RL M:196 |
| GET | `/api/v1/projects/{project_id}/relationships` | backend/app/api/v1/relationships.py:42 `list_relationships` → `services.relationship_service.list_relationships` | R | RL M:197 |
| POST | `/api/v1/projects/{project_id}/relationships` | backend/app/api/v1/relationships.py:49 `create_relationship` → `services.relationship_service.create_relationship` | W | RL M:198 |
| POST | `/api/v1/projects/{project_id}/relationships/batch` | backend/app/api/v1/relationships.py:56 `create_relationships` → `services.relationship_service.create_relationships` | 旧作者／管理员确认入口，editor 待核 | RL M:199 |
| POST | `/api/v1/projects/{project_id}/relationships/generate` | backend/app/api/v1/relationships.py:63 `generate_relationships` → `services.task_service.create_task` | AI/E | 待 AI/日志代理核验 M:200 |
| DELETE | `/api/v1/projects/{project_id}/relationships/{rel_id}` | backend/app/api/v1/relationships.py:78 `delete_relationship` → `services.relationship_service.delete_relationship` | W | RL M:201 |
| PATCH | `/api/v1/projects/{project_id}/relationships/{rel_id}` | backend/app/api/v1/relationships.py:71 `update_relationship` → `services.relationship_service.update_relationship` | W | RL M:202 |
| GET | `/api/v1/projects/{project_id}/relationships/{rel_id}/history` | backend/app/api/v1/relationships.py:90 `list_history` → `services.relationship_service.list_history` | R | RL M:203 |
| GET | `/api/v1/projects/{project_id}/scenes` | backend/app/api/v1/scenes.py:73 `project_scenes` → `services.scene_service.list_scenes` | R | S M:113 |
| GET | `/api/v1/projects/{project_id}/setting-categories` | backend/app/api/v1/setting_categories.py:29 `categories` → `services.setting_category_service.list_categories` | R | SC M:230 |
| POST | `/api/v1/projects/{project_id}/setting-categories` | backend/app/api/v1/setting_categories.py:45 `create_category` → `services.setting_category_service.save_category` | W | SC M:231 |
| POST | `/api/v1/projects/{project_id}/setting-categories/preview` | backend/app/api/v1/setting_categories.py:51 `preview` → `services.setting_category_query_service.query_category_entries` | R | SC M:232 |
| DELETE | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | backend/app/api/v1/setting_categories.py:74 `delete_category` → `services.setting_category_service.delete_category` | W | SC M:233 |
| GET | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | backend/app/api/v1/setting_categories.py:64 `category` → `services.setting_category_service.read_category` | R | SC M:234 |
| PATCH | `/api/v1/projects/{project_id}/setting-categories/{category_id}` | backend/app/api/v1/setting_categories.py:69 `update_category` → `services.setting_category_service.save_category` | W | SC M:235 |
| GET | `/api/v1/projects/{project_id}/setting-categories/{category_id}/entries` | backend/app/api/v1/setting_categories.py:87 `entries` → `services.setting_category_query_service.query_category_entries` | R | SC M:236 |
| GET | `/api/v1/projects/{project_id}/setting-entries` | backend/app/api/v1/setting_fields.py:90 `entries` → `services.setting_catalog_service.list_entries` | R | SF M:209 |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/ai-settings` | backend/app/api/v1/setting_fields.py:105 `ai_settings` → `services.setting_ai_visibility.read_settings` | R，AI 可见性配置人工读 | M:210；SF 条目同作品，见 phase-d-visibility.md |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/ai-settings` | backend/app/api/v1/setting_fields.py:110 `update_ai_settings` → `services.setting_ai_visibility.write_settings` | W，AI 可见性配置人工写 | M:211；SF 条目同作品，见 phase-d-visibility.md |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields` | backend/app/api/v1/setting_fields.py:117 `fields` → `services.setting_field_service.read_entry_fields` | R | SF M:212 |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields/{definition_id}/baseline` | backend/app/api/v1/setting_fields.py:135 `baseline` → `services.setting_field_service.write_baseline` | W | SF M:213 |
| POST | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/fields/{definition_id}/progressions` | backend/app/api/v1/setting_fields.py:148 `progression` → `services.setting_field_service.create_field_progression` | W | SF M:214 |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/legacy-fields/{field_id}/ai-visibility` | backend/app/api/v1/setting_fields.py:178 `legacy_ai_visibility` → `services.setting_field_service.write_legacy_visibility` | W，AI 可见性配置人工写 | M:215；SF 字段同作品，见 phase-d-visibility.md |
| POST | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/legacy-fields/{field_id}/binding` | backend/app/api/v1/setting_fields.py:165 `binding` → `services.setting_field_service.bind_legacy` | W，AI 可见性配置人工写 | M:216；SF 字段同作品，见 phase-d-visibility.md |
| GET | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/tags` | backend/app/api/v1/setting_categories.py:121 `tags` → `services.setting_category_service.read_tags` | R | SC M:237 |
| PATCH | `/api/v1/projects/{project_id}/setting-entries/{kind}/{entry_id}/tags` | backend/app/api/v1/setting_categories.py:128 `update_tags` → `services.setting_category_service.save_tags` | W | SC M:238 |
| GET | `/api/v1/projects/{project_id}/setting-entry-tags` | backend/app/api/v1/setting_categories.py:135 `suggestions` → `services.setting_category_service.suggest_tags` | R | SC M:239 |
| GET | `/api/v1/projects/{project_id}/setting-field-definitions` | backend/app/api/v1/setting_fields.py:20 `definitions` → `services.setting_field_service.list_definitions` | R | SF M:217 |
| POST | `/api/v1/projects/{project_id}/setting-field-definitions` | backend/app/api/v1/setting_fields.py:33 `create_definition` → `services.setting_field_service.save_definition` | W，含 AI 可见性配置 | SF M:218；见 phase-d-visibility.md |
| DELETE | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | backend/app/api/v1/setting_fields.py:48 `delete_definition` → `services.setting_field_service.delete_definition` | W | SF M:219 |
| GET | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | backend/app/api/v1/setting_fields.py:38 `definition` → `services.setting_field_service.read_definition` | R | SF M:220 |
| PATCH | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}` | backend/app/api/v1/setting_fields.py:43 `update_definition` → `services.setting_field_service.save_definition` | W，含 AI 可见性配置 | SF M:221；见 phase-d-visibility.md |
| POST | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options` | backend/app/api/v1/setting_fields.py:59 `create_option` → `services.setting_field_service.save_option` | W | SF M:222 |
| DELETE | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options/{option_id}` | backend/app/api/v1/setting_fields.py:76 `delete_option` → `services.setting_field_service.delete_option` | W | SF M:223 |
| PATCH | `/api/v1/projects/{project_id}/setting-field-definitions/{definition_id}/options/{option_id}` | backend/app/api/v1/setting_fields.py:64 `update_option` → `services.setting_field_service.save_option` | W | SF M:224 |
| GET | `/api/v1/projects/{project_id}/setting-timelines` | backend/app/api/v1/progressions.py:34 `list_timelines` → `services.setting_progression_service.list_timelines` | R | TL M:245 |
| POST | `/api/v1/projects/{project_id}/setting-timelines` | backend/app/api/v1/progressions.py:60 `create_timeline` → `services.setting_progression_service.create_timeline` | W | TL M:246 |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/baseline` | backend/app/api/v1/progressions.py:65 `read_baseline` → `services.setting_progression_service.read_baseline` | R | TL M:247 |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/baseline` | backend/app/api/v1/progressions.py:70 `update_baseline` → `services.setting_progression_service.update_baseline` | W | TL M:248 |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/effective` | backend/app/api/v1/progressions.py:145 `read_effective` → `services.setting_progression_service.read_effective` | R | TL M:249 |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions` | backend/app/api/v1/progressions.py:77 `list_progressions` → `services.setting_progression_service.list_progressions` | R | TL M:250 |
| POST | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions` | backend/app/api/v1/progressions.py:102 `create_progression` → `services.setting_progression_service.create_progression` | W | TL M:251 |
| DELETE | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | backend/app/api/v1/progressions.py:123 `delete_progression` → `services.setting_progression_service.delete_progression` | W | TL M:252 |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | backend/app/api/v1/progressions.py:109 `read_progression` → `services.setting_progression_service.read_progression` | R | TL M:253 |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/progressions/{event_id}` | backend/app/api/v1/progressions.py:116 `update_progression` → `services.setting_progression_service.update_progression` | W | TL M:254 |
| GET | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields` | backend/app/api/v1/progressions.py:162 `list_text_fields` → `services.setting_progression_service.list_text_fields` | R | TL M:255 |
| POST | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields` | backend/app/api/v1/progressions.py:167 `create_text_field` → `services.setting_progression_service.create_text_field` | W | TL M:256 |
| DELETE | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields/{field_id}` | backend/app/api/v1/progressions.py:181 `delete_text_field` → `services.setting_progression_service.delete_text_field` | W | TL M:257 |
| PATCH | `/api/v1/projects/{project_id}/setting-timelines/{timeline_id}/text-fields/{field_id}` | backend/app/api/v1/progressions.py:174 `update_text_field` → `services.setting_progression_service.update_text_field` | W | TL M:258 |
| PATCH | `/api/v1/projects/{project_id}/wizard-progress` | backend/app/api/v1/projects.py:36 `update_wizard_progress` → `services.project_service.update_wizard_progress`, `services.project_service.project_out` | 作者 | P M:57 |
| POST | `/api/v1/projects/{project_id}/wizard/{kind}` | backend/app/api/v1/wizard.py:13 `run_wizard` → `services.task_service.create_task` | 作品向导 AI 任务；E 未接通 | project_id → task_service.create_task → handler；真实 actor 与受控出站待集成 |
| GET | `/api/v1/projects/{project_id}/world` | backend/app/api/v1/story.py:37 `list_world` → `services.series_read.read_book_settings` | R | ST M:129 |
| POST | `/api/v1/projects/{project_id}/world` | backend/app/api/v1/story.py:51 `create_item` → `services.story_service.create_owned` | W | ST M:130 |
| DELETE | `/api/v1/projects/{project_id}/world/{item_id}` | backend/app/api/v1/story.py:74 `delete_item` → `services.story_service.delete_owned` | W | ST M:131 |
| GET | `/api/v1/projects/{project_id}/world/{item_id}` | backend/app/api/v1/story.py:55 `get_item` → `services.series_read.read_book_settings` | R | ST M:132 |
| PATCH | `/api/v1/projects/{project_id}/world/{item_id}` | backend/app/api/v1/story.py:61 `update_item` → `services.story_service.update_owned`, `services.series_read.read_book_settings` | W | ST M:133 |
| GET | `/api/v1/prompt-components` | backend/app/api/v1/prompt_components.py:21 `list_components` → `services.prompt_component_service.list_components` | 本人提示词组件 | component_id 按 prompt_component_service 用户规则 |
| POST | `/api/v1/prompt-components` | backend/app/api/v1/prompt_components.py:28 `create_component` → `services.prompt_component_service.create_component` | 本人提示词组件 | component_id 按 prompt_component_service 用户规则 |
| GET | `/api/v1/prompt-components/export` | backend/app/api/v1/prompt_components.py:33 `export_components` → `services.prompt_component_service.export_components` | 本人提示词组件 | component_id 按 prompt_component_service 用户规则 |
| POST | `/api/v1/prompt-components/import` | backend/app/api/v1/prompt_components.py:38 `import_components` → `services.prompt_component_service.import_components` | 本人提示词组件 | component_id 按 prompt_component_service 用户规则 |
| DELETE | `/api/v1/prompt-components/{component_id}` | backend/app/api/v1/prompt_components.py:53 `delete_component` → `services.prompt_component_service.delete_component` | 本人提示词组件 | component_id 按 prompt_component_service 用户规则 |
| GET | `/api/v1/prompt-components/{component_id}` | backend/app/api/v1/prompt_components.py:43 `get_component` → `services.prompt_component_service.to_out`, `services.prompt_component_service.get_component` | 本人提示词组件 | component_id 按 prompt_component_service 用户规则 |
| PUT | `/api/v1/prompt-components/{component_id}` | backend/app/api/v1/prompt_components.py:48 `update_component` → `services.prompt_component_service.update_component` | 本人提示词组件 | component_id 按 prompt_component_service 用户规则 |
| GET | `/api/v1/prompt-templates` | backend/app/api/v1/prompt_templates.py:24 `list_templates` → `services.prompt_service.list_templates` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| GET | `/api/v1/prompt-templates/export` | backend/app/api/v1/prompt_templates.py:29 `export_templates` → `services.prompt_service.export_overrides` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| POST | `/api/v1/prompt-templates/import` | backend/app/api/v1/prompt_templates.py:34 `import_templates` → `services.prompt_service.import_overrides` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| DELETE | `/api/v1/prompt-templates/{key}` | backend/app/api/v1/prompt_templates.py:54 `reset_template` → `services.prompt_service.delete_override` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| GET | `/api/v1/prompt-templates/{key}` | backend/app/api/v1/prompt_templates.py:39 `get_template` → `services.prompt_service.get_template` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| PATCH | `/api/v1/prompt-templates/{key}` | backend/app/api/v1/prompt_templates.py:49 `patch_template` → `services.prompt_service.patch_override` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| PUT | `/api/v1/prompt-templates/{key}` | backend/app/api/v1/prompt_templates.py:44 `save_template` → `services.prompt_service.save_override` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| POST | `/api/v1/prompt-templates/{key}/preview` | backend/app/api/v1/prompt_templates.py:70 `preview_template` → `services.prompt_service.preview` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| GET | `/api/v1/prompt-templates/{key}/versions` | backend/app/api/v1/prompt_templates.py:60 `list_versions` → `services.prompt_service.list_versions` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| POST | `/api/v1/prompt-templates/{key}/versions/{version_id}/restore` | backend/app/api/v1/prompt_templates.py:65 `restore_version` → `services.prompt_service.restore_version` | 系统默认模板／本人覆盖及版本 | key + version_id 按 prompt_service 用户规则；模板预览为其自身能力 |
| GET | `/api/v1/series` | backend/app/api/v1/series.py:16 `list_series` → `services.series_service.list_series` | 系列读：本人作者或已接受系列成员；书籍投影另核验 | 当前用户的作者／已接受系列；创建时由当前用户成为系列作者；F 专项 |
| POST | `/api/v1/series` | backend/app/api/v1/series.py:21 `create_series` → `services.series_service.create` | 系列创建：当前用户为系列作者 | 当前用户的作者／已接受系列；创建时由当前用户成为系列作者；F 专项 |
| DELETE | `/api/v1/series/{sid}` | backend/app/api/v1/series.py:36 `delete_series` → `services.series_service.delete_series` | 系列作者管理 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| GET | `/api/v1/series/{sid}` | backend/app/api/v1/series.py:26 `read_series` → `services.series_service.detail` | 系列读：作品列表另核验 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| PATCH | `/api/v1/series/{sid}` | backend/app/api/v1/series.py:31 `rename_series` → `services.series_service.rename` | 系列作者管理 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| POST | `/api/v1/series/{sid}/books` | backend/app/api/v1/series.py:42 `join_book` → `services.series_service.join` | 系列作者管理／书籍成员调整 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| DELETE | `/api/v1/series/{sid}/books/{pid}` | backend/app/api/v1/series.py:47 `leave_book` → `services.series_service.leave` | 系列作者管理／书籍成员调整 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| GET | `/api/v1/series/{sid}/collaborators` | backend/app/api/v1/collaboration.py:59 `series_members` → `services.collaboration_service.list_members` | 系列作者成员管理 | project_id / sid → 父资源；成员由 user_id 和父资源共同限定 |
| POST | `/api/v1/series/{sid}/collaborators` | backend/app/api/v1/collaboration.py:64 `invite_series` → `services.collaboration_service.invite` | 系列作者成员管理 | project_id / sid → 父资源；成员由 user_id 和父资源共同限定 |
| DELETE | `/api/v1/series/{sid}/collaborators/{user_id}` | backend/app/api/v1/collaboration.py:71 `revoke_series_member` → `services.collaboration_service.change_member` | 系列作者成员管理 | project_id / sid → 父资源；成员由 user_id 和父资源共同限定 |
| GET | `/api/v1/series/{sid}/entries` | backend/app/api/v1/series.py:52 `list_entries` → `services.series_service.list_entries` | 系列读 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| POST | `/api/v1/series/{sid}/entries` | backend/app/api/v1/series.py:65 `create_entry` → `services.series_service.create_entry` | 系列作者或已接受系列编辑者写 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| POST | `/api/v1/series/{sid}/entries/promote` | backend/app/api/v1/series.py:70 `promote_entry` → `services.series_service.promote` | 系列作者，或系列编辑且全部受影响书为已接受 editor | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| DELETE | `/api/v1/series/{sid}/entries/{eid}` | backend/app/api/v1/series.py:87 `delete_entry` → `services.series_service.delete_entry` | 系列作者或已接受系列编辑者写；受绑定约束 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| GET | `/api/v1/series/{sid}/entries/{eid}` | backend/app/api/v1/series.py:75 `read_entry` → `services.series_service.read_entry` | 系列读 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| PATCH | `/api/v1/series/{sid}/entries/{eid}` | backend/app/api/v1/series.py:80 `patch_entry` → `services.series_service.patch_entry` | 系列作者或已接受系列编辑者写；传播约束 | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| POST | `/api/v1/series/{sid}/entries/{eid}/move-to-book` | backend/app/api/v1/series.py:108 `move_entry` → `services.series_service.move_entry` | 系列作者，或系列编辑且全部受影响书为已接受 editor | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| POST | `/api/v1/series/{sid}/entries/{eid}/uses` | backend/app/api/v1/series.py:94 `use_entry` → `services.series_service.use_entry` | 系列作者，或系列编辑且全部受影响书为已接受 editor | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| DELETE | `/api/v1/series/{sid}/entries/{eid}/uses/{pid}` | backend/app/api/v1/series.py:101 `unuse_entry` → `services.series_service.unuse` | 系列作者，或系列编辑且全部受影响书为已接受 editor | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| GET | `/api/v1/series/{sid}/impact` | backend/app/api/v1/series.py:115 `impact` → `services.series_service.impact` | 系列读；按动作核验系列作者，或系列编辑及全部受影响书 editor | sid → 系列；条目按 eid + sid；需跨书动作复验全部受影响作品；F 专项 |
| GET | `/api/v1/skills` | backend/app/api/v1/skills.py:24 `list_skills` → `services.skill_service.list_skills` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| POST | `/api/v1/skills` | backend/app/api/v1/skills.py:31 `create_skill` → `services.skill_service.create_skill` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| POST | `/api/v1/skills/import` | backend/app/api/v1/skills.py:36 `import_skill` → `services.skill_service.import_skill`, `services.skill_service.reject_oversized_import` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| DELETE | `/api/v1/skills/{skill_id}` | backend/app/api/v1/skills.py:59 `delete_skill` → `services.skill_service.delete_skill` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| GET | `/api/v1/skills/{skill_id}` | backend/app/api/v1/skills.py:49 `get_skill` → `services.skill_service.to_out`, `services.skill_service.get_visible_skill` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| PATCH | `/api/v1/skills/{skill_id}` | backend/app/api/v1/skills.py:54 `update_skill` → `services.skill_service.update_skill` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| POST | `/api/v1/skills/{skill_id}/copy` | backend/app/api/v1/skills.py:76 `copy_skill` → `services.skill_service.copy_skill` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| GET | `/api/v1/skills/{skill_id}/export` | backend/app/api/v1/skills.py:86 `export_skill` → `services.skill_service.export_skill` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| POST | `/api/v1/skills/{skill_id}/preview` | backend/app/api/v1/skills.py:70 `preview_skill` → `services.skill_service.preview` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| PUT | `/api/v1/skills/{skill_id}/references` | backend/app/api/v1/skills.py:65 `replace_references` → `services.skill_service.replace_references` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| POST | `/api/v1/skills/{skill_id}/restore-builtin` | backend/app/api/v1/skills.py:81 `restore_builtin` → `services.skill_service.restore_builtin` | 用户 Skill／可见内置项 | skill_id 按 skill_service 归属及内置规则；非作品协作授权 |
| GET | `/api/v1/styles` | backend/app/api/v1/styles.py:24 `list_styles` → `services.style_service.list_styles` | 用户风格／可见内置预设 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| POST | `/api/v1/styles` | backend/app/api/v1/styles.py:29 `create_style` → `services.style_service.create_style` | 用户风格／可见内置预设 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| GET | `/api/v1/styles/export` | backend/app/api/v1/styles.py:34 `export_styles` → `services.style_service.export_styles` | 用户风格／可见内置预设 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| POST | `/api/v1/styles/extract` | backend/app/api/v1/styles.py:44 `extract_style` → `services.style_service.prepare_extract`, `services.style_service.run_extract` | 本人风格 AI 提取／试写，可能模型出站 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| POST | `/api/v1/styles/import` | backend/app/api/v1/styles.py:39 `import_styles` → `services.style_service.import_styles` | 用户风格／可见内置预设 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| POST | `/api/v1/styles/trial` | backend/app/api/v1/styles.py:66 `trial_style` → `services.style_service.prepare_trial`, `services.style_service.run_trial` | 本人风格 AI 提取／试写，可能模型出站 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| DELETE | `/api/v1/styles/{style_id}` | backend/app/api/v1/styles.py:98 `delete_style` → `services.style_service.delete_style` | 用户风格／可见内置预设 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| GET | `/api/v1/styles/{style_id}` | backend/app/api/v1/styles.py:88 `get_style` → `services.style_service.get_style` | 用户风格／可见内置预设 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| PATCH | `/api/v1/styles/{style_id}` | backend/app/api/v1/styles.py:93 `update_style` → `services.style_service.update_style` | 用户风格／可见内置预设 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| POST | `/api/v1/styles/{style_id}/copy` | backend/app/api/v1/styles.py:104 `copy_style` → `services.style_service.copy_style` | 用户风格／可见内置预设 | style_id 按 style_service 可见性；与项目作者私有风格隔离 |
| DELETE | `/api/v1/tasks` | backend/app/api/v1/tasks.py:34 `clear_finished` → `services.task_service.clear_finished` | T | 仅本人可见已结束记录，按可访问项目锁定后清理。 P:156 |
| GET | `/api/v1/tasks` | backend/app/api/v1/tasks.py:18 `list_tasks` → `services.task_service.list_tasks` | T | SQL 先过滤本人和项目有效性再分页，并在投影后再次验证。 P:155 |
| DELETE | `/api/v1/tasks/{task_id}` | backend/app/api/v1/tasks.py:109 `delete_task` → `services.task_service.delete_task` | T | 本人项目与可选提案身份；不能假设管理员可删除他人任务。 P:164 |
| GET | `/api/v1/tasks/{task_id}` | backend/app/api/v1/tasks.py:43 `get_task` → `services.task_service.get_task` | T | 本人及项目授权，私人引用投影。 P:157 |
| POST | `/api/v1/tasks/{task_id}/cancel` | backend/app/api/v1/tasks.py:71 `cancel_task` → `services.task_service.cancel_task` | T | 锁内校验本人和当前项目授权；活动轮次须有效。 P:160 |
| POST | `/api/v1/tasks/{task_id}/finish` | backend/app/api/v1/tasks.py:88 `finish_task` → `services.task_service.finish_paused_task` | T | 当前授权及提案身份校验。 P:163 |
| GET | `/api/v1/tasks/{task_id}/output` | backend/app/api/v1/tasks.py:48 `get_output` → `services.task_service.get_output` | T | 本人、task key 和授权复验。 P:158 |
| POST | `/api/v1/tasks/{task_id}/resume` | backend/app/api/v1/tasks.py:83 `resume_task` → `services.task_service.resume_task` | T | 需有效活动授权，重验 handler 和富文档待审核条件。 P:162 |
| POST | `/api/v1/tasks/{task_id}/retry` | backend/app/api/v1/tasks.py:78 `retry_task` → `services.task_service.retry_task` | T + O 受理 | 先锁本人项目授权，当前通用创建后续仍严格作者。 P:161 |
| GET | `/api/v1/tasks/{task_id}/rich-proposals/{token}` | backend/app/api/v1/tasks.py:95 `get_rich_proposal` → `services.rich_document_review.get_review` | T | task key、run key、token 必须匹配本人可见任务。 P:165 |
| POST | `/api/v1/tasks/{task_id}/rich-proposals/{token}/accept` | backend/app/api/v1/tasks.py:102 `accept_rich_proposal` → `services.rich_document_review.accept_review` | T + O | 接受前仍严格作者作品/章节，不能称协作者提案采纳已接通。 P:166 |
| GET | `/api/v1/tasks/{task_id}/stream` | backend/app/api/v1/tasks.py:53 `stream_task` → `services.collaboration_task_access.task_grant_identity`, `services.task_service.get_owned_task` | T | 原活动状态及授权轮次在流中复验。 P:159 |
| POST | `/api/v1/workshop/admin/items` | backend/app/api/v1/workshop.py:115 `publish` → `services.workshop_service.submit`, `services.workshop_service.get_item` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| DELETE | `/api/v1/workshop/admin/items/{item_id}` | backend/app/api/v1/workshop.py:128 `delete_item` → `services.workshop_service.delete_item` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| PATCH | `/api/v1/workshop/admin/items/{item_id}` | backend/app/api/v1/workshop.py:121 `patch_metadata` → `services.workshop_service.patch_metadata` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/workshop/admin/submissions` | backend/app/api/v1/workshop.py:90 `review_list` → `services.workshop_service.list_submissions` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/workshop/admin/submissions/{submission_id}/approve` | backend/app/api/v1/workshop.py:101 `approve` → `services.workshop_service.approve` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| POST | `/api/v1/workshop/admin/submissions/{submission_id}/reject` | backend/app/api/v1/workshop.py:108 `reject` → `services.workshop_service.reject` | 管理员原 RBAC 指定权限；不当然授予个人任务／AI | 按原管理接口依赖与目标保护；非项目协作旁路 |
| GET | `/api/v1/workshop/items` | backend/app/api/v1/workshop.py:36 `list_items` → `services.workshop_service.list_items` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| GET | `/api/v1/workshop/items/{item_id}` | backend/app/api/v1/workshop.py:134 `detail` → `services.workshop_service.get_item` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| POST | `/api/v1/workshop/items/{item_id}/hide` | backend/app/api/v1/workshop.py:139 `hide` → `services.workshop_service.set_visibility` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| POST | `/api/v1/workshop/items/{item_id}/import` | backend/app/api/v1/workshop.py:159 `import_item` → `services.workshop_service.import_item` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| DELETE | `/api/v1/workshop/items/{item_id}/like` | backend/app/api/v1/workshop.py:154 `unlike` → `services.workshop_service.like` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| POST | `/api/v1/workshop/items/{item_id}/like` | backend/app/api/v1/workshop.py:149 `like` → `services.workshop_service.like` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| POST | `/api/v1/workshop/items/{item_id}/unhide` | backend/app/api/v1/workshop.py:144 `unhide` → `services.workshop_service.set_visibility` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| GET | `/api/v1/workshop/mine/items` | backend/app/api/v1/workshop.py:63 `my_items` → `services.workshop_service.list_items` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| GET | `/api/v1/workshop/submissions` | backend/app/api/v1/workshop.py:68 `my_submissions` → `services.workshop_service.list_submissions` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| POST | `/api/v1/workshop/submissions` | backend/app/api/v1/workshop.py:75 `submit` → `services.workshop_service.submit` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| GET | `/api/v1/workshop/submissions/{submission_id}` | backend/app/api/v1/workshop.py:80 `get_submission` → `services.workshop_service.get_submission` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| POST | `/api/v1/workshop/submissions/{submission_id}/withdraw` | backend/app/api/v1/workshop.py:85 `withdraw` → `services.workshop_service.withdraw` | 创作工坊账户／本人提交或公共条目 | submission_id / item_id 按 workshop_service 规则；不继承作品协作权限 |
| GET | `/healthz` | backend/app/main.py:220 `healthz` → `API 内健康响应` | 公开健康检查 | 无项目 |


## Caveats / Not Found

- 本次仅做源码阅读和无数据库路由扫描，没有运行单项或大型测试，没有运行真实模型。已列各专项测试均引用既有阶段文件，未复现，也不能外推到所有 479 项。
- 直接调用提取是定位索引，复杂委托的深层调用、注入依赖、序列化、异步竞争和持锁写入必须沿对应源码及专项记录复核。旧表仍保留历史错误；本文件明确覆盖的差异优先以当前函数和最新专项为准。
- A1 的逐键对账已经记录，但并行修改、部分入口的行为与高风险权限尚未做最终独立测试；这不是 Gate A、D、E 或最终验收通过的声明。
