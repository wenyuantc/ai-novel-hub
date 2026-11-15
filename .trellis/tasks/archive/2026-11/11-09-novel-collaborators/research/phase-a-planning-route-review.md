# A1 复核：规划、资料、私人数据与任务入口

这是对当前代码的静态调用链核对，不把清单中的“候选”当作完成声明；其他代理仍在并行修改，行号是本次检查时的定位。HTTP 路径均省略共同前缀 `/api/v1`。章节普通 CRUD、场景、版本恢复和作品/系列管理由对应专项复核；本文件覆盖下列规划、资料、分析、私人数据与后台任务入口及相关边界。

## 记号和共用边界

- **R**：明确 `content.read` 人工能力，作者、接受的查看者/编辑者和原有 `projects.read` 管理员可读；**W**：明确 `content.edit` 人工能力，作者、接受的编辑者或同时有 `projects.read` 与 `projects.edit` 的管理员可写；管理员兼协作者按两种来源并集。未接受/撤权且无管理员权限者不可见，查看者写入被拒。**X**：`export`，作者与接受的协作者，不自动包含纯管理员。参见 `backend/app/services/project_access.py:43,77,127`；写入 `for_update=True` 经过认证屏障、父作品行锁和锁后成员重读。
- **O**：严格作者 `get_owned_project` / `get_owned_chapter`，非作者的纯管理员和协作者均不能借此入口访问；**M**：作品作者或接受的作品协作者的本人私人数据，不以管理员人工能力旁路；**T**：本人任务且作品处于活动状态，接受的协作者对活动任务还需当前 editor 与原邀请轮次一致；已结束任务允许当前接受成员查看，具体谓词在 `backend/app/services/collaboration_task_access.py:72`。项目以外的个人任务保留其原有本人语义。
- 父约束列举实际过滤/查询点；它描述该入口的已有执行路径，不表示所有损坏数据、并发或 E 阶段行为已获得动态验证。父锁、权限与角色以 `project_access` 实现为准，AI 路径不能因人工能力扩大。

## 规划

以下路径前缀为 `/projects/{project_id}/planning`。读服务经 `_snapshot` 检查 R 及前后规划 revision（`backend/app/services/planning_service.py:65`）；写服务经 `_editing` 检查 W、锁父行及 revision CAS（同文件 `:44`）。`_unit`、`resolve_source`、`_tag`、`_subplot`、`_crossing` 校验项目归属（`:77,97,397,470,596`），跨章节/细纲来源要校验章节、细纲、大纲共同归属。每行位置依次为 API 与 service。

| 方法和路径 | 调用链；能力 | 父作品及特别限制 |
| --- | --- | --- |
| GET `/matrix` | `planning.py:38` → `planning_service.matrix:861`；R | `query.rows/entries` 与候选限定作品；一致性快照。 |
| GET `/entries` | `planning.py:77` → `planning_service.list_entries:347`；R | 作品条目；单书协作者无系列读取权时不显示 `series_shared`，只保留匿名标志，参见 `:366`。 |
| PATCH `/units/batch` | `planning.py:93` → `planning_service.batch_update:248`；W | 来源与引用先在父作品中解析和验证。 |
| POST `/units/merge` | `planning.py:98` → `planning_service.merge_units:772`；W | 两个单元及其来源同作品。 |
| POST `/units/{unit_id}/binding` | `planning.py:103` → `planning_service.bind_unit:715`；W | 单元和目标来源同作品。 |
| GET `/units/context-preview` | `planning.py:108` → `get_manageable_project` → `planning_context_service.context_preview`；旧作者/有原管理员权限，非 R | 本地规划文本拼装；普通单书协作者尚未迁移，不能称为协作预览完成。 |
| GET `/detached` | `planning.py:124` → `planning_service.list_detached:384`；R | 仅项目下游离规划。 |
| GET `/tags` | `planning.py:136` → `planning_service.list_tags:404`；R | 项目标签。 |
| POST `/tags` | `planning.py:148` → `planning_service.save_tag:438`；W | 新标签绑定项目。 |
| PATCH `/tags/{tag_id}` | `planning.py:153` → `planning_service.save_tag:438`；W | `_tag` 限定项目。 |
| DELETE `/tags/{tag_id}` | `planning.py:158` → `planning_service.delete_tag:460`；W | `_tag` 限定项目；revision。 |
| GET `/subplots` | `planning.py:165` → `planning_service.list_subplots:517`；R | 项目支线。 |
| POST `/subplots` | `planning.py:177` → `planning_service.save_subplot:530`；W | 新支线绑定项目。 |
| PATCH `/subplots/{subplot_id}` | `planning.py:182` → `planning_service.save_subplot:530`；W | `_subplot` 限定项目。 |
| DELETE `/subplots/{subplot_id}` | `planning.py:189` → `planning_service.delete_subplot:541`；W | `_subplot` 限定项目。 |
| PUT `/subplots/{subplot_id}/nodes/{source_kind}/{source_id}` | `planning.py:196` → `planning_service.put_node:563`；W | 支线和来源均校验项目。 |
| DELETE `/subplots/{subplot_id}/nodes/{source_kind}/{source_id}` | `planning.py:211` → `planning_service.delete_node:583`；W | 支线和来源均校验项目。 |
| GET `/crossings` | `planning.py:226` → `planning_service.list_crossings:605`；R | 可选支线及来源校验项目。 |
| POST `/crossings` | `planning.py:245` → `planning_service.save_crossing:630`；W | 来源和参与支线校验项目。 |
| PATCH `/crossings/{crossing_id}` | `planning.py:250` → `planning_service.save_crossing:630`；W | 交汇与来源归项目。 |
| DELETE `/crossings/{crossing_id}` | `planning.py:257` → `planning_service.delete_crossing:665`；W | `_crossing` 限定项目。 |
| GET `/weave` | `planning.py:264` → `planning_service.weave:1065`；R | 项目行、支线与关联位置。 |

## 大纲、细纲和快照

以下路径前缀为 `/projects/{project_id}`。`story` 工厂以 `manage=True` 显式走人工能力；其默认 `manage=False` 与 Agent 调用仍保留旧严格路径。`plan_service.get_manual_project_outline` 限定大纲项目（`backend/app/services/plan_service.py:57`），计划读取及输出也检查计划与章节归属。

| 方法和路径 | 调用链；能力 | 父作品及特别限制 |
| --- | --- | --- |
| GET `/outlines` | `story.py:47` → `story_service.list_owned:56(manage=True)`；R | `list_items` 限定项目。 |
| POST `/outlines` | `story.py:54` → `story_service.create_owned:131(manage=True)`；W | 新大纲属于项目并更新规划 revision。 |
| GET `/outlines/{item_id}` | `story.py:58` → `story_service.get_collaborative:118`；R | `_get_item:125` 同时匹配 ID 与项目。 |
| PATCH `/outlines/{item_id}` | `story.py:64` → `story_service.update_owned:158(manage=True)`；W | 父锁后 `_get_item`。 |
| DELETE `/outlines/{item_id}` | `story.py:77` → `story_service.delete_owned:244(manage=True)`；W | 父锁后校验子资源，`planning_lifecycle.detach_outlines:169` 异作品反向引用匿名 409。 |
| GET `/outlines/tree` | `outline_pro.py:44` → `outline_service.outline_tree:42(manual=True)`；R | 输出当前项目大纲与细纲；默认 `manual=False` 不扩权。 |
| GET `/outlines/export` | `outline_pro.py:49` → `outline_transfer_service.export_outline:48`；X | 作品数据；纯管理员非协作者不获得导出。 |
| POST `/outlines/import/preview` | `outline_pro.py:54` → `outline_transfer_service.preview_import:146`；R | 本地解析和项目角色匹配；不代表写权限。 |
| POST `/outlines/import` | `outline_pro.py:61` → `outline_transfer_service.import_outline:157`；W | 父锁及跨作品级联预检；拒绝时同事务回滚。 |
| POST `/outlines/append` | `outline_pro.py:68` → `outline_service.append_outlines:76`；W | 新大纲属于项目。 |
| POST `/outlines/continue` | `outline_pro.py:73` → `task_service.create_task:210`；O，AI 任务 | 尚未接入协作者真实 actor；非人工 W。 |
| POST `/outlines/{outline_id}/move` | `outline_pro.py:85` → `outline_service.move_outline:105`；W | 父锁后人工大纲校验。 |
| POST `/outlines/{outline_id}/expand` | `outline_pro.py:92` → `task_service.create_task:210`；O，AI 任务 | 不能凭 W 推断可代作者创建任务。 |
| GET `/outlines/{outline_id}/plans` | `outline_pro.py:109` → `plan_service.list_plans:148`；R | 大纲和计划同作品。 |
| PUT `/outlines/{outline_id}/plans` | `outline_pro.py:114` → `plan_service.sync_plans:202(manual=True)`；W | 锁后大纲、细纲、章节和反向引用预检；可能隐式删空章时纯协作者先取发送写门禁并检查他人任务。 |
| PATCH `/outlines/{outline_id}/plans/{plan_id}` | `outline_pro.py:119` → `plan_service.update_plan:153`；W | 大纲、计划和人物限定项目。 |
| POST `/outlines/{outline_id}/plans/build` | `outline_pro.py:126` → `plan_service.build_chapters:342`；W | 父锁、大纲与绑定目标校验。 |
| GET `/chapters/{chapter_id}/plan` | `outline_pro.py:131` → `plan_service.chapter_plan_out:194`；R | 章节、计划与大纲均限定项目。 |
| GET `/outline-snapshots` | `outline_pro.py:136` → `outline_snapshot_service.list_snapshots:130`；R | 按项目列快照。 |
| POST `/outline-snapshots` | `outline_pro.py:141` → `outline_snapshot_service.create_manual:138`；W | 父锁后保存项目快照。 |
| GET `/outline-snapshots/{snapshot_id}` | `outline_pro.py:148` → `outline_snapshot_service.get_detail:179` → `_get:120`；R | 快照 ID 与项目共同限定。 |
| POST `/outline-snapshots/{snapshot_id}/restore` | `outline_pro.py:153` → `outline_snapshot_service.restore_snapshot:323`；W | 父锁、快照归属和级联预检；冲突回滚。 |
| DELETE `/outline-snapshots/{snapshot_id}` | `outline_pro.py:158` → `outline_snapshot_service.delete_snapshot:146`；W | 父锁后按项目校验快照。 |

## 笔记和提及

| 方法和路径 | 调用链；能力 | 父作品及特别限制 |
| --- | --- | --- |
| GET `/projects/{project_id}/notes` | `notes.py:15` → `note_service.list_notes:20`；R | 查询项目笔记。 |
| POST `/projects/{project_id}/notes` | `notes.py:31` → `note_service.create_note:65`；W | 父锁后创建，不触发模型。 |
| GET `/projects/{project_id}/notes/{note_id}` | `notes.py:36` → `note_service.get_note:60`；R | `_note:13` 同时匹配作品和 ID。 |
| PATCH `/projects/{project_id}/notes/{note_id}` | `notes.py:41` → `note_service.update_note:78`；W | 父锁、项目限定、revision CAS。 |
| PUT `/projects/{project_id}/notes/{note_id}/flags` | `notes.py:46` → `note_service.set_note_flags:99`；W | 父锁、项目限定；标记无 revision CAS。 |
| DELETE `/projects/{project_id}/notes/{note_id}` | `notes.py:51` → `note_service.delete_note:117`；W | 父锁、项目限定、revision CAS。 |
| GET `/projects/{project_id}/mentions/catalog` | `mentions.py:24` → `mentions/query.catalog:22` → `context.query_context:83`；R | 作品内候选。 |
| POST `/projects/{project_id}/mentions/preview` | `mentions.py:29` → `mentions/query.preview:39` → `query_context`；R | 用户文本本地字面匹配，不排队或调用模型。 |
| GET `/projects/{project_id}/mentions` | `mentions.py:34` → `mentions/query.list_mentions:78` → `query_context`；R | 四来源按作品筛选；聊天额外按本人会话筛选。 |
| GET `/projects/{project_id}/mentions/summary` | `mentions.py:61` → `mentions/summary.summarize:38` → `query_context`；R | 项目范围及本人聊天限制同上。 |
| GET `/projects/{project_id}/mentions/sources/{source_type}/{source_id}` | `mentions.py:90` → `mentions/window.source_window:61` → `query_context`；R | 来源窗口经 `mentions/sources._statement:19` 限定项目和 source ID；聊天还要求 `AgentConversation.user_id == actor`，纯管理员不提供 chat 来源（`context.py:83`）。 |

## 记忆和知识库

记忆人工修改同步本地检索单元；只有作品作者可由 API 的 `_enqueue_vectors` 派生向量任务（`backend/app/api/v1/memories.py:58`、`backend/app/services/memory_service.py:239`）。知识库 `KbSettingsOut` 只包含检索参数（`backend/app/schemas/kb.py:12`），索引状态另走 O，不能据此推断协作者已接通模型配置。

| 方法和路径 | 调用链；能力 | 父作品及特别限制 |
| --- | --- | --- |
| GET `/projects/{project_id}/memories` | `memories.py:27` → `memory_service.list_memories:280`；R | 记忆按项目筛选，自动来源的一致性检查不跨书计算。 |
| POST `/projects/{project_id}/memories` | `memories.py:66` → `memory_service.create_memory:331`；W | 父锁、本地索引；仅作者可排向量任务。 |
| PATCH `/projects/{project_id}/memories/{memory_id}` | `memories.py:84` → `memory_service.update_memory:354`；W | 父锁及 `get_project_memory:245` 项目限定；自动记忆仅可标记。 |
| DELETE `/projects/{project_id}/memories/{memory_id}` | `memories.py:93` → `memory_service.delete_memory:383`；W | 父锁、项目和人工记忆限定，不排新任务。 |
| POST `/projects/{project_id}/memories/search` | `memories.py:75` → `ai.use` 前置 → `memory_service.search_debug:531`；O | 会准备 embedding；编辑者仍被后续严格作者检查拒绝，E 未迁移。 |
| GET `/chapters/{chapter_id}/context-preview` | `memories.py:99` → `context_builder.retrieval_preview:1026`；O | 先严格查本人章节和项目，再预览知识与记忆检索；非上表纯本地规划预览，E 未迁移。 |
| GET `/projects/{project_id}/kb/settings` | `kb.py:28` → `kb_service.get_settings:70`；R | 项目设置输出不含 embedding 配置 ID。 |
| PATCH `/projects/{project_id}/kb/settings` | `kb.py:33` → `kb_service.update_settings:75`；W | 父锁后修改项目设置。 |
| GET `/projects/{project_id}/kb/documents` | `kb.py:38` → `kb_service.list_documents:100`；R | 按作品过滤文档。 |
| POST `/projects/{project_id}/kb/documents` | `kb.py:43` → `content.edit` + `ai.use` 前置 → `kb_service.upload_document:113`；O | 后续严格作者并创建索引任务；编辑者未接通上传。 |
| PATCH `/projects/{project_id}/kb/documents/{document_id}` | `kb.py:61` → `kb_service.set_inject:160`；W | 父锁，`get_project_document:92` 同时限定作品和文档；同步本地检索开关。 |
| DELETE `/projects/{project_id}/kb/documents/{document_id}` | `kb.py:68` → `kb_service.delete_document:191`；W | 父锁、同作品文档；索引活动中冲突。 |
| GET `/projects/{project_id}/kb/documents/{document_id}/chunks` | `kb.py:82` → `kb_service.list_chunks:216`；R | 先限定同作品文档，再按该文档列分块。 |
| POST `/projects/{project_id}/kb/documents/{document_id}/reindex` | `kb.py:74` → `ai.use` 前置 → `kb_service.reindex_document:201`；O | 作者锁及同作品文档；E 未迁移。 |
| POST `/projects/{project_id}/kb/search` | `kb.py:94` → `ai.use` 前置 → `kb_service.search_debug:266`；O | 模型/向量检索尚未为协作者接通。 |
| GET `/projects/{project_id}/kb/index-status` | `kb.py:102` → `kb_service.index_status:369`；O | 旧作者索引状态，非一般 R。 |
| POST `/projects/{project_id}/kb/rebuild` | `kb.py:107` → `ai.use` 前置 → `kb_service.start_rebuild:389`；O | 创建作者索引任务；E 未迁移。 |

## 分析与人工回写

`analysis_service._task_actor` 要求作者或接受的协作者，纯管理员保持 404；`_active_tasks` 与 `_visible_task_ids` 使用 `task_visible_to_actor` 同时过滤真实 actor、作品和活动授权轮次，故不可见的活动任务不投影 ID 或进行中状态。`_recheck_task_references` 在后续 await 结束后根据原 actor、任务 UUID、作品和任务当初的活动身份再次查询（`analysis_service.py:399` 附近）；状态查询还要求任务仍在活动状态。历史和详情额外限定分析、章节、项目一致。`writeback_service.get_manual_change:601` 在父锁后校验分析、章节及回写同属作品，确认/撤销持可见性写门禁，拒绝仅持父锁；人工回写本身不派生分析或向量任务。

| 方法和路径 | 调用链；能力 | 父作品及特别限制 |
| --- | --- | --- |
| GET `/projects/{project_id}/analysis/statuses` | `analysis.py:45` → `analysis_service.project_states:525(manual=True)`；R + M | 查询本作品章节；纯管理员无分析可见性，任务 ID 仅本人。 |
| GET `/chapters/{chapter_id}/analysis-status` | `analysis.py:50` → `analysis_service.chapter_state:542`；R + M | 先按章节定位并授权父作品。 |
| GET `/chapters/{chapter_id}/analyses` | `analysis.py:55` → `analysis_service.list_analyses:554`；R + M | 章节及分析项目一致。 |
| GET `/analyses/{analysis_id}` | `analysis.py:60` → `analysis_service.get_analysis:582(manual=True)`；R + M | 分析、章节和项目一致；Agent 默认非 manual 严格作者。 |
| POST `/analysis-changes/{change_id}/confirm` | `analysis.py:70` → `writeback_service.confirm_change:829` → `get_manual_change:601`；W + M | 父锁和可见性写门禁；纯管理员无协作者身份则 404。 |
| POST `/analysis-changes/{change_id}/reject` | `analysis.py:78` → `writeback_service.reject_change:957` → `get_manual_change`；W + M | 父锁，同作品关联。 |
| POST `/analysis-changes/{change_id}/revert` | `analysis.py:65` → `writeback_service.revert_change:716` → `get_manual_change`；W + M | 父锁和可见性写门禁。 |
| POST `/chapters/{chapter_id}/analyze` | `analysis.py:22` → `analysis_service.request_analyze:646`；O | 严格作者章节后排任务；不因 R/W 变成协作 AI。 |
| POST `/projects/{project_id}/analysis/analyze-pending` | `analysis.py:38` → `analysis_service.analyze_pending:688`；O | 严格作者项目后排队。 |
| POST `/chapters/{chapter_id}/revise` | `analysis.py:83` → `task_service.create_task:210`；O，AI | 任务 handler 还校验章节；不是人工回写。 |

兼容边界：旧任务 `statistics_key` 允许为空，无法证明整数 ID 没有复用时响应隐藏其任务引用和活动状态，不更改任务记录或分析正文；正常 UUID 的作者任务保持可见。项目状态、章节状态、历史和详情在请求中途授权变化或任务身份变化的响应前复查已用 HTTP 屏障验证；本专项尚未进行 PostgreSQL 并发验证。

## 私人生成记录、Agent 与任务

生成记录经 `personal_projection.require_personal_project:174` 要求 M，并由 `generation_log_service.public_logs:118` 校验 actor 与项目、投影嵌套私人引用；章节记录还校验章节项目（`generation_log_service.py:129`）。Agent 会话、工具和任务生成的开放程度要按各入口分别判断，不能因 M 投影可用就宣称会话发送已开放。

| 方法和路径 | 调用链；能力 | 父作品及特别限制 |
| --- | --- | --- |
| GET `/projects/{project_id}/chapters/{chapter_id}/generation-logs` | `chapter_versions.py:64` → `generation_log_service.list_logs:137`；M | 只返回当前 actor、当前项目、当前章节的记录。 |
| GET `/projects/{project_id}/chapters/{chapter_id}/generation-logs/{log_id}` | `chapter_versions.py:69` → `generation_log_service.get_log:149`；M | 额外匹配 log ID，嵌套私人引用投影。 |
| GET `/projects/{project_id}/agent/conversations` | `agent.py:46` → `agent/service.list_conversations:150`；O | 项目及 `conversation.user_id` 均匹配本人。 |
| POST `/projects/{project_id}/agent/conversations` | `agent.py:51` → `agent/service.create_conversation:169`；O | 锁作者项目，建立本人会话。 |
| GET `/projects/{project_id}/agent/conversations/{conversation_id}` | `agent.py:58` → `agent/service.get_conversation:196` → `owned_conversation:100`；O | 同作品且本人会话，消息/提案再做私人引用投影。 |
| PATCH `/projects/{project_id}/agent/conversations/{conversation_id}` | `agent.py:65` → `agent/service.update_conversation:220` → `owned_conversation`；O | 锁作者项目和本人会话，插件仅本人。 |
| DELETE `/projects/{project_id}/agent/conversations/{conversation_id}` | `agent.py:77` → `agent/service.delete_conversation:286` → `owned_conversation`；O | 活动轮次冲突。 |
| GET `/projects/{project_id}/agent/conversations/{conversation_id}/generation-logs` | `agent.py:72` → `agent/service.list_generation_logs:246`；M | 例外：只读本人会话的本人工程记录；不连带开放会话读写或发送。 |
| POST `/projects/{project_id}/agent/conversations/{conversation_id}/messages` | `agent.py:83` → `agent/service.send_message:295` → `owned_conversation` / `task_service.create_task`；O，AI | 真实本人会话、父作品，历史 epoch 复验。 |
| POST `/projects/{project_id}/agent/changes/{change_id}/confirm` | `agent.py:92` → `agent/service.decide_change:585` → `owned_change:326`；O | 按本人会话和项目校验，可能执行提案。 |
| POST `/projects/{project_id}/agent/changes/{change_id}/reject` | `agent.py:97` → `agent/service.decide_change:585` → `owned_change`；O | 同作品本人会话，可能恢复轮次。 |
| POST `/projects/{project_id}/agent/changes/{change_id}/undo` | `agent.py:112` → `agent/service.undo_change:716` → `owned_change`；O | 执行效果先预检；记忆变化可由作者排向量任务。 |
| POST `/projects/{project_id}/agent/turns/{task_id}/confirm-all` | `agent.py:102` → `agent/service.decide_turn:634` → `_turn:347`；O | 本人 `agent.turn`、作品与会话关联。 |
| POST `/projects/{project_id}/agent/turns/{task_id}/reject-all` | `agent.py:107` → `agent/service.decide_turn:634` → `_turn`；O | 本人轮次，可能恢复任务。 |
| POST `/projects/{project_id}/agent/turns/{task_id}/undo` | `agent.py:117` → `agent/service.undo_turn:732` → `_turn`；O | 本人轮次与作品，效果预检。 |

以下任务服务受 `T` 过滤并在操作时复核作品/任务身份（`task_service.get_owned_task:369`、`_lock_user_task_project:379`），响应涉及 `personal_projection.project_payload`；活动长连接携带 actor、令牌版本和 `statistics_key`，引擎订阅时复验。`T` 不代表现在有协作者任务的完整创建/执行路径。

| 方法和路径 | 调用链；能力 | 父作品及特别限制 |
| --- | --- | --- |
| GET `/tasks` | `tasks.py:17` → `task_service.list_tasks:446`；T | SQL 先过滤本人和项目有效性再分页，并在投影后再次验证。 |
| DELETE `/tasks` | `tasks.py:33` → `task_service.clear_finished:700`；T | 仅本人可见已结束记录，按可访问项目锁定后清理。 |
| GET `/tasks/{task_id}` | `tasks.py:42` → `task_service.get_task:419` → `get_owned_task`；T | 本人及项目授权，私人引用投影。 |
| GET `/tasks/{task_id}/output` | `tasks.py:47` → `task_service.get_output:423`；T | 本人、task key 和授权复验。 |
| GET `/tasks/{task_id}/stream` | `tasks.py:52` → `get_owned_task` → `engine.subscribe`；T | 原活动状态及授权轮次在流中复验。 |
| POST `/tasks/{task_id}/cancel` | `tasks.py:67` → `task_service.cancel_task:509`；T | 锁内校验本人和当前项目授权；活动轮次须有效。 |
| POST `/tasks/{task_id}/retry` | `tasks.py:76` → `task_service.retry_task:267` → `create_task:208`；T + O 受理 | 先锁本人项目授权，当前通用创建后续仍严格作者。 |
| POST `/tasks/{task_id}/resume` | `tasks.py:83` → `task_service.resume_task:278`；T | 需有效活动授权，重验 handler 和富文档待审核条件。 |
| POST `/tasks/{task_id}/finish` | `tasks.py:90` → `task_service.finish_paused_task:323`；T | 当前授权及提案身份校验。 |
| DELETE `/tasks/{task_id}` | `tasks.py:113` → `task_service.delete_task:643`；T | 本人项目与可选提案身份；不能假设管理员可删除他人任务。 |
| GET `/tasks/{task_id}/rich-proposals/{token}` | `tasks.py:99` → `rich_document_review.get_review:265` → `_owned_proposal:218`；T | task key、run key、token 必须匹配本人可见任务。 |
| POST `/tasks/{task_id}/rich-proposals/{token}/accept` | `tasks.py:106` → `rich_document_review.accept_review:281`；T + O | 接受前仍严格作者作品/章节，不能称协作者提案采纳已接通。 |

## E 阶段及验证边界

1. 知识库上传、文档索引、全量索引、向量检索、章节检索上下文及本地规划上下文预览仍分别受上述旧 helper 限制。`ai.use` 前置不是模型或任务授权已完成的证据；需单独设计真实 actor、模型/费用来源和撤权边界。
2. `analysis_service.enqueue_auto:740`、`enqueue_auto_in_transaction:764` 会以作品作者排自动分析；`task_engine/handlers/chapter.py:175` 和 `rich_document_review.py:394` 是已核对的触发点。当前章节生成/采纳受理仍严格作者（`handlers/chapter.py:50`、`rich_document_review.py:281`），未来开放协作者之前必须重新设计派生任务身份，不能沿用作者入队。Agent 撤销记忆后也可能排作者向量（`agent/service.py:671`），当前同样由 O 守卫。
3. 本次独立细纲隐式删章验证：SQLite 单项包括他人 queued/running/paused、待审核、任务范围、作者/管理员、授权来源变化、非删除同步和多章原子性；真实 PostgreSQL 双连接使用专用 `collab_planning` 随机 schema，覆盖撤权、降权、他人任务、发送期间和锁前后章节集合变化，专项 1 passed。旧规划/生命周期/跨书级联/协作/Agent 回归组合 79 passed（分析可见性新改动之前）。专用 PG fixture 优先 `AINOVEL_COLLAB_PLANNING_POSTGRES_TEST_URL` 并回退标准 `AINOVEL_COLLAB_POSTGRES_TEST_URL`；仅专用变量路径本次实际执行，标准变量回退尚待错峰验证。分析状态/历史的请求前降权、修订、重邀及请求中途授权变化专项 7 passed；新增任务结束、整数 ID 替换、旧作者 UUID 缺失反例 4 passed。已结束本人任务和管理员兼协作者不因管理员人工权限放行其他活动任务。
4. 除第 3 项所述，本文件为当前代码静态复核，不代表逐个入口均已执行 HTTP、并发或外部模型测试；相关测试证据见阶段 D/E 文件。后端未配置独立 Python 类型检查器，运行测试不能替代类型检查。
