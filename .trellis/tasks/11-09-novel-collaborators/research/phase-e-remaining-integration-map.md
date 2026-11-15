# Research: E1–E7 尚未闭环的协作 AI 接入图

- Query: 依据当前代码逐条梳理 accepted editor 的章节/场景、向导、实体、大纲、分析、批量、知识库/记忆、封面、Agent 及自动派生链，定位真实 actor、原授权轮次、模型选择、出站、结果、重试和清理尚缺的接口，给出可协调的实施顺序与必须使用真实 PostgreSQL 的验证。
- Scope: internal；只读静态调查，2026-11-10 当前树快照。`analysis_service.py`、`cover_service.py`、任务和 Agent 文件当时仍有并行修改，行号及结论须在分配前重新比对最终代码。
- Date: 2026-11-10

## Findings

### 文件与证据口径

- `.trellis/tasks/11-09-novel-collaborators/prd.md`、`design.md`、`implement.md`：E1–E7 的模型来源、真实 actor、可见性、共享向量、撤权与并发验收合同；Gate E 仍未完成。
- `.trellis/workflow.md`、`.trellis/spec/backend/index.md`、`.trellis/spec/backend/rbac-quotas.md`：保持既有作者 helper、管理员权限和配额边界；后台引擎沿现有单进程及排空旧执行器前提，不推断跨进程恢复已经完成。
- `backend/app/services/project_ai_config.py:45,348,441`：`ResolvedProjectModel`、`ResolvedSharedEmbedding` 选择、序列化、数据库重验；`backend/app/services/collaboration_ai_guard.py:313,502,522`：HTTP/任务发送 guard。
- `backend/app/services/task_service.py:246`、`task_engine/engine.py`、`chapter_claim.py`、`kb_index_service.py`：项目任务受理、执行 UUID、章节/文档 claim 及任务/资源受限清理；`task_engine/handlers/*.py` 是各 Job 实际桥接。
- `research/phase-e-rewrite-collaboration-review.md`、`phase-e-author-chapter-wiring.md`、`phase-e-analysis-execution.md`、`phase-e-engine-fencing.md`、`phase-e-ai-guard-review.md`、`phase-e-retrieval-callers.md`：具体切片的实现与历史验证；不能将早先描述当作当前全部代码状态。
- 外部参考：无。本次未运行模型、数据库、HTTP 测试或大型测试；下述测试通过数仅归属于引用的历史专项记录。

**结论**：通用 `task_service.create_task` 在 handler 校验后仍对有项目的 Task 调用 `get_owned_project(..., lock=True)`，并未普遍接收 editor（`task_service.py:246`）。章节 handler 的 `capture_*` / `bind_queued_claim` 明确拒绝 `_collaboration_grant`，`generation_service` 的完成收据仍拒绝协作 claim（`task_engine/handlers/chapter.py:64,96`、`generation_service.py:262`）。因此已有任务执行 UUID、claim、guard 或 `ai.use` 前置，不等于编辑者后台 AI 已开放。另一方面，同步章/场景及区块改写已有独立 editor 接入，不能把它误写成一律作者专属；其专项仍需按最新树完成真实 PostgreSQL 及综合验证。封面图片协作实现仍在改动中，本快照 `cover_service.py:27` 的 `_COLLABORATION_IMAGE_ENABLED = False`，不能依据不可达分支宣称已开放。

### 共通接口：受理、真实发送、帧和持久化是不同边界

1. HTTP 请求必须从 `DbDep` 的 `db.info["authenticated_actor"]` 保留原始 `AuthenticatedActor(id, token_version)`；`collaboration_ai_guard.for_http` 只接受原认证会话中的 actor（`:502`），不能在后续数据库读取时按当前 token 版本重建请求。后台 `for_task` 使用领取时的真实 task actor、`task_key`、`queued_at`、`execution_key` 和原 grant（`:522`），后台没有原 HTTP 会话，不可伪装持有用户 token。
2. `ResolvedProjectModel` 固定 actor、project/owner、`source=actor|owner`、配置归属/指纹、邀请 UUID/revision（`:45`）；显式 config ID 仅本人、无显式选择时本人默认/最新优先，只有本人没有同类型配置且作者授权才回退作者，失效后不切换模型。绑定仅由服务端产生并存于受信 Task；与独立 `_collaboration_grant` 比较，禁止相信客户端提交保留键，也不把作者配置 ID、指纹、授权对象、私有端点或错误外发。
3. `for_task(..., chapter=original_claim)` 在受理、领取后从本执行体固定的 Task/章 claim 构造；`_matches_task_binding` 对 `_project_model` / `_shared_embedding` 和 grant 解码并比对（`collaboration_ai_guard.py:223`），不是从当前数据库读取一条新 Task 重新给旧执行体续签。构造成功也不能代替真正执行前、每一跳发送时及写入事务中的再次校验。
4. `SendGuard.prepare_provider` 使用绑定固定配置，在短事务重验后从可信 factory 构建，真实 SDK 每一跳经 User → 可见性门禁 → Project → Task/Chapter → 模型锁的物理 lease，响应头释放。HTTP 请求与后台任务分别用 `for_http` / `for_task`；`guard_provider` 仅能保护其调用方实际传入的回调，不会因为别处存在 `SendGuard` 自动完成 grant/模型授权。
5. 一次性确认 `after_verify` 只能由 `SendGuard.verify` 在真实发送租约内消费；缓存命中通过 `verify_without_one_shot` 和 `ai_dispatch.require_preflight_lease` 验证，既不消费也不证明未来发送（`collaboration_ai_guard.py:361,405`、`retrieval_service.py:356`）。区块 `request_id` 的数据库 receipt、实际发送只允许一次、307/SDK 重试和预检次数都要各测；不能用一次预检视作一次发送。
6. 发送锁不覆盖响应头后的整个流。输出帧、工具返回、保存/提案/结果、成功日志与衍生任务各自要在对应事务或帧交付边界重验原身份、授权及版本；不承诺撤回已送出的字节。失败只允许原 UUID/入队/执行/章或文档 claim 的受限 CAS 清理，未知提交先查原事务 receipt，不能清新执行体或自动重复发送。
7. `resolve_shared_embedding` 返回 `mode=keyword|vector` 的 `ResolvedSharedEmbedding`，共享向量只使用项目作者的模型，要求独立授权；无授权时保留关键词模式，禁止借编辑者个人向量覆盖项目空间（`project_ai_config.py:441`）。真实 HTTP/后台分别以相同 guard 接出站；来源快照、空间 key、缓存预检、每批 CAS 写入与返回前复验均是独立集成工作。

### 当前 HTTP → 工作 → 模型/出站 → 保存、重试和清理

以下均省略共同前缀 `/api/v1`；行内“待接”是缺口，不是已实现承诺。具体角色身份还要覆盖纯管理员及管理员兼 viewer：管理人工能力不能推出 AI 能力。

| 入口及当前状态 | 实际链路与尚缺环节 |
| --- | --- |
| `POST /chapters/{chapter_id}/generate`、`/polish`、`/revise`；`POST /projects/{project_id}/chapters/{chapter_id}/scenes/{scene_id}/generate\|polish\|revise` | `chapters.py:117` / `analysis.py:84` / `scene_ai.py:44` → `task_service.create_task` → `ChapterHandler._claim` → `generation_service.validate_and_claim`；章 ID 先关联作品，现为 `get_owned_chapter/get_owned_project`（`generation_service.py:300,322`）。引擎 queued→running 冻结 execution 与 `ChapterClaim`，`build_chapter_job` 在运行时重新选择作者配置；`_prepare_scene_step` 先准备检索、再生成；`run_chapter_job` → `_save_chapter_result` 原子写章、版本、本人 log 和 claim receipt，失败只释放原 claim。**作者内部加固，不支持 editor**；需把锁后 `ai.use`、服务端冻结模型/原 grant、编辑者 claim，文本和向量的 `for_task` 以及真实 actor 输出/日志/重试接通，保留作者旧任务兼容且不能用现存 guard 装作已连通。 |
| `POST /chapters/{chapter_id}/rewrite`；`.../scenes/{scene_id}/rewrite` 及对应 `/rewrite/apply` | `chapter_versions.py:83` / `scene_ai.py:65` → `rewrite_service.prepare_rewrite_job` → `for_http` → `run_rewrite_job`；非作者经 `_ai_project_chapter` 定位父书并验证 `ai.use`，解析本人模型或获授权作者回退，`_output_lease` 护送单帧，成功日志带可信 model binding，`_apply` 在锁内验证本人成功 log、完整上下文和原 grant，章响应走 actor 安全投影（`rewrite_service.py:114,170,203,385,487`）。**已有 editor 的独立实现**；不使用共享向量。专项记录 SQLite/本机 TCP SDK 定向通过，5 个 PostgreSQL 场景在记录时仅收集。仍需以最终代码运行真实 PG、撤权/重邀、模型失效、跨书和兼容旧作者分支，不能外推后台生成。 |
| `POST .../scenes/{scene_id}/sections/{section_key}/rewrite` 及 `/rewrite/apply` | `rich_document_section_ai.prepare_section_rewrite` 在校验章/景/区块后创建一次性 receipt，编辑者 `for_http(..., after_verify=mark_sent)`；`run_rewrite_job` 发送、写本人成功 log；`apply_section_rewrite` 校验原 receipt、本人 log、原 grant 和正文/上下文版本（`rich_document_section_ai.py:58,213,236`）。**已有 editor 同步接入，非整项 E 完成**。必须在真实 PG + SDK 测预检不消费、实际首跳只一次、307 禁止重发、撤权与采纳两种锁序。 |
| `POST /projects/{project_id}/wizard/{kind}`（outline/characters/world/careers） | `wizard.py:14` → `WizardHandler.validate_and_claim/build_job` → `wizard_service.validate_wizard/build_wizard_job`；当前严格作者，Job 从原请求重新选择配置而非消费受理绑定，旧 `guard_provider` 不等于 editor 的 grant guard。`run_wizard_job → save_wizard_result` **直接落库**：覆盖/追加大纲，或替换角色、世界、职业及关系/挂接（`wizard_service.py:806,874`）；非续写保存路径不能仅依赖项目 active lock。`continue_from_task_id` 还要绑定 `continue_from_task_key` 和来源指纹（`research/phase-e-wizard-resume-identity.md`）；abort 无内容回退。需在发送、每帧及多实体保存事务绑定原任务 execution、grant、model 和可见性；替换前校验依赖/快照、父行和级联原子性。该服务未见直接 `generation_log_service` 写入，现有 task 输出/结果需按本人隔离，是否新增日志依产品合同确认。 |
| `POST /projects/{project_id}/characters/generate`、`/characters/{character_id}/parse-relations`、`/organizations/generate`、`/careers/generate`、`/relationships/generate` | `task_service.create_task` → `EntityHandler`（`task_engine/handlers/entities.py`）→ `entity_ai_service.validate_entity_task/build_entity_job/run_entity_job`（`:498,657,895`）。当前按严格作者检查，受理存 `ai_config_id` 但运行按 `request.ai_config_id` 重新解析，使用通用 visibility guard；返回人物、组织、职业或关系**草稿**和建议，当前不直接写生成记录或内容；`on_abort` 无需业务回退。用户后续人工确认属于独立 W；需给原 task 保存受信 model/grant、每次发送及每帧/done 投影重验，解析关系确保原 character 与全部关联同项目。 |
| `POST /projects/{project_id}/outlines/continue`、`/outlines/{outline_id}/expand` | `outline_pro.py:74,93` → `OutlineHandler` → `outline_ai_service.validate_outline_task/build_outline_job/run_outline_job`（`:220,307,545`），现为作者检查、Job 时按原请求重选配置；expand 可分批多次模型发送，可能先进行知识/记忆向量检索，`continue_result` / `expand_result` 为草稿，不直接写作品，用户后续同步细纲属人工 W；任务输出属本人。需给每个文本/向量批次、最终草稿投影和 retry 绑定同一原 task/grant/model；验证 outline 与 project 同属、模型/权限变更不得接续旧批次。 |
| `POST /chapters/{chapter_id}/analyze`、`POST /projects/{project_id}/analysis/analyze-pending` | `analysis.py:23,39` → `analysis_service.request_analyze/analyze_pending` → `AnalysisHandler` → `validate_analyze/build_analysis_job/run_analysis_job`（`analysis_service.py:701,743,1196,1246,1435`）。现在严格作者，`AnalysisExecution` 保存 actor、task UUID、queued/execution、章和 grant，但 `_analysis_initial_check` / `_lock_analysis_execution` 显式拒绝协作 grant 与保留模型绑定；作者 build 时冻结模型，发送和最终分析/回写/自动记忆写事务检查执行体。成功后的 `enqueue_memory_for_execution` 再核原任务。**当前是作者执行防护**；要支持 editor 必须在子章定位、分析模型绑定、发送、结果和派生向量全过程保持原 grant，不能直接删除拒绝条件。分析任务不写 `GenerationLog`，个人任务 ID/状态要过滤；已有分析正文和人工回写读写属于 D 的独立能力。 |
| `POST /projects/{project_id}/batch-generate/preview`、`POST /projects/{project_id}/batch-generate` | `batch.py` → `batch_service.preview` / `task_service.create_task → BatchHandler → validate_start/build_batch_job/run_batch_job`（`batch_service.py:256,380,1230`），当前严格作者。逐章 `_generate_chapter` 在原 batch execution 中创建章 claim，复用 generation 服务/日志与完成 receipt；待审核可能暂停，`/tasks/{id}/resume\|retry` 校验断点；`_analyze_chapter` 在原 batch 内做分析并排记忆索引，非简单另起子任务。需为每个章节 attempt、分析阶段、提案接受、checkpoint 和失败清理贯穿 editor 的原 grant/model/shared embedding，不能因 batch 容器有 grant 字段就认为下游已接通。 |
| `POST /projects/{project_id}/kb/documents`、`.../{document_id}/reindex`、`/kb/rebuild` | `kb.py:44,75,108` 先做人工/`ai.use` 前置，`kb_service.upload_document/reindex_document/start_rebuild` 后仍严格作者（`kb_service.py:113,216,406`）→ `kb.index` → `kb_index_service.validate_index_task/build_index_job/run_index_job`。文档排队/运行 claim 已绑定原 task 与 execution，分块、向量写入与 claim 有作者内部防护，但 `build_index_job` 仍要求 `project.user_id == task.user_id`；`_embed_stale` 仍调用旧 `store_embeddings`。需服务端固定 actor/grant/共享向量模式、数据来源/space 快照和每批 CAS，禁用作者向量授权时按设计允许上传并使用关键词、不伪造作者任务；重建需显式错误。 |
| `POST /projects/{project_id}/kb/search`、`/memories/search`；`GET /chapters/{chapter_id}/context-preview` | 路由有 `ai.use` 前置，但 `kb_service.search_debug`、`memory_service.search_debug` 及 `context_builder.retrieval_preview` 后续仍严格作者；`retrieval_service.get_embedder(user_id)` 为旧个人默认模型路径（`kb_service.py:283`、`memory_service.py:607`、`retrieval_service.py:550`）。不能因为检索原语或 guard 已存在就开放共享向量。接入需显式判断关键词与共享作者向量、受控缓存 preflight、源正文/检索行一致性、真实发送及结果投影时原授权；`GET .../planning/units/context-preview` 是已单独实现的纯本地 R，并非该向量预览。 |
| `POST/PATCH /projects/{project_id}/memories...` 的自动向量派生 | `memories.py:_enqueue_vectors` 在人工写已提交后取原 `AuthenticatedActor`；`memory_service.may_enqueue_vectors` 当前只允许作品作者，editor 的本地人工记忆可写但不代作者排队。`kb_index_service.enqueue_memory` 再验证作者；分析/Agent 派生目前分别走 `enqueue_memory_for_analysis` / `enqueue_memory`。要在共享向量授权开启时让任务归真实 editor，并在禁用时只维护关键词和清除受影响旧向量，不能用 `project.user_id` 替 actor。 |
| `GET /projects/{project_id}/cover/prompt`、`POST .../cover/generate` | 当前 `default_prompt → require_project(ai.use)` + `visible_default_style(actor_user_id)`，可按角色提供本地提示词并不泄露作者私有风格。`generate_cover` 的作者分支 `resolve_config → build_provider → generate_image`；正在开发的 `_generate_collaborator_cover` 已出现 `resolve_project_model` / `for_http` 及最终 model/grant 复验，但 `_COLLABORATION_IMAGE_ENABLED=False` 时 editor 返回 404（`cover_service.py:27,87,117`）。这是进行中的 gated 分支；需核对 provider 真实图片请求、OpenAI 图片 URL 的独立下载、固定异常脱敏、结果与实际 ASGI 返回边界，在 PG 发送与撤权验证后才能决定开关。保存封面是独立人工 `PUT .../cover`。 |
| `POST /projects/{project_id}/agent/conversations/{conversation_id}/messages` 及 `/changes/{change_id}/confirm\|reject\|undo`、`/turns/{task_id}/...` | `agent.py:mutation_db` 复制原 token 到单独事务；`agent.service.owned_conversation/send_message` 当前严格作品作者且会话本人 → `agent.turn` → AgentHandler → `agent.runner._turn/_process_calls/_external_call/_save_log`。已有原 task/execution 和外部调用 claim、SAVEPOINT/审计以及部分输出隐私原语；`agent.context.guard_agent_provider` 使用作者型执行检查，明确拒绝协作 grant（`agent/context.py:134,155,227`）。本人历史生成日志只读投影不意味着会话、消息或工具已开放。需将模型来源、原 grant、本人会话、子任务/外部工具、提案/撤销、摘要与每帧接入，事务内成功审计和回滚后失败审计正确分离；不能把所有工具调用假设为可撤回。 |
| 后台自动派生及旧任务恢复 | `ChapterHandler.on_success → analysis_service.enqueue_auto → enqueue_auto_in_transaction` 使用 `Task.user_id=project.user_id`（`analysis_service.py:795,819`）；`rich_document_review._accept_review` 成功也可派生自动分析；批量分析在原 BatchJob 内执行；分析成功再排记忆索引。当前这些只可用于严格作者链。editor 开放前需让派生操作继承原真实 actor、grant、原配置选择和任务/费用归属，在最终保存事务与原任务身份下创建或安全失败；旧任务无可靠 UUID/执行/授权不可由现在数据库值续签。 |

### 必须串行协调的接口与可独立实施的顺序

此处建议逻辑顺序，不表示当前代理已释放文件；主会话需先确认各文件的实际所有权。所有可能开放 editor 的切片在共享基础协议尚未闭环时应维持服务端关闭或失败关闭，不能仅靠前端隐藏。

1. **协议冻结、统一受理与引擎**：一个负责人独占 `task_service.py`、`task_engine/engine.py`、`hub.py`、`cancellation.py`、必要 registry/context 接口，以及对应测试；与 `project_ai_config.py` / `collaboration_ai_guard.py` 所有者先确定输入 schema 和内部公开接口。要求受理时由服务端覆盖 `_collaboration_grant`、`_project_model`、`_shared_embedding`，锁后持久化，领取后冻结 UUID/queued/execution，实际原 claim 必须随 Job 传递；retry/resume 禁止旧授权重邀续签。避免让多个功能组同时修改统一 task/engine 文件。兼顾当前所有者作者 claim、提案和任务兼容，单进程恢复前排空旧执行器。
2. **共享向量与检索（独立模块）**：一个负责人独占 `kb_service.py`、`kb_index_service.py`、`memory_service.py`、`retrieval_service.py` 及 `api/v1/kb.py`、`memories.py`；使用第 1 步固定的接口而不并行改引擎/章节。首先连接 `ResolvedSharedEmbedding`、缓存预检和来源快照；再接文档上传/重建、每批发送/写 CAS 与真实 actor 的记忆/分析派生；最后接搜索与章节预览。向导/章/Agent 等调用方只在接口稳定后逐一迁移。索引旧作者行为与异步 cleanup 应另测。
3. **章节 → 提案 → 批量（共享依赖需分序）**：章节组独占 `generation_service.py`、`task_engine/handlers/chapter.py`，使用已稳定的 guard/claim/检索协议；保留同步改写专项自身代码及 `rich_document_section_ai.py` 已实现的一次性协议。确认单章模型与章 claim 后，提案组修改 `rich_document_review.py`；批量组随后修改 `batch_service.py` / handlers/batch.py，复用前述接口，不同时改章节共享文件。将直接/批量分析与完成 receipt 及未知提交一并验收。
4. **分析与自动派生**：分析组独占 `analysis_service.py` / `handlers/analysis.py`，与索引组先约定 `enqueue_memory_for_analysis` 事务接口；不能和章节/批量组并行修改对方文件。使 `AnalysisExecution` 可用可信原 grant/绑定，替代作者专用拒绝时先覆盖作者、批量和未知提交；由中央接入方在章节 on_success 与提案处传原执行身份。分析生成记忆与后续向量不可用假装同一提交。
5. **向导、实体、大纲三个独立 Job**：协议稳定后分别独占 `wizard_service.py` + handlers/wizard.py、`entity_ai_service.py` + handlers/entities.py、`outline_ai_service.py` + handlers/outline.py，互不修改中央任务及共享日志/检索文件。向导直接删除/替换多个子资源，必须单独加原执行体写屏障；实体和大纲只返回草稿，但 SSE/task output 仍需本人和当前授权终检。保留向导续写 source UUID/指纹；各组移交最小适配补丁，不自行改变统一协议。
6. **Agent 与封面独立推进但分别验收**：Agent 负责人独占 `agent/`、handlers/agent.py 和 Agent 路由；先等待正在进行的事务/私有数据/提案身份修改合入，使用中央 task、guard 和共享检索接口。封面负责人独占 `cover_service.py`、封面路由/响应 schema 和本专项测试；等待正在修改的代码释放后审查模型网络和结果边界，开关以最终专项真实验证为条件。两组不抢共享 guard/project_ai_config 文件。
7. **最终端到端 E7**：以真实服务 API、实际传输和隔离 PG 验角色矩阵、个人模型优先/显式伪造/缺失与作者回退、费用来源、邀请降级/撤销/重邀、任务/日志/输出隐私、外部调用计数、未授权无实际发送、授权失效不换模型、每项清理及历史兼容；再与全后端/前端和迁移、旧进程排空一起交付。不要因为一个 Job 的安全测试通过而开放其他 Job。

### 必须使用真实 PostgreSQL 的验证清单

- **锁顺序与物理发送**：两个应用连接加本机真实 SDK/HTTP 服务器，在响应头前分别使撤权、降级、作者模型授权关闭、账号禁用、配置删除/变更先提交和发送先获得 lease；验证零发送或撤权等待至响应头，后续每跳/流帧/保存失败关闭。观察真实 PG blocking PID，避免测试自行制造锁等待环；不以 Fake provider、SQLite 或调用守卫次数代替。
- **Task/claim 代次**：queued→running 同事务 UUID 与章/文档 claim；同 task 整数 ID 重用、同 UUID 重排、execution/claim 更换、取消/重试/人工复位/管理员 drain、受限 abort 和新工作先受理，旧执行体不能发、写或清新占用。模拟提交确认丢失并检查持久 receipt；无事实保持安全屏障，不自动补发。
- **同步改写/区块**：`tests/test_collaboration_rewrite_identity_postgresql.py` 涵盖的真实发送 lease、撤权/重邀旧采纳、实际 ASGI 单帧交付与慢客户端释放；`tests/test_collaboration_ai_guard_postgresql.py` 对真实 lease 与 preflight/one-shot 分开验证。上述文件在对应历史记录时是已收集，须按最终代码核实实际通过，而不是将 collected 当 passed。
- **索引及缓存**：文档上传/取消/重建与新 claim 两种顺序；向量 provider 多批发送和每批写、缓存命中与清理/索引删除、作者模型 A→B→A、授权关/开及关键词模式冻结、来源正文篡改/跨书关联；分别测试 `test_collaboration_retrieval_postgresql.py`、`test_collaboration_retrieval_cache_preflight_postgresql.py` 和真实 editor 路由端到端。现有纯原语测试不能代替完整 E6。
- **结果与派生**：向导替换、章节保存与提案采纳、批量断点、分析结果与自动记忆、自动索引派生，分别在锁前/锁后撤权、任务轮次变化、章/场景/可见性版本改变时验证事务原子；task/log/Agent 输出仅本人及当前授权，管理员兼协作者不能读取别人私人内容。同步封面还要覆盖模型响应、独立图片下载和返回前撤权。
- **Agent 外部行为与审计**：本人原任务/会话、提案 key/view 版本、外部 MCP 结果或工具提交与撤权、异常取消、SAVEPOINT 回滚、审计提交失败；对已发生外部副作用按事实记录且不重复。历史 `test_agent_context_execution_identity_postgresql.py`、`test_collaboration_analysis_execution_postgresql.py` 等作者专项已有阶段性 PG 结果，但需最新代码与 editor 链各自重测。

## Caveats / Not Found

- 本次是静态调用链调查；代码在调查期间继续变化。尤其封面协作分支及分析 pending/receipt 正在由其他代理修改，文中“当前”仅限读取时，最终行为、测试数和行号应以主会话实际结果为准。
- 没有执行真实模型、PostgreSQL 或其他测试；文中指出的历史通过/仅收集状态只引用对应研究文档，不能证明最终代码通过。未发现 `for_task(...)` 被生产任务实际调用或项目调用方实际调用 `resolve_shared_embedding(...)`；这不排除其他分支正在接线，仍须最终树复核。
- 无法仅由静态调用链证明所有 provider 的网络重定向、图片 URL 下载、流与任务取消时序；未运行测试的风险是待验证边界，不直接宣称已发生安全事故。
