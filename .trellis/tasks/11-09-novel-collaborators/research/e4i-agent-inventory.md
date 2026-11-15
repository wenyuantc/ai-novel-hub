# Research: E4-i 协作者智能体（Agent）开放——现状盘点与切片

- **Query**: 为 E4-i「给协作者开放智能体」盘点现状（关闭点、工具、模型与费用、重鉴权、E5a 事务边界、隐私、SSE/任务面、前端、测试），给出切片和待裁决问题。
- **Scope**: internal（静态读码 + 任务文档；未运行测试，未改代码）
- **Date**: 2026-11-14
- **说明**: 行号以当前工作树为准（大量文件为未提交改动）。早期研究（`phase-e-agent-*.md`，2026-11-09~10）的部分描述已被后续 E3–E6 改动覆盖，以本文为准；与早期文档不一致处在文中注明。

---

## 0. 结论摘要

1. **协作者 Agent 在 6 层被关闭**：HTTP 服务层（`owned_conversation` / `_lock_owned_project` / `owned_change` / `_turn` 全部走 `get_owned_project`）、写屏障（`lock_author_visibility_write(manage=False)`）、任务 handler（`AgentHandler` 拒绝 grant / 非本人模型来源，且未声明 `collaboration_execution_ready`）、引擎领取/收尾（未 ready 的 grant 任务 `_mark_stalled` / 失败）、个人投影（`project_agent_payload` 默认 `owner_only=True`）、订阅白名单（`stream_kind_open` 要求 handler 有 `stream_guard`）。另有 MCP 发送租约、摘要执行检查两处内置 `Project.user_id == actor`。前端以 `canUseOwnerAI` 隐藏入口。
2. **E5a 基础设施已基本就绪，但没有接到真实 editor 路径**：`AgentAuditCoordinator` / `AgentAuditScope` / `TransactionSession(audit_mode)` 已把失败审计挪出 SAVEPOINT（`transactions.py:149-298`、`audit_writer.py:256-334`），且有管理员兼编辑者的集成测试——但该测试靠 monkeypatch 模拟“未来协作者入口”。`AgentAuthorizationChangedError` 已定义却**没有任何 raise 点**（`transactions.py:36`），授权失效目前在确认路径会被当成普通工具失败（`service.py:798-799`、`base.py:241-243`）。
3. **最大的功能风险是“人工能力并集”**：Agent 工具写入若只按 `content.edit` 判定，**viewer 兼 `projects.edit` 管理员**会经管理员来源通过；工具层必须同时要求 `ai.use` + 原邀请轮次（grant）。现有 `base._lock_write` 在 `visibility_locked=True` 时完全跳过复核（`base.py:169-173`，确认路径 `service.py:743` 即如此）。
4. **工具层大部分可复用人工入口的协作者分支**：服务函数普遍有 `manual=True` / `manage=True` 分支走 `project_access.require_project(content.read|content.edit)`，Agent 工具目前调用的是默认（严格作者或管理员 `get_manageable_project`）分支。Skill、风格、AIConfig、MCP 插件、任务列表已经按真实 actor 隔离。
5. **检索工具与 E6 冲突**：`search_memories` / `search_knowledge` 用 `retrieval_service.resolve_embedder(ctx.user_id)`（`read.py:328-336`），对编辑者会用**编辑者本人**向量模型去查作者空间，违反 E6 裁决 1；后台任务里也无法复用 HTTP 专用的 `kb_service.search_access`（它依赖 `for_http`）。需要在受理时冻结 `_shared_embedding` 并以 `for_task` 发送，或对编辑者退化为关键词。
6. **`start_task` 白名单 10 类全部是 `admission_preflight_aware` 且 `collaboration_execution_ready`**（章节 3 类、批量、分析、大纲 2 类、角色/组织/职业生成）。签名提案 `sign_origin` / `execute_admitted` 链已存在，但其中 `_bound_actor`、`_require_origin_facts` 和 `create_task(caller_transaction=True)` 对编辑者的 grant 透传尚未验证。
7. **任务面**：`agent.turn` 不在受控订阅白名单、不在排队/待审核受限收尾白名单（`engine.py:141-147`）。开放后编辑者被撤权时，其 paused 的 Agent 轮次会永久停在 paused（E5-a 遗留），需纳入收尾或单独设计。
8. 建议顺序：**E4i-a（E5a 收口：授权失效错误与工具层能力判定）→ b（受理/handler/模型）→ c（工具迁移与检索）→ d（确认/撤销/提案任务）→ e（引擎收尾与订阅）→ f（前端）→ g（PG 与 E7 端到端）**。

---

## 1. 协作者 Agent 当前被关在哪里

### 1.1 HTTP 路由（`backend/app/api/v1/agent.py`）

路由前缀 `/projects/{project_id}/agent`（`agent.py:49`）。`mutation_db`（`agent.py:28-43`）只校验原认证快照并开 RR 会话，**不做项目授权**；授权全部在 service。

| 路由 | 入口 → 关闭点 | 现状 | 打开需要满足 |
|---|---|---|---|
| `GET /conversations`（`agent.py:52`） | `service.list_conversations` → `get_owned_project`（`service.py:451-452`） | 编辑者/viewer 404 | 改为 `require_personal_project`（作者或 accepted 成员，`personal_projection.py:420-426`）或 `require_project(ai.use)`，取决于 Q4；查询仍按 `AgentConversation.user_id == actor`（`service.py:456-458`，已具备） |
| `POST /conversations`（`agent.py:57`） | `create_conversation` → `_lock_owned_project`（`service.py:473`、`295-299`） | 404 | 认证写屏障 → 发送写门禁 → 作品锁 → 锁内 `ai.use`（editor），`auto_execute` 继承“本人本作品最近会话”（`service.py:476-490` 已按 user 过滤） |
| `GET /conversations/{cid}`（`agent.py:64`） | `owned_conversation`（`service.py:302-320`）→ `get_owned_project`；随后 `project_agent_payload` 默认 `owner_only=True`（`personal_projection.py:568-571`） | 双重关闭 | `owned_conversation` 换成协作者感知的读取（按 Q4 决定 viewer 是否可读）；`get_conversation` 调投影时显式 `owner_only=False`（参照 `public_changes` 的 `service.py:341`） |
| `PATCH /conversations/{cid}`（`agent.py:71`） | `owned_conversation(lock=True)`（`service.py:540`） | 404 | 同创建的锁序；`mcp_plugin_ids` 已校验本人插件（`service.py:549-553`）；`auto_execute` 是否允许编辑者打开见 Q3 |
| `GET /conversations/{cid}/generation-logs`（`agent.py:78`） | `require_personal_project` + `AgentConversation.user_id` + `GenerationLog.actor_user_id`（`service.py:562-599`） | **已对 accepted 成员（含 viewer）开放本人历史日志**（测试 `test_collaboration_log_private_views.py:452`） | 无需改；只需与会话读取口径（Q4）一致 |
| `DELETE /conversations/{cid}`（`agent.py:83`） | `owned_conversation(lock=True)`（`service.py:604`） | 404 | 同写屏障；保留“未决外部结果不能删”（`service.py:608-616`） |
| `POST /conversations/{cid}/messages`（`agent.py:89`） | `send_message`：先 `owned_conversation`（`service.py:631`）→ 锁外 `preflight_project_model`（`632-635`）→ `create_task`（`645-652`）→ `_lock_owned_project`（`657`） | 404 | 见 §1.2；`create_task` 对 `collaboration_execution_ready` handler 自动走 `require_project(ai.use, grant)`（`task_service.py:554-559`） |
| `POST /changes/{id}/confirm|reject`（`agent.py:98-109`） | `decide_change` → `owned_change`（`service.py:675-693`，lock 时 `lock_author_visibility_write(manage=False)`）→ `execute_change`（`service.py:718`） | 404 | 锁内 `ai.use` + 原 grant；授权失效须抛不可吞的策略错误（§4、§5） |
| `POST /turns/{task_id}/confirm-all|reject-all`（`agent.py:112-123`） | `decide_turn` → `_turn`（`service.py:696-704`）+ `get_owned_task`（`service.py:1314`） | 404 | 同上；`get_owned_task` 已按本人 + 协作可见（E5） |
| `POST /changes/{id}/undo`、`/turns/{task_id}/undo`（`agent.py:126-137`） | `_undo_changes` → `_lock_owned_project`（`service.py:1338`） | 404 | 锁内 `ai.use` + grant（撤销是 Agent 动作，不只是人工编辑，见 Q8） |

`lock_author_visibility_write(manage=False)` 本身是作者限定：`setting_ai_visibility.py:97-99` 先 `get_owned_project`。Agent 工具的 `lock_project`（`tools/base.py:72-79`）也调它。

### 1.2 `agent.turn` 任务（`backend/app/services/task_engine/handlers/agent.py`）

| 环节 | 关闭点 | 打开条件 |
|---|---|---|
| handler 能力声明 | `AgentHandler` 只有 `execution_identity_aware`、`admission_preflight_aware`（`agent.py:148-151`），**无 `collaboration_execution_ready`** | 声明 ready 后，`create_task` 才会对 grant 走 `require_project(ai.use, grant_identity=…)`（`task_service.py:554-559`），否则落到 `get_owned_project(lock=True)`（`task_service.py:575-576`） |
| 受理 `validate_and_claim` | `owned_conversation(lock=True)`（`agent.py:155`）；绑定必须 `project_owner_id == user_id`、`source == "actor"`、`grant_identity is None`（`agent.py:167-169`） | 接受 `source ∈ {actor, owner}`、`grant_identity == 锁内成员轮次`；`cfg = resolve_config(user_id, binding.config_id)`（`agent.py:170`）对作者回退配置会 404，需改为按 binding 的 `config_owner_id` 解析 |
| 重试 `validate_retry` | `GRANT_KEY in task.input` 直接拒绝（`agent.py:205-206`）；`project_owner_id=user_id` 反序列化（`agent.py:221-227`） | 原 grant 同轮次才允许；重邀后旧轮次拒绝（与 E4 其他类型一致） |
| 续跑 `revalidate_resume` | 同上（`agent.py:261`、`268-274`） | 同上 |
| 构建 `build_job` | `GRANT_KEY in task.input` 拒绝（`agent.py:302`）；绑定限 actor（`313-319`）；`require_write_execution`（`322`，**已支持 grant 复核**，见 `execution_identity.py:178-212`）；provider 用作者型 `guard_provider(provider, stamp, sessionmaker)`（`agent.py:402`） | 对 grant 任务改用 `collaboration_ai_guard.for_task(execution, binding, stamp, …)`（`collaboration_ai_guard.py:1461-1500`，已对 `agent.turn` 专门核对 `_agent_actor_token_version`，`1491-1493`） |
| 事件/收尾校验 | `verify_event` / `verify_finalization`（`agent.py:411-458`）只核对执行体与资源（`runner.verify_job_resources`，`runner.py:104-135`，内含 `AIConfig.user_id == job.user_id`，作者回退模型会失败） | 资源校验按 binding 的配置归属；帧发布需 `stream_guard`（§7） |
| 引擎领取 | grant 任务且 handler 未 ready → `_mark_stalled`（`engine.py:1259-1265`） | 声明 ready |
| 引擎运行/收尾 | 同理失败关闭 `"任务执行身份尚未确认"`（`engine.py:1401-1407`） | 声明 ready |
| HTTP 重试路由 | 非作者且 handler 未 ready → 409「协作任务重试暂不可用」（`api/v1/tasks.py:127-129`） | 声明 ready |
| 撤权受限收尾 | `agent.turn` 不在 `_COLLABORATION_QUEUED_CLEANUP_KINDS` / `_PAUSED_CLEANUP_KINDS`（`engine.py:141-147`） | 见 §7.2 |
| 订阅 | `stream_kind_open` 要求 handler 有 `stream_guard`（`stream_evidence.py:457-473`），协作者订阅 Agent 入口 404（`api/v1/tasks.py:71-75`） | 见 §7.1 |

### 1.3 其它硬编码作者校验

- MCP 真实发送租约：`Project.user_id == identity.actor.id`（`agent/mcp_tools.py:189-192`）；`deserialize_project_model(..., project_owner_id=project.user_id)`（`mcp_tools.py:254-257`）已用真实作者，但上面的项目查询会先拒绝编辑者。
- 摘要执行检查：`_summary_execution` 要求 `binding.project_owner_id == execution.user_id`、`grant_identity is None`（`context.py:202-207`）；`_check_summary_execution` 以 `Project.user_id == actor` 锁作品（`context.py:236-242`），并拒绝 `_collaboration_grant`（`context.py:262`）。
- 历史摘要：非作者会话只要有 `summary` 就 `AgentHistoryUnverifiable`（`context.py:782-784`）。对新开放的编辑者会话影响很小（新会话无旧摘要），但编辑者会话自身生成摘要后，**下一轮也会被这条规则拦下**——这条规则原意是挡“作者风格回执混入的旧摘要”，需要区分“本人在受控路径生成的摘要”。
- 个人投影：`owner_only` 默认 True（`personal_projection.py:568-571`）；`public_changes` 已显式置 False（`service.py:341`），`get_conversation` 未置。

### 1.4 前端门控

- `projectAccess.ts:49-50`：`canUseOwnerAI = role === 'owner' && can_use_ai`。
- `ProjectLayout.tsx:49-60`：「项目智能体」按钮与 `AgentDock` 仅 `canUseOwnerAI` 挂载；`?agent=open`（`ProjectLayout.tsx:31-33`，链接来源 `api/tasks.ts:207-208`）也只在挂载 AgentDock 时生效。
- `useAgentConversation.ts:36,114-115,139,156,163,168`、`AgentChangeCard.tsx:65`、`AgentTimeline.tsx:97` 的作用域复核都用 `'canUseOwnerAI'`。

---

## 2. Agent 工具清单与协作者口径

注册表：`tools/__init__.py:13`（读 + Skill + 写 + 正文 + 任务）；MCP 不在注册表（`mcp_tools.build_tool_map` 按会话本人插件）。

公共机制：`Tool.run`（读，`base.py:158-167`）、`Tool.preview`（`base.py:175-225`，SAVEPOINT 内调用 service 再回滚）、`Tool.execute`（`base.py:227-243`，SAVEPOINT）；写前 `_lock_write` → `lock_project` → `lock_author_visibility_write(manage=False)`（`base.py:169-173`、`72-79`）。

### 2.1 读工具（`tools/read.py:398-422`、`write.py:625`、`skills.py:104-108`）

| 工具 | 现调用 | 关闭方式 | 编辑者应有（capability） | viewer / 未授权编辑者 | 可复用的协作者入口 |
|---|---|---|---|---|---|
| `get_project_overview` | `project_for_model`（`read.py:142-161`），风格按本人/预设过滤 | 外层严格作者 | 读（`content.read`，Agent 整体需 `ai.use`） | viewer 无 Agent；未授权编辑者同编辑者（读不涉作者模型） | 已有 Agent 专用风格投影（`phase-e-agent-style-privacy.md`） |
| `list_characters` / `get_character` | `story_service.list_owned` / `get_owned`（`read.py:169-180`）→ `get_owned_project`（`story_service.py:62-75,183-185`） | 作者严格 | `content.read` | 同上 | `list_owned(manage=True)`、`get_collaborative`（`story_service.py:193-196`） |
| `list_world_settings` | `list_owned`（`read.py:220-222`） | 作者严格 | 同上 | 同上 | `manage=True` |
| `list_organizations` / `get_organization` | `organization_service.list_orgs/get_org/list_members`（`read.py:191-201`）→ `_read_project(manual=False)` = `get_manageable_project`（`organization_service.py:161-165`） | 管理员旁路可读，编辑者 404 | `content.read` | 同上 | `manual=True` |
| `list_careers`、`list_relationships` | `career_service._read_project`（`career_service.py:153-157`）、relationship 同类 | 同上 | 同上 | 同上 | `manual=True` |
| `get_outline_tree` / `get_outline` | `outline_service.outline_tree`（`read.py:228-240`；`outline_service.py:42-46`） | 默认严格（测试 `test_collaboration_planning_access.py:411` 断言编辑者 404） | 同上 | 同上 | `manual=True` |
| `list_chapters` | `chapter_service.list_chapters` → `get_manageable_project`（`chapter_service.py:220-221`）+ `analysis_service.project_states` | 同上 | 同上 | 同上 | `list_manual_chapters`（`chapter_service.py:228`）、`project_states(manual=True)`（`analysis_service.py:587-591`） |
| `read_chapter`、`get_chapter_analysis` | `get_project_chapter` → `get_owned_project`（`chapter_service.py:76-80`） | 严格作者 | 同上 | 同上 | 人工章节读取入口（`get_manual_chapter`） |
| `list_foreshadows`、字段/条目/时间线类 | `foreshadow_service`、`setting_field_service.list_definitions(manual=False)`（`setting_field_service.py:173-181`）、`setting_progression_service.list_timelines`（`write.py:332`） | 默认非协作 | 同上 | 同上 | 各自 `manual=True` |
| `search_memories` / `search_knowledge` | `resolve_embedder(ctx.user_id)` + `retrieval_service.search`（`read.py:328-360`） | 外层严格作者 | `ai.use`（检索是 AI 上下文）；**必须走作者共享空间**：未授权 → 关键词；授权 → 作者向量（E6 裁决 1） | viewer 无；未授权编辑者仅关键词 | HTTP 版 `kb_service.search_access`（`kb_service.py:171-229`）**依赖 `for_http`，不能用于后台**；后台应参照大纲/实体：受理时冻结 `_shared_embedding`（`handlers/outline.py:53-76`）+ `for_task` 发送 |
| `list_writing_styles` | `style_service.list_styles(ctx.user_id)`（`read.py:371-372`） | 外层 | 本人风格 + 预设 | — | 已按 actor |
| `list_active_tasks` | `Task.user_id == ctx.user_id`（`read.py:375-386`） | 外层 | 只看本人任务 | — | 已按 actor |
| `list_skills` / `load_skill` / `read_skill_reference` | `skill_service`（本人或全局，`references.py:97`） | 外层 | 本人 Skill | — | 已按 actor |

**读工具执行前重验**：`Tool.run` 只在有 `scope` 时 `scope.verify`（`base.py:162-163`），不取写锁、不复核成员；后台循环中读工具结果写入前由 `_result → _lock_execution → require_write_execution` 复核（`runner.py:374-384`、`341-355`），该复核包含 `task_visible_to_actor(active_grant_required=True)`（`execution_identity.py:198-209`）。即：撤权后读到的结果**不会被写入会话**，但读本身发生在复核前（只读数据库，不出站）。

### 2.2 写工具（`kind ∈ small | delete`，`write.py:558-643`）

`update_project`；`create/update/delete_` × {character, world_setting, outline, organization, relationship, career, foreshadow}；`update_setting_field_baseline`、`record_setting_field_progression`、`update_setting_baseline`、`record_setting_progression`、`set_org_member`、`remove_org_member`、`assign_career`、`remove_career_assignment`、`update_chapter_plans`、`update_chapter_meta`。

| 项 | 内容 |
|---|---|
| 编辑者应有 | 与人工编辑相同的写范围（`content.edit`），**且**本轮 Agent 授权（`ai.use` + 原 grant） |
| viewer | 不能进入 Agent（无 `ai.use`） |
| 未授权编辑者（`allow_owner_ai=false`） | 写工具不涉作者模型，与已授权编辑者相同（模型来源只影响对话模型，见 §3） |
| 现调用 | `story_action(story_service.create_owned/update_owned/delete_owned)`（`write.py:122`、`564-577`），默认 `manage=False` → `lock_author_visibility_write(manage=False)` 作者严格（`story_service.py:206-218`）；组织/职业等 `_write_project(manual=False, visibility_write=True)` → `lock_author_visibility_write`（默认 `manage=True`，管理员可过）（`organization_service.py:168-178`）；`update_project` 已是 `require_project(content.edit)`（`project_service.py:555-563`，且非作者禁改风格/AI 设置）；`update_chapter_meta` → `chapter_service.update_chapter(manual=False)`（`write.py:553`、`chapter_service.py:339-350`） |
| 可复用 | 各 service 的 `manage=True` / `manual=True` 分支（`require_project(content.edit, for_update=True, visibility_write=True)`）；人工路由已全部走这些分支（阶段 D） |
| 风险 | ① `manual` 分支只要求 `content.edit`，**管理员人工能力会并入**；Agent 写必须另加 `ai.use` + grant 复核（见 §4）。② `manual=True` 是否带有“人工专属”副作用（如人工版本来源、审计 action 名）需逐个核对，未逐行验证。③ 测试 `test_collaboration_story_access.py:38`、`test_collaboration_planning_access.py:411`、`test_collaboration_content_access.py:103` 断言**默认（非 manual）helper 仍作者严格**；若 Agent 改为传 manual 而不改默认 helper，这些断言无需翻转。 |

### 2.3 正文工具 `replace_chapter_text`（`write_text.py:91`，kind `text`）

- 锁章节行、拒绝 `generating`、`ensure_outside_batch`、经 `scene_service.replace_scene_content` + `version_service.record(..., "agent")`（`write_text.py:21-40`、`63-90`）。项目权限只靠外层 `_lock_write`。
- 编辑者：`content.edit` + `ai.use` + grant；需确认编辑者持有的章节 claim（E4 单章）与 `generating` 判定的关系（未逐行验证）。
- 版本 `source="agent"` 的 `generation_log_id` 投影已按 actor 过滤（E2）。

### 2.4 启动任务 `start_task`（`tools/tasks.py:517-519`）

- 白名单 `AllowedKind`（`tasks.py:41-63`）：`chapter.generate/polish/revise/batch/analyze`、`outline.expand/continue`、`character/organization/career.generate`。**不含** `relations.*`、`wizard.*`、`kb.index`。
- 全部 `admission_required`（`tasks.py:72-75`）→ 通用 `execute` 失败关闭（`tasks.py:263-268`），只能经确认服务的 `execute_admitted`（`tasks.py:487-514`）。
- 链路：runner 写提案时 `sign_origin`（`runner.py:680-686`）→ 确认 `prepare_confirmed_admission`（锁外预检，`tasks.py:424-460`）→ 锁内 `execute_admitted` → `create_task(..., admission=…, caller_transaction=True)`。
- 这 10 类 handler 均已 `collaboration_execution_ready = True`（chapter `:168`、analysis `:24`、batch `:141`、outline `:33`、entities `:45`），中央 `create_task` 对 ready handler 已按 `require_project(ai.use, grant)` 受理（`task_service.py:554-559`）。
- 编辑者口径：可启动与其人工入口一致的任务（这 10 类人工入口均已对编辑者开放），任务归编辑者、模型按本人优先/授权回退。
- 待核实缺口：`_bound_actor`（`tasks.py:91-96`）、`_require_origin_facts`（`tasks.py:463-484`，要求 `turn.user_id == actor.id`，编辑者自己的轮次满足）、`_validate_spec` 的 `resolve_project_model` 与 `preflight` 比较（`tasks.py:186-201`）在 grant 场景是否一致；`caller_transaction=True` 路径对 grant 的处理未在测试中覆盖。
- **隐私待核实**：`preview` 把 `spec.input.get("ai_config_id")` 写入提案（`tasks.py:243`）。编辑者走作者回退时，该值若为作者配置 ID，会进入 `AgentChange.preview`（模型可见、编辑者可见）。个人投影会收集 `configs` 引用并按本人过滤（`personal_projection.py:681-684`），但**模型上下文**不经该投影（`context.change_result_for_model`），需在开放前确认/补测。

### 2.5 MCP / 外部工具

- 解析：`mcp_tools.build_tool_map(db, settings, job.user_id, conversation)`（`runner.py:856`）、`resolve(..., job.user_id, ...)`（`runner.py:640-642`）、插件归属 `McpPlugin.user_id == actor`（`mcp_tools.py:213-215`、`service.py:549-553`）——**只用编辑者本人插件**。
- 手动确认：`_execute_external`（`service.py:860` 起）先提交 `executing` 再发网，真实发送租约 `_send_lease`（`mcp_tools.py:163-270`）锁内核对设置门禁、可见性、作品、任务、会话、提案、插件、模型绑定——**项目查询作者限定**（`:189-192`）。
- 自动调用：`_require_auto_external_transport` 恒抛（`runner.py:138-142`），作者也关闭；两处 strict xfail（`test_agent_mcp_skills.py:99`、`test_agent_authenticated_actor.py:324`）。
- 编辑者口径见 Q2（是否允许把作品内容发往编辑者本人插件）。

---

## 3. 模型解析与费用

| 项 | 现状 | 开放后 |
|---|---|---|
| 对话模型 | `send_message` 锁外 `preflight_project_model(actor, project_id, "text", config_id=req.ai_config_id)`（`service.py:632-635`），该函数已实现“本人显式 → 本人默认 → 本人无配置且 `allow_owner_ai` 时作者默认”（`project_ai_config.py:548` 起，E1）。但 `AgentHandler` 拒绝 `source != "actor"` 或带 grant（`handlers/agent.py:167-169`） | 放开 handler：binding 冻结在 `_project_model`；`cfg` 按 binding 的 `config_owner_id` 读取；`verify_job_resources` 的 `AIConfig.user_id == job.user_id`（`runner.py:122-128`）改按 binding 归属 |
| 是否允许未授权编辑者用本人模型 | `project_access` 中 `ai.use = owner or editor`（`project_access.py:65`），与 `allow_owner_ai` 无关 | 按 R4，未授权编辑者用本人模型可开 Agent（见 Q1） |
| 每次模型发送 | 作者：`guard_provider` + `context.guard_agent_provider`（`agent.py:402`、`context.py:373-388`） | 编辑者：`collaboration_ai_guard.for_task(...).prepare_provider`，每跳租约内重验 actor/令牌、原 grant、`allow_owner_ai`（作者来源时，`execution_identity.py:206-209`）、配置；摘要发送需同等 guard |
| 检索嵌入 | `resolve_embedder(ctx.user_id)`（`read.py:330-336`） | 受理时 `resolve_shared_embedding`（`project_ai_config.py:928-954`）冻结 `_shared_embedding`；运行时 `for_task(..., extra_bindings/embedding)` 发送；未授权 → 关键词；作者无配置 → 关键词；作者配置本地不可用 → 关键词（E6 裁决 16/17 同口径） |
| 费用归属 | `_save_log` 写 `actor_user_id=job.user_id`、`model_binding`、token 用量（`runner.py:781-825`） | 已按真实 actor；`model_binding` 记录 `source=owner` 时即作者密钥计费（与 E4/E6 一致）。Task 归编辑者（`task_service.create_task` 的 `user_id`），并发额度按编辑者 |
| 派生任务费用 | `start_task` 派生 Task 的 `user_id` 继承轮次 actor（spec `agent.md:202-215`） | 不变；派生任务各自按其 handler 的本人优先/授权回退重新解析模型（不继承 Agent 对话模型来源），需在 Q5 中确认 |
| 撤销后记忆索引 | `_undo_changes` → `kb_index_service.enqueue_memory`（`service.py:1371-1376`），该函数对非作者 `NotFoundError`（`kb_index_service.py:1130-1134`）被 `except Exception` 吞为 warning | 编辑者撤销后应显式不派生或走 E6 的绑定记忆入队，不应依赖异常 |

---

## 4. 撤权与发送守卫：检查点现状

| 检查点 | 作者现状 | 编辑者缺口 | 可复用 |
|---|---|---|---|
| 每轮受理 | `owned_conversation(lock)` + 中央受理 | 锁内 `ai.use` + grant、history 未封存 | `create_task` 的 ready 分支（`task_service.py:554-559`） |
| 领取 / build_job | `require_write_execution`（`agent.py:322`）+ 会话锁 | 已能复核 grant；需改绑定/配置/provider（§1.2） | 引擎领取复核（E4） |
| 每次模型流式发送 | `guard_provider` + `guard_agent_provider` | 需 `for_task` guard；摘要 guard 改造 | `collaboration_ai_guard.SendGuard` |
| 每次读工具 | 无锁复核；结果写入前 `_lock_execution` | 读本身未按 grant 复核（只读不出站）；可接受或在 `run` 前加轻量 `require_project(ai.use)`（见 Q9） | `require_write_execution` |
| 每次写工具预览/执行（后台） | `_lock_execution` 在 `_process_calls` 开头和写提案前（`runner.py:584`、`666`）；`tool.preview` 内 `_lock_write` 作者锁 | `_lock_write` 换成 `ai.use` + grant + `content.edit`（**不能只 `content.edit`**） | `project_access.require_project(..., grant_identity=)` |
| 自动执行 | `_lock_execution` + `execute_change`（`runner.py:727-746`） | `execute_change` 用 `lock_author_visibility_write`（`service.py:718`），`ToolCtx(visibility_locked=True)`（`service.py:743`）使工具跳过复核 | 同上 |
| 提案确认（HTTP） | `owned_change(lock)` → `execute_change` | 同上；且无执行体锁，授权失效被吞为 `failed`（`service.py:798-799`） | `AgentAuthorizationChangedError` 需真正抛出 |
| 派生任务受理 | `execute_admitted` → 中央受理 | 中央受理已按 grant；需验证签名来源与 grant 同时绑定 | E3-A 签名来源 |
| 撤销 | `_lock_owned_project` | 锁内 `ai.use` + grant + 版本预检 | — |
| MCP 发送 | `_send_lease`（`mcp_tools.py:163-270`）锁内全量复核 | 项目条件改为 owner 或 accepted editor 原 grant | 设置门禁 `settings_gate.acquire_send` |
| 最终保存（assistant 消息/断点/日志） | `_lock_execution` / `verify_job_resources` | 已复核 grant；配置归属需改 | — |

结论：E4/E5 的租约与发送守卫（`for_task`、`require_write_execution`、`task_visible_to_actor`）可以直接复用；缺的是 **Agent 自有的作者锁替换** 和 **授权失效的错误分类**。

---

## 5. E5a 事务边界

### 5.1 已实现（2026-11-10 基础切片后的当前代码）

- `TransactionSession(audit_mode)`：`commit` 只 flush、`rollback` 空（`base.py:46-69`），并挂 `AgentAuditScope`。
- `AuditOperation.commit`：在 Agent 作用域内只把成功 **stage**，不置 `committed`（`audit_writer.py:271-279`）；`__aexit__` 在作用域内不做 rollback/查询/插入，只 `coordinator.fail(snapshot)`，`UnauthorizedError` 则 `discard`（`audit_writer.py:303-312`）。
- `AgentAuditCoordinator`（`transactions.py:149-287`）：`flush_failures` 拒绝在保存点内调用（`:181-182`）；staged 但根事务无成功行 → 转失败（`:185-193`）；`commit` 失败或取消 → `reconcile_after_failure`，先 rollback/invalidate，再以独立会话按 `operation_id` 查事实后补失败（`:216-287`），shield 防重复取消。
- 根事务入口已接：`decide_change`（`service.py:1177-1181`）、runner `_process_calls`（`runner.py:577-582`）。
- PG（`collab_agents`）11 项、SQLite 41 项通过（`phase-e-agent-transactions-impl.md:228-246`，**为当时代码**）。

### 5.2 管理审计何时启用

`AuditOperation.__aenter__`：仅当 actor 是管理账号，且作品 `user_id != actor`（除非 `include_own_project`）时启用（`audit_writer.py:93-121`）。**它不区分写入来源**——管理员兼编辑者以编辑者身份写他人作品，`project.update`/`chapter.update`/`planning.update` 等都会记管理审计（调用点见 `project_service.py:558`、`chapter_service.py:249,291,342,505`、`planning_service.py:45`、`setting_field_service.py:96` 等）。人工路由对管理员兼编辑者也是如此，Agent 开放后会自然进入审计作用域。

### 5.3 场景核对

| 场景 | 现状 | 风险 / 缺口 |
|---|---|---|
| 管理员兼编辑者，工具成功 | 成功 stage → 根 `coordinator.commit` 前 `flush_failures` 核对 → 提交 | 已覆盖（`test_agent_audit_integration.py`，但入口靠 monkeypatch `lock_author_visibility_write`/`owned_change`/`owned_conversation`/`project_agent_payload`，`:56-123`） |
| 管理员兼编辑者，预览 | 预览成功不记；预览失败按 `fail` 处理（`preview_failure` 用例） | “预览中的业务拒绝是否算管理动作失败”属既有边界（`phase-e-agent-transactions.md:213`），保持 |
| 内层 SAVEPOINT 失败、外层继续 | 失败快照在根事务提交时写入 | 已覆盖 |
| 外层 SAVEPOINT 回滚（`execute_change` 的 `begin_nested`，`service.py:770`） | staged 成功无对应行 → 转失败 | 已覆盖 |
| 授权失效（撤权/降级/旧轮次） | `AgentAuthorizationChangedError` **无 raise 点**（全仓仅定义于 `transactions.py:36`）；工具内 `require_project` 抛 403/404 → `Tool.execute` 转 error dict（`base.py:241-243`）或 `execute_change` 记 `failed`（`service.py:798-799`），**轮次继续** | 需在锁后复核失败处抛 `AgentAuthorizationChangedError`（`VisibilityPolicyError` 子类，`base.py:239-240`、`service.py:794-796`、`runner.py:742-743` 均会上抛） |
| 降级为 viewer 但保留 `projects.edit` | `content.edit` 经管理员来源仍为真 | Agent 写会**以管理员身份成功**并记管理审计——必须以 `ai.use` + grant 为前置 |
| 权限失效且审计已进入作用域 | `fail` → 根事务回滚后 `_record_failure` 独立写 | 机制在；需用真实 editor 入口 + PG 双连接验证 |
| 异常取消（`CancelledError`） | `audit_scope` 的 `except BaseException` → `reconcile_after_failure`（`transactions.py:289-298`），shield | 机制在；HTTP 确认被客户端断开、后台任务取消两条路径需真实 editor 补测 |
| 提交确认丢失 | `_reconcile` 先查 `operation_id`，不补造相反结果（`:123-142`） | 已有 SQLite 注入测试；PG 未在最新代码重跑 |
| 撤销 `_undo_changes` | 单根事务，**未接 `audit_scope`**（`service.py:1335-1386`）；`effects.undo` 直接写快照行，不经 service，不触发 `AuditOperation` | 管理员兼编辑者撤销不产生管理审计——与“经 service 写会记审计”不一致（见 Q6） |
| `execute_admitted`（派生任务） | 在根事务（非保存点）调用中央受理；中央受理在自身保存点内完成 | 任务受理不属于管理审计采集清单，无新增风险 |

### 5.4 修复方向（不在本研究中实施）

1. 新增 Agent 专用能力检查函数（例如“锁后要求作者，或 accepted editor 且 `(invitation_id, revision)` 等于任务/提案绑定的 grant”），替换 `lock_project`、`execute_change` 的 `lock_author_visibility_write(manage=False)`、`_lock_owned_project`；失败时抛 `AgentAuthorizationChangedError`，而不是 403/404 普通错误。
2. `ToolCtx` 携带 grant；`visibility_locked=True` 只允许由上述函数在同一事务锁后设置。
3. `_undo_changes` 接 `audit_scope`（是否记管理审计见 Q6）。
4. 用真实 editor 入口重跑 `test_agent_audit_integration.py` 场景（去掉 monkeypatch），PG 覆盖：撤权先提交 / 工具写先提交、令牌撤销、40001、确认请求取消。

---

## 6. 隐私

| 项 | 现状 | 缺口 |
|---|---|---|
| 会话归属 | `AgentConversation.user_id` = 创建者（`service.py:479-485`），所有查询带 `user_id == actor`（`service.py:311-315`、`456-458`、`688`） | 无；作者看不到编辑者会话、反之亦然 |
| 提及（mentions） | `mentions/sources.py:48-52` 按 `AgentConversation.user_id == user_id` | 已满足 design §2.2；需确认提及路由传真实 actor（未逐行核对路由） |
| 生成日志 | `actor_user_id` + `require_personal_project`（`service.py:562-599`） | 已满足 |
| effects / 撤销快照 | 持久 `effects` 完整；对外经 `public_changes` → `project_agent_payload`（`service.py:335-343`）按 actor 脱敏（E2） | `get_conversation` 未传 `owner_only=False`（§1.1） |
| 引擎帧 | `proposal`/`executed` 帧出队时按原 change_key 重读并 `public_changes`（`engine.py:3043-3066`） | 编辑者订阅需受控管线（§7） |
| 作者私人风格 | `project_for_model` 只给本人/预设（`read.py:142-161`）；历史回执重放过滤（`context.py:573-660`）；`update_project` 非作者禁改风格（`project_service.py:564-565`） | 已有 35 项测试（`test_agent_project_style_privacy.py`）；编辑者自身摘要被 `context.py:782-784` 拦截需调整 |
| 系统提示词 | `prompt_service.get_override(conversation.user_id, "agent.system")`、`project_info(actor_user_id=conversation.user_id)`、`skills_catalog(conversation.user_id)`（`context.py:920-931`） | 已按 actor |
| 模板/Skill/MCP/AIConfig | 引用校验只允许本人（`references.py:93-97`） | 已按 actor |
| 作者配置 ID | `_save_log` 写 `ai_config_id=job.config.id`（`runner.py:806`），日志对外投影按 actor 过滤配置 ID（E2） | `start_task` 预览中的 `ai_config_id`（§2.4）与模型上下文路径需补测 |
| 撤权后的旧会话 | 会话不随撤权删除（`AgentConversation` 外键只级联用户/作品，`models/agent.py:48-49`） | 撤权后读取/重邀后是否恢复可见见 Q7 |
| 作品删除阻断 | `has_unsettled_external_change(project_id=…)` 覆盖作品下所有会话（`project_service.py:90-158`） | 编辑者的“外部结果未知”会**阻止作者删除/彻删作品**（Q2 相关） |

---

## 7. SSE 与任务面

### 7.1 订阅

- 现状：协作者订阅 `agent.turn` 在入口 404（`api/v1/tasks.py:71-75`；`test_task_sse_scope.py:163-203` 断言）。
- 纳入 `stream_kind_open` 需要：`AgentHandler.stream_guard(job, execution, current_execution, event, data) -> SendGuard`（参照 `handlers/outline.py:202-209`），即 build_job 在领取结果上建 `for_task` guard 并保存；`verify_finalization` 用 `guard.require_task_write`。
- 需证明的帧：`reset`、`delta`（模型文本，属于本轮已持久化或正在发送）、`stage`、`tool_start`/`tool_end`（含工具摘要，可能含作品内容）、`proposal`/`executed`（已有按原 change_key 重读的投影，`engine.py:3043-3066`）、`paused`、`done`、`error`。领取前的 `QueuedSource` / `ClaimSource`（E5-e）对 Agent 返回 `execution`（`implement.md:982`），需确认受控管线对 Agent 的领取帧证明。
- 替代：参照 E6 裁决 5，**协作者不订阅、前端轮询会话详情**（AgentDock 现依赖 `useTaskStream`，`useAgentConversation.ts:24-25`），改动面小但实时性差（见 Q10）。

### 7.2 待审核（paused）与撤权

- Agent 的 `paused` 表示“等待确认提案”（spec `agent.md:340-342`），续跑由 service 在全部确认/拒绝后自动 `resume_task`；任务面板不显示「继续」。
- 撤权/降级时：E5-a 的待审核受限收尾不含 `agent.turn`（`engine.py:146-147`），**编辑者的 paused 轮次会一直 paused**，其 `proposed` 提案保留；`resource_key=agent:{cid}` 只挡 queued/running，`active_turn` 挡 open 状态（`service.py:323-332`），所以只影响该编辑者自己的会话，不阻塞作者。
- 取消路径：`cancellation.py:433-436` 已允许 paused 的 `agent.turn` 走资源回滚分支。
- 受限收尾需要定义：任务 CAS 为 failed、本轮 `proposed` 提案改为 `rejected`/`failed`（不执行）、`executing` 外部提案保留未知屏障、不写新消息之外的内容。

---

## 8. 前端改动点

| 位置 | 现状 | 需要 |
|---|---|---|
| `utils/projectAccess.ts:49-50` | `canUseOwnerAI` | 新增 `canUseAgent`（= `can_use_ai`，或后端新增 `can_use_agent` 能力位）；保留 `canUseOwnerAI` 给灵感等仍仅作者入口 |
| `pages/project/ProjectLayout.tsx:49-60` | 按钮与 `AgentDock` 用 `canUseOwnerAI` | 改 `canUseAgent`；viewer 是否显示只读会话见 Q4 |
| `?agent=open`（`ProjectLayout.tsx:31-33`、`api/tasks.ts:207-208`） | 仅作者有效；E5-g 已为全局面板非当前作品 Agent 任务补说明（`implement.md:1086`） | 随 AgentDock 门控变化；viewer 打开链接的处理 |
| `components/agent/useAgentConversation.ts:36,114-168` | 作用域复核 `'canUseOwnerAI'` | 换成新能力；撤权中途应清空详情 |
| `AgentChangeCard.tsx:65`、`AgentTimeline.tsx:97` | 撤销回调 `'canUseOwnerAI'` | 同上 |
| `AgentConversationView.tsx:86` | `AIConfigSelect`（已支持 `useAIModelSource` 的 own/owner/none 提示，`hooks/useAIModelSource.ts:13-21`） | 编辑者显示“使用作者模型，费用由作者承担”；无模型且未授权时禁用发送 |
| MCP 插件选择 / 自动执行开关 | 本人插件 | 按 Q2/Q3 决定是否对编辑者隐藏 |
| 订阅 | `useTaskStream` | 若 Q10 选轮询，需为协作者改为定时 `agentApi.get` |
| 测试 | `collaborationWorkspace.test.ts:89,101,114,131`、`useProjectAccess.test.ts:56-59`、`projectAccess.test.ts:37-57`、`stores/project.test.ts:54`、`agentUI.test.ts` | 编辑者“无项目智能体”断言需翻转；viewer、纯管理员、管理员兼 viewer 保持隐藏 |

---

## 9. 测试

### 9.1 开放后需翻转或调整的断言

| 测试 | 当前断言 | 开放后 |
|---|---|---|
| `tests/test_task_sse_scope.py:163-203` | 编辑者 `agent.turn` 已结束任务订阅 404 | 若开放订阅：改为受控回放；否则保持 |
| `tests/test_agent_audit_integration.py:56-123` | 以 monkeypatch 模拟编辑者入口 | 去掉 monkeypatch，改用真实入口（断言不放宽） |
| `tests/test_agent_context_execution_identity.py:149-158`（`failure="grant"`） | 摘要拒绝带 grant 的任务 | 改为“grant 与成员轮次一致才通过；不一致失败关闭” |
| `tests/test_task_retry_admission_api.py:95` | 协作者重试只在 handler ready 时用原来源 | 需确认参数里是否含 `agent.turn`；若含则翻转 |
| `tests/test_collaboration_planning_access.py:411`、`test_collaboration_story_access.py:38/154`、`test_collaboration_content_access.py:103` | 默认（非 manual）service helper 对编辑者 404 | 若 Agent 改传 manual 而不改默认 helper：**不翻转** |
| `tests/test_agent_api.py::test_cross_user_every_agent_endpoint_is_not_found` | 非成员 404 | 保持 |
| 前端 `collaborationWorkspace.test.ts` 等 | 编辑者无智能体 | 按 §8 翻转 |
| strict xfail（自动 MCP） | 作者也关闭 | 与 E4-i 无关，保持 |

### 9.2 需新增

SQLite 角色矩阵：owner、editor（授权/未授权）、viewer、pending、revoked、非成员、纯管理员（只读/可写）、管理员兼 viewer、管理员兼 editor × 每个路由；工具逐类正反例（含管理员兼 viewer 写工具必须失败）；检索零作者向量调用（未授权）/作者向量（授权）/编辑者本人向量配置不被使用；提案预览不含作者配置 ID；撤销后不误派生。

PostgreSQL 并发（`AINOVEL_COLLAB_POSTGRES_TEST_URL`，必须真实运行）：
1. 发消息受理 vs 撤权（两种提交顺序）。
2. 模型真实 HTTP 发送 lease vs 撤权/降级/`allow_owner_ai` 关闭/令牌撤销（响应头前后）。
3. 后台写工具执行 vs 撤权；管理员兼编辑者降级为 viewer 后写工具失败，不产生成功审计、也不产生重复失败审计；工具先持锁成功时恰好 1 条 success（裁决 29）。
4. HTTP 确认（含 `execute_admitted` 派生任务）vs 撤权；确认请求取消。
5. 撤销 vs 撤权；撤销与人工编辑竞争（effects 版本预检）。
6. 手动 MCP 确认发送 vs 撤权（未知结果屏障）。
7. 摘要发送 vs 撤权。
8. RR 快照 40001 在编辑者路径上映射为安全 409。
9. paused 轮次在撤权后的受限收尾（若纳入）与本人确认竞争。

---

## 10. 切片建议

### E4i-a　E5a 收口：Agent 能力判定与授权失效错误（后端，不开放入口）

- **范围**：新增 Agent 锁后能力函数（作者 / accepted editor + grant 匹配；不接受管理员来源）；`lock_project`、`execute_change`、`_lock_owned_project`、`owned_change(lock)`、`_turn(lock)` 内部改调它，但入口仍只放行作者（以开关或保持作者分支）；失败抛 `AgentAuthorizationChangedError`；`ToolCtx` 增加 grant；`_undo_changes` 接 `audit_scope`（按 Q6）。
- **前置**：无。
- **验收**：作者行为不变（`test_agent_*` 全过）；`test_agent_audit_integration.py` 可去掉 `lock_author_visibility_write` 的 monkeypatch，仅保留入口模拟；新增“管理员兼 viewer 写工具失败”“授权失效终止轮次而非 failed 继续”的单元测试；SQLite + PG 审计协调用例重跑。
- **文件**：`app/services/agent/{service.py,transactions.py,tools/base.py}`、`app/services/setting_ai_visibility.py`（或新 helper）、`tests/test_agent_audit_*.py`。

### E4i-b　受理与 handler：模型来源、grant、配置归属（后端，不开放入口）

- **范围**：`AgentHandler` 接受 `source=owner` 和 grant（validate_and_claim / retry / resume / build_job）；`cfg` 按 binding 读取；`verify_job_resources` 配置归属；build_job 对 grant 用 `for_task` guard；摘要路径（`_summary_execution` / `_check_summary_execution` / `context.py:782-784`）支持 editor；声明 `collaboration_execution_ready`。
- **前置**：a。
- **验收**：编辑者本人模型、授权回退作者模型、无效本人配置不回退、撤权后零模型请求；作者旧任务兼容；重邀后旧轮次重试/续跑拒绝。
- **文件**：`task_engine/handlers/agent.py`、`agent/{runner.py,context.py}`、`collaboration_ai_guard.py`（若需小适配）、新测试文件。

### E4i-c　工具迁移与检索（后端）

- **范围**：读/写/正文工具改调 `manual=True`/`manage=True` 协作者分支；`search_*` 改为冻结 `_shared_embedding` + `for_task`（或编辑者关键词）；`start_task` 预览不暴露作者配置 ID。
- **前置**：a、b。
- **验收**：每个工具对编辑者成功、对管理员兼 viewer 失败；默认 helper 测试不变；检索零作者向量调用（未授权）。
- **文件**：`agent/tools/{read.py,write.py,write_text.py,tasks.py}`、`handlers/agent.py`（冻结 embedding）。

### E4i-d　HTTP 入口开放：会话、消息、确认、撤销、派生任务

- **范围**：`service.py` 各入口切换到 a 的能力函数；`get_conversation` 投影 `owner_only=False`；viewer 按 Q4；MCP `_send_lease` 项目条件（按 Q2）；`execute_admitted` 对编辑者端到端。
- **前置**：a–c。
- **验收**：§9.2 SQLite 矩阵；派生任务归编辑者且费用来源正确；`has_unsettled_external_change` 行为按 Q2。
- **文件**：`agent/service.py`、`agent/mcp_tools.py`、`personal_projection.py`（仅调用参数）。

### E4i-e　引擎收尾与订阅

- **范围**：`agent.turn` 纳入排队/待审核受限收尾（定义提案处置）；按 Q10 实现 `stream_guard` 或保持 404。
- **前置**：b、d。
- **验收**：撤权后 paused 轮次失败、提案不执行、外部未知屏障保留；订阅帧证明（若开放）。
- **文件**：`task_engine/{engine.py,stream_evidence.py}`、`handlers/agent.py`、`tests/test_task_sse_scope.py`。

### E4i-f　前端

- **范围**：§8 全部；费用来源提示；撤权中途清理。
- **前置**：d（e 决定订阅还是轮询）。
- **验收**：前端 lint/test/build；浏览器以作者、授权编辑者、未授权编辑者、viewer 各走一遍。
- **文件**：`utils/projectAccess.ts`、`hooks/useProjectAccess.ts`、`stores/project.ts`、`pages/project/ProjectLayout.tsx`、`components/agent/*`。

### E4i-g　PG 与 E7 端到端

- **范围**：§9.2 PG 全部；E7 中“Agent 作者配置 ID、实际请求次数、费用来源”。
- **前置**：a–f。
- **验收**：真实 PG 通过（skip 不算），全量回归按阶段收口策略。

---

## 11. 待用户裁决的问题（附推荐）

| # | 问题 | 推荐 | 理由 |
|---|---|---|---|
| Q1 | 未开启 `allow_owner_ai` 的编辑者能否用自己的模型开 Agent？ | **能** | 与 R4 及其它已开放 AI 一致（`ai.use = owner or editor`，`project_access.py:65`）；费用由编辑者自己承担 |
| Q2 | 编辑者能否使用 MCP（手动确认）？ | **V1 不开放**（编辑者会话隐藏 MCP，`_send_lease` 保持作者限定） | 外部副作用不可撤销；编辑者的“结果未知”会阻止作者删除作品（`project_service.py:90-158`）；作品内容发往编辑者私有服务的边界未定义 |
| Q3 | 编辑者能否开启 `auto_execute`（small/text 提案免确认）？ | **允许**，与作者相同 | 自动执行仍走同一锁后复核与审计；只限 small/text（`runner.py:408-417`），可逐项撤销 |
| Q4 | viewer 能否只读查看会话（含自己作为编辑者时的历史会话）？ | **允许只读查看本人会话**（列表、详情、生成日志），不能发送、确认、撤销 | 生成日志已对 accepted 成员开放（`service.py:562-599`），会话详情同口径更一致；设计“viewer 不挂载智能体”指不可使用，前端以只读面板呈现 |
| Q5 | 编辑者通过 `start_task` 能启动哪些任务？ | **现有白名单 10 类全部允许**，不增加 `relations.*`/`wizard.*` | 10 类人工入口均已对编辑者开放且 ready；派生任务按各自 handler 的本人优先/授权回退解析模型 |
| Q6 | 管理员兼编辑者：Agent 写入是否记管理审计？撤销是否也记？ | **按现行 `AuditOperation` 规则记录（与人工路由一致）；撤销接 `audit_scope` 但不新增审计 action** | 人工路由已对管理员兼编辑者记录；撤销走 `effects.undo` 不经 service，新增 action 超出 V1 |
| Q7 | 撤权后再被重新邀请，旧会话是否恢复可见？ | **恢复可见（只读历史），但旧轮次不能续跑/重试/确认** | 会话属于本人私人数据，与“重新邀请不自动恢复旧任务”一致；旧 grant 轮次在锁内失败关闭 |
| Q8 | 编辑者撤销 Agent 修改需要 `ai.use` 还是只需 `content.edit`？ | **`ai.use` + 原 grant**（降为 viewer 后不能撤销） | 撤销属于 Agent 操作；否则管理员兼 viewer 可经管理员来源撤销 |
| Q9 | 读工具是否每次执行前锁内重验 grant？ | **不单独加锁**，依赖写入结果前的 `_lock_execution` 复核 | 读工具只读数据库不出站；结果写入前已按 grant 复核，避免每次读取多一次写锁（SQLite 全库锁） |
| Q10 | 协作者 `agent.turn` 订阅是否开放？ | **V1 保持 404，前端轮询会话详情** | 与 E6 裁决 5 一致；Agent 帧种类多（delta/tool/proposal），实现 `stream_guard` 及帧证明成本高 |
| Q11 | 撤权后编辑者的 paused Agent 轮次如何处理？ | **纳入受限收尾：任务失败、`proposed` 提案标为失败（不执行）、外部未知屏障保留** | 避免永久 paused；与 E5 裁决“paused 与运行中一样由系统收尾”一致 |
| Q12 | 检索工具对编辑者：授权时用作者向量还是一律关键词？ | **与 E6 一致：授权且作者有可用配置 → 作者向量（受理时冻结），否则关键词** | E6 已统一所有检索入口口径（design §6.2） |
| Q13 | 编辑者会话自己生成的摘要（`context.py:782-784` 现会拦截） | **允许本人受控路径生成的摘要继续使用**，旧的无来源摘要仍拦截 | 编辑者会话不存在作者私人风格回执混入的历史问题；需要摘要来源标记区分 |

---

## 12. 相关文件

| 路径 | 用途 |
|---|---|
| `backend/app/api/v1/agent.py` | 路由与 `mutation_db` |
| `backend/app/services/agent/service.py` | 会话/消息/确认/撤销入口与作者锁 |
| `backend/app/services/agent/runner.py` | 后台循环、工具处理、日志、执行体锁 |
| `backend/app/services/agent/context.py` | 消息组装、摘要、风格历史过滤 |
| `backend/app/services/agent/transactions.py` | RR 会话、审计协调器、授权失效错误 |
| `backend/app/services/agent/tools/{base,read,write,write_text,tasks,skills,references,visibility}.py` | 工具 |
| `backend/app/services/agent/mcp_tools.py` | MCP 解析与发送租约 |
| `backend/app/services/task_engine/handlers/agent.py` | `AgentHandler` |
| `backend/app/services/task_engine/{engine.py,stream_evidence.py,execution_identity.py,cancellation.py}` | 领取/收尾/订阅/执行体复核 |
| `backend/app/services/{task_service.py,project_access.py,personal_projection.py,setting_ai_visibility.py,audit_writer.py,project_ai_config.py,collaboration_ai_guard.py,kb_service.py,kb_index_service.py}` | 共享协议 |
| `backend/app/api/v1/tasks.py` | 重试/订阅入口门 |
| `frontend/src/{utils/projectAccess.ts,hooks/useProjectAccess.ts,pages/project/ProjectLayout.tsx,components/agent/*,api/tasks.ts}` | 前端 |
| `.trellis/spec/backend/agent.md`（`:195-215` start_task、`:318-370` 运行契约、`:373-388` 安全规则） | 规范 |

## 13. Caveats / Not Found

- 未运行任何测试；“已覆盖”指存在对应测试文件和用例，不代表当前工作树通过。早期 PG 结果对应当时代码。
- 未逐行核对各 service `manual=True` 分支是否带有人工专属副作用（版本来源、审计 action、版本备注），c 片实施前需逐个确认。
- 未核实 `start_task` 预览中 `ai_config_id` 在作者回退场景的实际取值，也未核实模型上下文是否会带出该值。
- 未核实提及（mentions）HTTP 路由是否传真实 actor；仅确认 `sources.py` 查询按 `user_id` 过滤。
- 未核实 `replace_chapter_text` 与 E4 单章 claim（编辑者占用）之间的互斥是否完整。
- `test_task_retry_admission_api.py:95` 的参数是否包含 `agent.turn` 未展开查看。
