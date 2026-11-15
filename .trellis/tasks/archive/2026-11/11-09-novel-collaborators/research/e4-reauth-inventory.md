# Research: E4 重新鉴权与受限 CAS 清理现状盘点

- **Query**: 为 implement.md 阶段 E4（任务准备、受控模型/向量发送、Agent 工具、每批索引写、提案采纳和最终保存前重新鉴权；撤权或降级使旧工作失败关闭；按任务 UUID/轮次 CAS 回退占用）逐检查点核实当前代码，列出缺口、相关测试和建议实施切片。
- **Scope**: internal（只读静态调查，未运行测试；行号以 2026-11-11 工作树为准，工作区有大量未提交改动）
- **Date**: 2026-11-11
- **依据**: prd.md R3/R4；design.md §5.1、§5.3（134-138 行）、§6.2-6.3；implement.md 阶段 E 与 2026-11-10/11 进度记录。

## 0. 共用原语（E1-E3 已有）

| 原语 | 位置 | 行为要点 |
|---|---|---|
| `project_access.resolve_access/require_project(..., grant_identity=)` | `backend/app/services/project_access.py:70-150` | User 屏障 →（可选）`lock_visibility_write` → 项目父行锁 → 锁后 `populate_existing` 重读成员；`grant_identity` 不等于当前 `(invitation_id, revision)` 或非 accepted → 403 `permission_denied`；viewer 请求 `ai.use` → 403 `project_readonly`；撤销/非成员 → 404。 |
| `project_ai_config._access` / `_validate_binding` / `revalidate_project_model` / `revalidate_shared_embedding` | `backend/app/services/project_ai_config.py:385-398, 864-912, 915-925, 957-983` | 每次重验都重跑 `resolve_access`：要求 `can_use_ai`、作者或 accepted **editor**、grant 与绑定一致；`source=owner` 还要求当前 `allow_owner_ai`；配置行指纹/实例键一致；向量要求仍是作者默认向量配置。 |
| `collaboration_task_access.task_visible_to_actor(active_grant_required=True)` | `backend/app/services/collaboration_task_access.py:82-115` | SQL 谓词：Task 本人 + 账号启用（可带 token_version）+ 项目 active + （作者 或 accepted editor 且 Task 内 `_collaboration_grant` 与当前成员轮次逐字一致）。 |
| `execution_identity.require_write_execution` | `backend/app/services/task_engine/execution_identity.py:178-203` | 原 token 写屏障 → `lock_visibility_write` → `lock_active` → `SELECT … FOR UPDATE` Task 且套用上面谓词 → `matches_running`；失败回滚并抛 `task_execution_changed`。**不单独检查 `allow_owner_ai`**（依赖 revision 递增使 grant 失配，见 §3 注）。 |
| `collaboration_ai_guard.SendGuard`（`for_http`/`for_task`） | `backend/app/services/collaboration_ai_guard.py:417-846, 1058-1124` | `authorize`（发送 actor）→ 发送 lease 内 `_verify_locked`（项目 active 读锁、可见性戳、场景戳、Task 行 `_check_task`、文档/向量快照、章节 claim、实体来源、向导续写、`_verify_bindings`→上表重验）；`require_task_write`（644-710）在同一根事务按 User→门禁→项目→Task→claim→绑定重验并持锁到提交。 |
| `chapter_claim` | `backend/app/services/chapter_claim.py:242-249, 364-472` | `_authorize` = `require_project(ai.use, grant_identity=claim.grant_identity)`；`require_running_chapter`/`require_sending_chapter` 均先鉴权；`release_chapter_claim` **不要求仍是成员**，只按 task_id/task_key/queued_at/execution_key/claim_key/prev_status CAS。 |
| `kb_index_service` 文档 claim（0045） | `backend/app/services/kb_index_service.py:141-423` | `require_writing_document_claim`/`require_sending_document_claim` 含 `require_project(ai.use, grant)`；`release_document_claim` 只按原 task UUID/queued_at/claim_key CAS，置 `failed` 并清三列。 |
| 撤权写入 | `backend/app/services/collaboration_service.py:288-328`（`_lock_parent` 203-206） | User → `lock_visibility_write` → `lock_active` → 改 status/role/allow_owner_ai，**任何变更 `revision += 1`**（含放宽），同事务审计。 |
| 本地通知 | `backend/app/services/task_engine/engine.py:1672-1682` | `notify_collaboration_change` 只唤醒**订阅者**（SSE 队列），不触达运行中的执行体；由 `api/v1/collaboration.py:35-99` 在提交后调用。 |

## 1. 检查点现状

### 1.1 任务准备（受理 / 领取 / build_job）

**受理** `task_service.create_task`（`backend/app/services/task_service.py:528-558`）：handler 校验后，仅当 `accepted_model` 存在且 handler 同时声明 `execution_identity_aware` 与 `collaboration_execution_ready` 时调用 `require_project(ai.use, for_update, visibility_write, grant_identity=accepted_model.grant_identity)`；否则 `get_owned_project(lock=True)`。全仓 **没有任何 handler 声明 `collaboration_execution_ready`**（grep 仅见读取处：`task_service.py:550`、`engine.py:680,801`、`api/v1/tasks.py:118`）。→ 作者：严格作者锁；协作者：一律 404。

各 handler 的 validate 也仍有作者专用调用：`batch_service.py:265,304,457`、`book_import/reverse.py:464,496,585`、`outline_ai_service.py:259,342`、`entity_ai_service.py:740,770,1056`、`wizard_service.py:477,745`、`kb_index_service.py:464`（`validate_index_task`）、`analysis_service.py:787`。唯 `ChapterHandler` 已用 `require_project(..., grant_identity)`（`handlers/chapter.py:163`、`generation_service.py:854`）。

**领取** `TaskEngine._claim_and_run`（`engine.py:608-772`）：
- `authz.gate` → 锁 User(actor+owner) →（单章类）`lock_visibility_write` → `lock_active`；`from_queued_task` 冻结身份后仅 `authz.require_authenticated(actor)`（原 token）。
- 带 grant 的任务若 handler 未就绪 → `return`（684 行注释“历史身份或原认证已失效：不得从当前用户补签”同样覆盖 token 失效）。**不调用 `require_project`/成员轮次核对**；CAS 条件只有 `visible_task_project_exists()`（`task_service.py:102-105`，仅项目生命周期）。
- 结果：撤权/降级/账号停用/令牌变更后，排队任务**既不运行也不失败**，保持 `queued`；`_dispatch`（486-528）每次轮询重新派发→立即返回，并在本轮把 `serial_key` 记为 busy（同串行键后续任务被阻塞）；`uq_tasks_active_resource`（`models/entities.py:797-800`）使同资源键（如 `chapter:N`、`kbdoc:N`、`batch:P`）无法再受理新任务；单章类排队 claim 使章节保持 `generating`。

**`_run`**（`engine.py:773-880`）：handler 未就绪或身份不符时 `hub.close` 后直接 `return`（796-805、873-878），**不调用 `_finalize`/`on_abort`**，仅 `remember_abort_failure(project_id, None)`；Task 留在 `running`，靠重启 `recover()`（304-421）或管理员 drain 收尾。

**build_job**：
- 章节 `handlers/chapter.py:247-301`：`require_write_execution` + `require_running_chapter`（均含成员轮次）。作者/协作者同一路径。
- KB `kb_index_service.build_index_job`（804-871）：bound-memory 分支要求 `grant_identity is None` 并 `get_owned_project`；文档/rebuild/旧 memory 分支 `project.user_id != task.user_id → 404`（844 行），embedder 来自 `resolve_embedder(task.user_id)`（`retrieval_service.py:538-561`，无 guard）。
- outline/entities/wizard/agent/batch/analysis/book_import 的 build_job 均对 `grant_identity is not None` 显式拒绝（`handlers/outline.py:118`、`handlers/entities.py:122-123`、`handlers/wizard.py:92`、`handlers/agent.py:302-318`、`batch_service.py:443`、`analysis_service.py:811,891`、`book_import/reverse.py:566`），并用 `require_write_execution` 或 `get_owned_project`。

撤权/降级时：作者任务不受影响；协作者任务当前无法受理；若存在带 grant 的历史排队任务则卡在 `queued`（见 §2）。

### 1.2 受控模型发送

已经过 `SendGuard`（每跳重验成员+grant+模型来源）的生产路径：
- 后台 `for_task`：`generation_service.py:1156,1299,1465`、`handlers/chapter.py:312`、`handlers/batch.py:112`、`analysis_service.py:2074`、`outline_ai_service.py:371,531`、`entity_ai_service.py:1001,1216`、`wizard_service.py:786`、`book_import/reverse.py:598,715`、`handlers/book_import.py:84`、`kb_index_service.py:1084`。
- 同步 `for_http`：`rewrite_service.py:364`、`rich_document_section_ai.py:213`、`cover_service.py:162`（`_COLLABORATION_IMAGE_ENABLED = True`，`cover_service.py:28`）。

只用普通 `guard_provider`（仅可见性戳，不含成员/grant）的路径——均限于 `execution is None` 的作者直调/无任务分支：`wizard_service.py:891-895`、`outline_ai_service.py:430,474`、`entity_ai_service.py:1229`、`analysis_service.py:2086`、`rewrite_service.py:361`（binding 为 None 时）。Agent 用 `agent/context.py:370` 的自定义 authorize/verify，但 `context.py:194-207,262` 显式拒绝 grant。

撤权/降级时：协作者同步改写/区块改写/封面的下一跳发送在 lease 内 `_verify_bindings` → `_access` 失败 → `CollaborationSendBlocked`（`permission_denied`）；作者路径只重验 token/可见性。已有测试覆盖（§3）。

### 1.3 向量（embedding）发送

- 受控：`kb_index_service._run_bound_memory`（1070-1109，`for_task` + `embedding_inputs`）、`retrieval_service.require_sending_embedding_snapshots`（约 1030-1082，含 `require_project(ai.use, grant)` 与 `matches_running`）、`require_sending_document_claim`（`kb_index_service.py:374-411`）。
- **未受控**：`_embed_stale`（`kb_index_service.py:913-968`）直接 `embedder.embed`（956），embedder 由 `resolve_embedder(task.user_id)` 构造，未经 `SendGuard`/lease；仅作者任务可达（844 行守卫）。
- 同步检索：协作者无作者授权时只关键词；`allow_owner_ai=True` 的协作者检索/上传 → `collaboration_embedding_unavailable`（`kb_service.py:49-50,150-163`）；`test_shared_http_knowledge_e6.py` 整文件 strict xfail（E6）。

### 1.4 Agent 工具执行

- 全链路严格作者：`agent/service.py:295-299`（`_lock_owned_project`→`get_owned_project`）、`302-320`、`675-705`（`owned_change`/`_turn`→`lock_author_visibility_write(manage=False)`→`get_owned_project`，`setting_ai_visibility.py:78-101`）、`agent/runner.py:599,892`（`get_owned_project`）。
- 每步写（`_lock_execution` → `require_write_execution`，`runner.py:339-353`；`_checkpoint` 356-371；`_result`）作者有 token/项目/Task 重验；读工具 `tool.run` 本身不单独重验，但结果落库前 `_result` 会重验。
- 撤权/降级：协作者无法进入；作者不受影响。E5a（SAVEPOINT 审计边界）是开放协作者 Agent 的前置。

### 1.5 每批索引写

- bound memory：每批 `guard.require_task_write`（`kb_index_service.py:1099`）+ `require_bound_index_job`（992-1022，作者限定）。
- 文档模式：`_lock_running_document`（275-288）只锁项目 active + Task CAS + 文档 claim，**无认证/成员/token 重验**；`_embed_stale` 每批写前同样只调 `_lock_running_document`（960），rebuild/旧 memory 模式**无任何重验**。现仅作者可达。
- `retrieval_service.require_writing_embedding_snapshots`（约 1150-1180）含 `require_write_execution` + `require_project(grant)`，供受控路径使用。
- 协作者 keyword 上传是同步 HTTP 写（`kb_service.py:150-200` `_lock_search_result` 等），已带 grant 重验。

### 1.6 提案采纳

- 富文档审阅：`rich_document_review._accept_review`（`rich_document_review.py:1020-1050`）`require_project(ai.use, for_update, visibility_write, grant_identity=原 grant)`，`ForbiddenError` 转 404；协作者还要求原 token 版本。待审核期间的 `verify_single_pending_event`（572 起）与 `_persist_pending` 也带 `task_visible_to_actor(active_grant_required=True)`。
- 同步改写采纳：`rewrite_service.py:525-560`（非作者要求本人成功日志 + `_trusted_log_grant` + `require_project(grant)`）；区块：`rich_document_section_ai.py:267`。
- Agent 提案：严格作者（1.4）。

### 1.7 最终保存

- 单章/批量：`generation_service.py:1459-1480` 先 `guard.require_task_write`（有 execution）再 `_actor_chapter_project(..., grant, lock=True)`（`generation_service.py:296-325`）；引擎 `done/paused` 事件 `_event_task`（`engine.py:882-916`）+ `ChapterHandler.verify_event`（`handlers/chapter.py:303-343`，完成凭据 + `revalidate_project_model`）。
- 分析：`analysis_service.py:2165`、`handlers/analysis.py:109` `require_task_write`；`analysis_service.py:1302,1421,1456,1963` `require_project(grant)`。
- 大纲/实体/向导/导入：`handlers/outline.py:168,188`、`handlers/entities.py:171,198`、`entity_ai_service.py:1362`、`wizard_service.py:190-296,744,1266`（`require_write_execution`）、`handlers/book_import.py:88`。
- 引擎每个事件与 `_flush`（`engine.py:964-1076`）对 `execution_identity_aware` handler 调 `verify_event`；非单章类走 `require_write_execution`。
- 封面：`cover_service.py:183` `require_project(ai.use, for_update, grant)`。

## 2. 占用状态与清理路径

| 占用 | 回退路径 | 是否按 UUID/轮次 CAS | 已知遗留 |
|---|---|---|---|
| 章节 `generating` + `generation_claim_*`（0047 类） | `ChapterHandler.on_abort`→`release_chapter_claim`（`handlers/chapter.py:369-391`、`chapter_claim.py:422-472`）；批量 `restore_current`（`batch_service.py:564-576`）；启动恢复 `capture_running_claim`（`engine.py:383-391`） | 是：task_id+task_key+queued_at+execution_key+claim_key+prev_status，且不要求成员身份 | (a) `_fail_reset_queued_chapter`（`engine.py:566-606`）在 `project.user_id != claim.actor_id` 时直接 `return`（574 行），协作者排队章节任务在 claim 被人工复位后永不失败；且先 `require_authenticated(actor, write=True)`，账号停用时抛错。(b) 旧 `generating` 行无 claim 键时只能人工 `reset_generating_chapter`（475 起）。(c) 带 grant 的排队任务在领取前被跳过（1.1），claim 不会被释放。 |
| `Task.execution_key`/`running` | `_finalize`→`complete_state`；`recover()` | 是（statistics_key+queued_at+execution_key） | `_run` 早退（796-805、873-878）不收尾，只记 `remember_abort_failure`，需重启/管理员 drain。 |
| `Task` 排队行（resource_key 唯一、serial_key） | 无系统失败路径 | — | 撤权/降级/停用/令牌失效后永远 `queued`，阻塞同资源受理与同串行键调度（1.1）。 |
| 知识库文档 `index_task_key/queued_at/claim_key`（0045） | `KbIndexHandler.on_abort`→`kb_index_service.on_abort`（1231-1248）→`release_document_claim`（414-423） | 是，含 cleanup 终态集合，不要求成员 | 无 claim 的旧排队文档任务：`queued_document_claim` 返回 None → 不领取、不释放；implement.md:115 记录为 `task_cleanup_unproven` 阻塞删除，与 design §5「不留永久锁」存在张力（`test_task_execution_fencing.py:190`）。 |
| 分析 `_analysis_pending` | 已知失败只清本轮 pending（`test_collaboration_analysis_execution.py:978,992`） | 是 | `recover()` 对 `chapter.analyze`/`wizard.*` 一律记为待核对（317-323），不自动清理。 |
| 富文档审阅 pending | `_durable_rich_review` 保留 paused（`engine.py:1100-1114`） | 是（proposal id/token + task 轮次） | 撤权后由采纳端 404 失败关闭；待审核记录本身不自动丢弃。 |
| Agent `AgentChange.status=executing`、外部调用 claim | `recover_external`（`agent/service.py:102-283`） | 是（`_external_claim_id`/task_key） | 作者专用。 |
| book_import | `on_abort` 无回退（`handlers/book_import.py:111-115`）；implement.md:112 记录失败关闭时 status=failed、execution_key 清空 | — | 作者专用。 |

## 3. 相关测试

### 3.1 strict xfail（reason 含「待实现：」）

| 文件::用例 | 关联 | 预计何时转正 |
|---|---|---|
| `tests/test_collaboration_chapter_execution.py:52-56` `test_editor_chapter_task_is_attributed_to_real_actor` | 章节受理开放（`collaboration_execution_ready`） | 与 E4「章节入口开放」切片一起 |
| `tests/test_collaboration_chapter_execution_postgresql.py:178-184` `test_editor_reinvite_after_send_cannot_save_old_result` | 章节保存前按原轮次重验（E4 核心场景） | 同上，需真实 PG |
| `tests/test_collaboration_editor_proposal.py:303-309` `test_editor_proposal_analysis_runs_as_original_actor_with_frozen_model` | 分析 handler 就绪；reason 写明“任务保持 queued” | 分析入口开放切片（E4/E3 交界） |
| `tests/test_collaboration_notifications.py:50-55`、`tests/test_collaboration_notifications_postgresql.py:20-25`、`tests/test_collaboration_task_privacy.py:130-134`、`tests/test_collaboration_task_postgresql.py:41-44` | `/tasks/{id}/stream` 协作者 404（`api/v1/tasks.py:70-73`） | **E5**（SSE/帧发布身份），非 E4 |
| `tests/test_shared_http_knowledge_e6.py:13-14`（整文件） | 同步共享向量上下文 | **E6** |
| `tests/test_agent_mcp_skills.py:95-99`（6 例）、`tests/test_agent_authenticated_actor.py:324` | 自动 MCP 外部调用真实发送门禁 | Agent/E5a 专项，非 E4 主线 |
| `tests/test_api_key_actor_fencing.py:17`（11 例）、`tests/test_user_token_identity_reuse.py:8` | 平台 Key / JWT 身份复用 | 认证专项，与 E4 无直接关系 |

### 3.2 已通过、E4 可直接复用或需扩展的测试

- 发送守卫撤权：`tests/test_collaboration_ai_guard.py`（39 例，如 86、181、257、644、878、962 行）及 `_postgresql.py`。
- 章节 claim：`tests/test_chapter_claim_core.py:228` `test_revoked_editor_cannot_save_but_system_can_release_original_claim`、`:198`、`:92`；`test_chapter_claim_postgresql.py`、`test_chapter_claim_author_*`、`test_batch_chapter_claim*.py`。
- 章节执行：`tests/test_collaboration_chapter_execution.py:81,142,232,606`。
- 文档 claim：`tests/test_kb_index_claims.py:36,57,76,96,122`、`tests/test_task_execution_fencing.py:99-277`（含 `:190` 旧任务保守失败关闭）、`tests/test_kb_document_claim_send_e6.py`、`tests/test_kb_index_identity_postgresql.py`。
- 执行身份：`tests/test_task_execution_identity.py`、`tests/test_task_execution_fencing_postgresql.py`、`tests/test_task_event_guard.py`。
- 提案：`tests/test_collaboration_review_claim.py`、`test_collaboration_task_review.py:316`、`test_collaboration_editor_proposal.py`（非 xfail 部分）。
- 分析：`tests/test_collaboration_analysis_execution.py:418` `test_analysis_rejects_accepted_editor_before_any_model_send`、`:1330`。
- 关闭态断言（开放入口时需改写）：`tests/test_entity_ai_execution_identity.py:682`、`tests/test_wizard_collaboration_closed.py`、`tests/test_task_sse_scope.py`（`test_collaborator_stream_fails_closed_without_engine_frame_proof`，E5）。

未找到：针对“排队任务在撤权/令牌失效后被系统失败并释放 claim”的测试；针对 `_fail_reset_queued_chapter` 协作者分支的测试；针对 `_run` 早退后无需重启即可收尾的测试。

## 4. 缺口归纳（与 E4 原文逐项对应）

1. **任务准备**：领取阶段只验 token，不验成员/grant；带 grant 任务在 handler 未就绪时被永久留在 `queued`（`engine.py:672-686`）。作者 token 失效同样留排队。
2. **受控模型/向量发送**：协作者可达路径已覆盖；作者旧 KB 文档/rebuild 的 `_embed_stale` 未经 lease/guard（E6 范围，但影响 E4“每批”要求）。
3. **Agent 工具**：协作者整体关闭；开放需替换 `get_owned_project` 系列并先完成 E5a。
4. **每批索引写**：文档/rebuild 分支每批无认证与成员重验（仅作者可达，E6 改 `IndexJob` 时一并补）。
5. **提案采纳/最终保存**：章节、分析、富文档、同步改写已按原 grant 重验；Agent 采纳仍作者专用。
6. **撤权使旧工作失败关闭**：运行中工作在下一次检查点失败（依赖 DB），但：运行中执行体不会被本地通知（通知只到订阅者）；排队工作不会被失败。
7. **受限 CAS 清理**：章节/文档 release 原语已就绪且不要求成员；缺的是“谁来调用”：排队撤权任务的系统失败 + claim 释放、`_fail_reset_queued_chapter` 的协作者分支、`_run` 早退收尾、旧无 claim 文档任务的清理决策。

## 5. 建议 E4 实现切片

> 仅陈述可独立验证的分片与依赖，具体取舍由主会话决定。每片都须 SQLite + 独立 PostgreSQL（`AINOVEL_COLLAB_POSTGRES_TEST_URL`）各跑一遍。

### 不开放协作者入口即可完成/测试

**E4-a 领取期授权复核 + 排队任务系统失败（核心）**
- 改动：`task_engine/engine.py`（`_claim_and_run` 在 CAS 前于同事务锁序 User→门禁→项目后，对 grant 任务以 `task_visible_to_actor(active_grant_required=True)` 或 `require_project(ai.use, grant)` 判定；失效时走新的“系统失败”CAS：`status=failed`、按 task UUID/queued_at/`execution_key IS NULL` 条件，并调用 `chapter_claim.release_chapter_claim`（queued phase）/`kb_index_service.release_document_claim`，记统计）；token 失效/账号停用沿同一路径；`task_service.py` 可能需要共享的 STAGE/统计辅助。
- 测试：直接构造带 `_collaboration_grant` 的 queued Task（测试内插库，绕过受理）→ 撤权/降级/重邀/`allow_owner_ai` 变更后引擎轮询：Task 变 failed、章节回到 prev_status、文档变 failed、同 resource_key 可再次受理、同 serial_key 后续任务被领取；作者 token 变更同样收尾；旧轮次清理不影响新轮次（先失败旧任务、再受理新任务、旧清理迟到）。PG：撤权与领取两种提交顺序双连接。
- 风险：与管理员取消（`admin_cancel_operation_id`）、`_cancel_requested`、`cleanup_scope`、项目删除 `task_cleanup_unproven` 交互；不能在未证明执行体停止时释放 claim；统计 `record_state_change` 一致性；SQLite 全库写锁超时。

**E4-b `_fail_reset_queued_chapter` 协作者分支**
- 改动：`engine.py:566-606` 去掉/替换 `project.user_id != claim.actor_id` 早退，改为只按 claim/Task CAS（与 `release_chapter_claim` 一样不要求成员）；认证失败不应阻止系统失败。
- 测试：协作者 grant 排队章节任务 + 人工复位 → 任务 failed、不触碰当前章节；账号停用情形。
- 风险：确认不会误失败新轮次（已有 `capture_queued_claim(task) != claim` 守卫）。

**E4-c `_run` 早退收尾**
- 改动：`engine.py:796-805, 873-878` 在身份已证明属于本执行体时走 `_finalize`（failed + on_abort），否则保持现状记 `remember_abort_failure`。
- 测试：handler 未就绪/身份不符时 Task 不长期停在 running；章节 claim 释放；旧执行体不能收尾新轮次。
- 风险：`_run` 早退正是“身份无法证明”的保护分支，必须区分“可证明本轮”与“无法证明”。

**E4-d 本地执行体唤醒**
- 改动：`engine.notify_collaboration_change` 增加对 `(project_id, actor_id)` 运行中执行体的检查触发（如登记取消意图或标记下次检查），仍以 DB 为准。
- 测试：撤权后无需等待下一模型跳即停止（以 Fake provider 阻塞流 + 撤权断言终态）；跨实例仅依赖 DB 检查。
- 风险：不能把内存通知当授权依据；与 `cancel_running` 交互。

**E4-e 旧无 claim 文档任务的清理决策**（implement.md:115 未决）
- 改动：`kb_index_service.on_abort`/`queued_document_claim` 或项目删除路径；需先做设计裁决。
- 测试：`test_task_execution_fencing.py:190` 的预期需按裁决改写。

### 必须与入口开放一起做

**E4-f 章节单章入口开放**：`handlers/chapter.py` 声明 `collaboration_execution_ready=True`；核对 `task_service.py:549-558` 分支、`api/v1/tasks.py:118` 重试分支、`generation_service` 收据 schema 3 路径。转正 §3.1 前两项 xfail；新增“撤权/降级/重邀在受理后、领取前、发送前、保存前”四时点矩阵（SQLite + PG）。依赖 E4-a/b。
**E4-g 批量 / 分析 / 大纲 / 实体 / 向导**：逐一把 `get_owned_project` 换成 grant 版、移除 build_job 的 `grant_identity is not None` 拒绝并声明就绪；每类各自测试四时点矩阵。分析转正 `test_collaboration_editor_proposal.py:303`。`tests/test_entity_ai_execution_identity.py:682`、`test_wizard_collaboration_closed.py` 需改写。
**E4-h KB 索引**：与 E6 合并（`build_index_job` 844 行、`_embed_stale` 受控化、每批 `require_writing_*`）。
**E4-i Agent**：依赖 E5a；替换 `agent/service.py`、`agent/runner.py`、`setting_ai_visibility.lock_author_visibility_write(manage=False)` 的作者限定，读工具执行前是否需独立重验需设计确认。
**E5（不属 E4）**：`api/v1/tasks.py:70-73` 流、任务列表/输出/取消/重试/审核的协作者开放与对应 4 个 SSE xfail。

## Caveats / Not Found

- 未运行任何测试；“已通过”仅指 implement.md 记录或用例存在，不代表当前树结果。
- `research/phase-e-remaining-integration-map.md`（11-10）称“未发现 for_task 被生产调用”，当前树已有多处生产调用（§1.2），该文档对应结论已过时。
- 未核实 `admin_task_service`/管理员 drain 对带 grant 排队任务的处理细节，E4-a 实施前需补读 `task_engine/cancellation.py`。
- `require_write_execution` 不直接检查 `allow_owner_ai`；当前依赖 `collaboration_service.change_member` 对任何变更 `revision += 1`（`collaboration_service.py:320`）使 grant 失配。若将来出现不递增 revision 的授权变更，该假设失效。
