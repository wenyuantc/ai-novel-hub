<!-- 自动摘录自 .trellis/spec/backend/agent.md；逐行保留原文，使用时按行号回查完整规范。 -->

<!-- .trellis/spec/backend/agent.md:167-295 -->
## 场景：提案、自动执行与确认（`runner.py` / `service.py`）

### 1. 常量

```python
MAX_TOOL_CALLS = 8; MAX_WRITE_PROPOSALS = 50; AUTO_EXECUTE_LIMIT = 3   # runner.py
RESULT_LIMIT = 8000                                                     # context.py，单条工具结果
```

### 2. 契约

- **工具调用上限 8**（按调用个数累计，含读工具，跨续跑累计在 `checkpoint.tool_calls_used`）：一次响应里超出剩余额度的调用不执行，
  工具结果 `{"error": "本轮最多调用 8 次工具，请直接回答或分批进行"}`。用满后下一次请求 `tool_choice="none"`；模型仍返回调用时为每个调用补
  `{"error": "已达到工具调用上限，请直接回答"}`，再追加 assistant 消息「本轮已达到工具调用上限，请分批继续。」并结束（不会无限循环）。
- **单次调用的处理**：已有对应 tool 消息的跳过（续跑 / 重试幂等）；已有提案的直接并入本批；参数不是 JSON 对象 →「工具参数必须是合法的 JSON 对象」；
  未知工具 →「工具不存在」（注册表与本轮 MCP 映射表都没有）；在映射表中但 `resolve` 失败 →「工具不存在或已变更」；MCP 工具先按 `inputSchema` 校验参数，
  免确认工具直接调用并写 tool 消息，其余生成 `kind="external"` 提案（同样受 50 项上限约束）；读工具立即 `run` 并写 tool 消息；写工具先检查本轮提案数，≥ 50 → `{"error": "单轮最多 50 项写操作，请分批进行"}`
  （不生成提案，作为与 8 次上限无关的防御）；否则 `preview`，出错直接作为工具结果，成功则建 `agent_changes(status=proposed, seq=本轮序号)`。
- **自动执行**：同时满足才执行——会话 `auto_execute`；本批有待确认提案；`checkpoint.auto_execute_used` 未置；**本轮全部提案就是这一批**（首个写批次）；
  ≤ 3 项；每项 `kind ∈ {small, text}` 且不是 `update_chapter_plans`、不是带 `stages` 的 `update_career`（`external` 一律返回 False，批次里有一项 external 整批都要确认）。先置 `auto_execute_used` 并保存断点，再逐项
  `execute_change`（照常比较预览）；遇到 `preview_changed` 停止，剩余项等待确认。同轮后续写批次一律需要确认，也不回滚先前已自动执行的修改。
- **暂停**：本批仍有 `proposed / executing` → 保存 `pending_change_ids`，产出 `stage`「等待确认 N 项修改」与 `paused {reason: "confirm", change_ids}`；
  已处理（执行 / 拒绝 / 失败）的提案补写 tool 消息（`change.result` 或 `{"error": change.error}`）。
- **确认 / 拒绝**（`decide_change`）：先 rollback 释放旧快照，再取进程内会话锁（`conversation_lock`，SQLite 没有行锁）并对会话行 `FOR UPDATE`；
  `executing` → 409「修改正在处理中」；`proposed` 但轮次不是 `paused` → 409「请等待本轮暂停确认，或先重试中断的轮次」；拒绝写
  `result = {"message": "用户拒绝了该修改"}`（续跑时作为工具结果交给模型）。
- **确认时的预览比较**（`execute_change`）：重新 `preview`；出错 → `failed`；与存储的 `preview` **不相等** → 用新预览覆盖、commit，抛
  409 `{detail: "数据已经变化，请检查最新预览后重新确认", code: "preview_changed", change: ChangeOut}`，**不执行**，用户须对新预览再次确认。
  相等 → 条件 UPDATE `proposed → executing`（影响 0 行 → 409，防重复点击）→ SAVEPOINT 内 `snapshot → execute → flush → snapshot → diff`
  （正文替换另加 `chapter_text` 效果）→ `executed` + `effects` + `executed_at`；工具返回 error / `AppError` / 意外异常 → `failed`（意外异常记日志，
  文案「修改执行失败：服务器内部错误」），SAVEPOINT 回滚不留部分修改。
- **external 提案的确认**（`_execute_external`）：不走 snapshot / SAVEPOINT；重新 `resolve` 生成预览（`guards` 为插件 id、当前 `schema_hash`、是否可用）比较，
  不同 → 409 `preview_changed`；相同 → 条件 UPDATE `proposed → executing`（`executed_at` 记开始时间）并**先 commit**，再在确认请求内同步调用一次 MCP（单独连接，
  30 秒）；成功 `executed`（`result = {"text"}`、`effects = []`），失败 `failed`。停在 `executing` 的中断调用由 `recover_external` 判为失败，**从不重调**。
  `decide_change` 取锁后、`maybe_resume`、runner 处理调用前都会先回收该轮超过 2 分钟的项，进程启动时全部回收。细节见 skills-mcp.md。
- **续跑**：本轮已无 `proposed / executing` 且任务 `paused` → `task_service.resume_task`（`maybe_resume`）。`confirm-all` / `reject-all` 按
  `seq` 依次调用 `decide_change(resume=False)`，遇到第一个 `AppError` 或 `failed` 停止并在 `error` 中返回，最后统一 `maybe_resume`。
- **并发视图**：智能体的写接口与 runner 都用 `transactions.session`（PostgreSQL 为 `REPEATABLE READ`，快照不会把并发请求的修改算成自身效果）；
  序列化失败（`40001`）→ 回滚 + 409「数据同时被其他操作修改，请刷新预览后重试」，runner 中为 `error` 事件。

### 3. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 项目 / 会话 / 提案 / 轮次不属于当前用户 | 404（`会话不存在` / `修改提案不存在` / `智能体轮次不存在`） |
| 同会话已有排队 / 运行 / 待确认轮次时发送消息、删除会话 | 409 `当前会话已有进行中或待确认的轮次` / `会话有进行中或待确认的轮次，无法删除` |
| 确认时预览已变化 | 409 `code = preview_changed`，附最新 `change` |
| 轮次未暂停时确认 / 拒绝 | 409 |
| 待确认时 `POST /tasks/{id}/resume` | 409 `请先确认或拒绝本轮全部修改`（`revalidate_resume`） |
| `POST /tasks/{id}/finish` | 409 `请确认或拒绝全部修改，或取消本轮智能体任务`（`finish_result` 直接拒绝） |
| 模型不支持工具调用 | 任务 failed，`error = TOOLS_UNSUPPORTED` |
| 确认 external 时插件 / 工具已停用、移出会话或 schema 已变化 | 409 `code = preview_changed` |
| 撤销 external 提案 | 400 `外部操作无法撤销`；整轮撤销跳过并在 `notice` 注明 |

### 4. 必需的测试

`tests/test_agent_runtime.py`（读后作答 + usage、确认续跑 / 拒绝 / 重复确认、自动执行阈值 3、第二批需确认、首批需确认时之后也不自动、含删除的批次、
整体同步不自动、8 次上限且每个调用都有结果、50 项上限独立于工具额度、续跑失败重试不重复写、取消后重试保留已提交结果、取消后新一轮补全协议、
排队中取消的重试保留原提案、日志保留与配置删除、重启中断后重试、不支持工具的提示）；`tests/test_agent_mcp_skills.py`（external 提案确认续跑、免确认、
连接复用与关闭、映射表外的名称、确认时 409、不自动执行、超时作为工具错误、中断回收、强制启用 Skill，清单见 skills-mcp.md）；`tests/test_agent_api.py`（会话 CRUD 与偏好继承、
忙碌会话的 send / delete / resume / finish 409、预览刷新后再撤销冲突、并发确认只执行一次、**每个端点**跨用户 404、序列化失败回滚且保留提案）。

---

## 场景：效果快照与撤销（`effects.py`）

### 1. 签名

```python
EFFECT_TABLES: frozenset[str]     # 见下
async def snapshot(db, project_id) -> Snapshot            # {table: {pk_key: row_dict}}，列查询、JSON 可序列化、只查当前项目
def diff(before, after) -> list[Effect]                    # [{table, pk, before | None, after | None}]
def chapter_text_effect(chapter_id, before_version_id, after_version_id, before_hash, after_hash) -> Effect   # table = "chapter_text"
async def undo(db, project_id, effects) -> bool            # 不 commit；返回是否需要在提交后 enqueue_memory
```

### 2. 契约

- **差异范围**：`EFFECT_TABLES` = `projects`、`characters`、`organizations`、`organization_members`、`relationship_types`（只取本项目自定义；全局预置只读）、
  `relationships`、`relationship_history`、`careers`、`career_stages`、`character_careers`、`career_history`、`world_settings`、`outlines`、
  `chapter_plans`、`chapters`、`foreshadows`、`foreshadow_characters`、`foreshadow_history`、`story_memories`、`chapter_characters`、
  `chapter_analyses`、`analysis_changes`、`setting_timelines`、`setting_field_definitions`、`setting_field_options`、`setting_reference_handles`、`setting_ai_links`、`setting_categories`、`setting_category_rules`、`setting_text_fields`、`setting_progressions`、`chapter_versions`、`generation_logs`、`tasks`。规则取自备份注册表 `PROJECT_RULE` + `RULES`
  （**顺序即插入顺序，逆序即删除顺序**），另加 `tasks`。写工具新改动一张表时必须加进来。
- **列**：忽略 `updated_at` / `word_count`；`created_at` 必须保留（创建后不变，不影响冲突比较；撤销删除时靠它恢复原创建时间，见 `test_undo_delete_keeps_original_created_at`），但 `preview_diff` 必须跳过它，否则新建类提案的预览随秒变化，确认时误判 409，见 `test_preview_diff_ignores_created_at_of_inserted_rows`）；`chapters.content` 不进快照，只存 `_content_hash`，空白正文另存 `_empty_content`
  （唯一会被工具删除的章节是细纲同步删掉的空章节）；`chapter_versions` 只跟踪这些被删空章节的版本（正文修改产生的新版本属于历史，不撤销）；
  `generation_logs` / `tasks` 只取 `id / project_id / chapter_id`，只记录修改（章节删除导致的 SET NULL），不记录增删。
- **效果版本**：新diff／chapter_text为`format_version:6`，登记scenes及新规划列／本地角色，章结构签名包含Scene和本地规划，内容版本不回退。可见性兼容阈值仍是5，不能因当前版本升6把合法v5误判为缺可见语义。合法旧v1–v5按各自时间线／typed／分类／可见保护预检；缺Scene语义的旧章文本仅安全单Scene桥接，不安全多Scene返回`legacy_effect_requires_manual_correction`。`preflight_effect_versions`仍按逆序before构造拟恢复图，不能覆盖后来作者限制。旧v2／v3 UPDATE缺标签时保留后来标签，旧CREATE遇后来非空标签拒删，合法旧DELETE补空。
- **Scene恢复**：正文工具先把章绝对范围解析为唯一Scene，再调用`replace_scene_content`；预览签名绑定真实Scene和结构。新chapter_text前后结构snapshot必须可证明一致；恢复只改目标正文并推进新版本。Core撤销遵循注册表拓扑并核对章结构签名，恢复后统一刷新章投影／故事位置；真删除空章恢复也必须恢复初始Scene。不能直接赋`Chapter.content`绕过投影。详见[独立场景](./scenes.md)。
- **结构历史引用**：Core undo删除实体前按语义扫描保留`ChapterVersion.scene_snapshot.raw`的POV／typed引用，不把任意JSON整数当ID。只剩历史引用也须拒绝删目标；本批effect将同一历史一起删除／撤回引用时按拟恢复图允许，不能blanket deny。失败保留历史和目标、完整备份仍可再导入。
- **目录代次**：`fields_revision/categories_revision/ai_visibility_revision/ai_history_epoch` 从备份和 effects 项目列快照／比较／恢复集合排除；受影响目录从当前高水位递增，失败不增。还原后同事务重验字段、分类、链接及每行项目 scope，再 `materialize_project`。预览对新 ID 及 JSON 里的 option／handle 语义归一。详见[字段恢复合同](./setting-fields.md)与[分类恢复合同](./setting-categories.md)。
- **AI工具结果白名单**：整理标签进入数据库及effects完整快照，但不自动进入模型消息。`tools/base.serialized`仅ORM分支过滤`setting_tags`；显式DTO／dict合同不随意改写。必须使用真实FakeLLM工具返回哨兵验证，不能只查read DTO。
- **字段工具**：`list_setting_field_definitions`、`list_setting_entries`、`get_setting_entry_fields`为三个有界读工具；`update_setting_field_baseline`与`record_setting_field_progression`原子处理空槽首写。原`record_setting_progression`扩四类值，仍校验作品／字段／章节／引用；provider对象有显式properties和严格嵌套ID检查。`list_setting_timelines.items[].custom_fields`含id／label／field_type／definition_id与必要当前元数据，并返回fields_revision。旧尚未执行且缺新语义的提案须重新生成。
- **快照用列查询**（不是 ORM 实例），避免数据库级联或 Core UPDATE 之后读到 identity map 里的旧值。
- **冲突检测**（任一条不满足 → 409 `{detail: "数据已变化，无法撤销，请检查冲突", code: "undo_conflict", conflicts: [{table, label}]}`，不做任何修改）：
  - 原来是插入 / 修改：当前行（`FOR UPDATE`、项目范围内）必须等于 `after`；
  - 原来是删除：该主键在**全表**都不存在（id 可能已被其他项目复用）；
  - 不支持撤销项目的创建 / 删除；涉及的章节正在 `generating` → 冲突；行的 `project_id` 不是当前项目 → 冲突；
  - **依赖检查**（前面都通过后）：撤销插入时，若有别的表经外键引用这行、且该引用不是本次一起插入或本次改出来的 → 冲突「新增了关联记录」
    （防止删除新建实体时悄悄级联删掉 / 解除之后新增的关联）；新建角色被 `chapter_plans.character_ids`（JSON，没有外键）后来引用 → 冲突。
  - `chapter_text`：章节存在、非 `generating`、当前正文 hash == `after.hash`，且前后两个版本都存在、内容 hash 与记录一致。
- **还原顺序**（在 `begin_nested()` 内）：
  1. 先把**既有行**指向本次新建父记录的外键改回 `before` 值——处理 NO ACTION 外键（`relationships.type_id`、`character_careers.stage_id`）
     与级联外键（如新建大纲后章节的 `outline_id`），否则删父记录会失败或把既有行一起级联删掉；
  2. 按注册表**逆序**删除插入出来的行（子表先于父表）；
  3. 按注册表**顺序**用原 id 重新插入被删的行；`TableRule.deferred` 的列（自引用 / 互相引用，如 `organizations.parent_id`、`chapters.plan_id`）先插 null；
  4. 把修改过的行整行改回 `before`（含章节号平移涉及的所有登记列）；
  5. 回填 deferred 列。
  之后只刷新本轮identity map中的受影响实例与Project。单项`effects.undo(..., discard_inserts=True)`维持原默认；整轮service传False，外层commit成功后才`discard_undone_inserts`，失败调用`refresh_after_rollback`恢复同session值和原对象attached身份。Core写后refresh的对象可能不dirty，SAVEPOINT回滚不会自动修正其缓存；不能靠预检掩盖恢复后失败，也不能提前expunge破坏外层失败恢复。**不能 `expire_all`**，调用方还持有会话／提案对象；preview同样局部恢复并保留无关pending编辑。
- **正文撤销**：从 `before.version_id` 的版本恢复正文与字数，记 `source="restore"` 版本（note「撤销智能体修改，恢复自 vN」）。
- **派生数据**：`story_memories` 有变化时，删除插入行的检索单元（`retrieval_service.delete_refs`），对恢复的行 `memory_service.index_memories`
  重建关键词部分并 `invalidate`；返回 True，由 service 在 commit 后 `kb_index_service.enqueue_memory` 排队向量任务。
- **原子性**：`undo` 开头对项目行做无值变化 UPDATE（取项目写锁，并防止 SQLite legacy 模式下 RELEASE SAVEPOINT 提交外层事务）；还原中
  `IntegrityError` → 409 `undo_conflict`（「关联记录或唯一字段已变化」）。整轮撤销（`POST /turns/{id}/undo`）按 `seq` 倒序逐项 `undo`，
  全部在一个外层事务里，任一项冲突整体 rollback。Core恢复后即使ORM对象不dirty，普通失败、物化／commit失败和`CancelledError`也都必须进入受影响identity的回滚刷新；取消属于`BaseException`，不能仅catch Exception。刷新后重抛原异常，不改变任务取消／超时协议。
- **service 层规则**：只有 `executed` 可撤销（`undone` 重复撤销直接返回）；含 `kind = task` → 409「已启动的任务不能撤销，请在任务面板取消」
  （整轮撤销自动排除 task）；含 `kind = external` → 400「外部操作无法撤销」（整轮撤销排除 external，`TurnChangesOut.notice` =「已跳过 N 项外部操作（无法撤销）」）；
  有运行中（非 `paused`）轮次时 → 409「请等待智能体暂停或结束后撤销」。撤销成功后标记 `undone` 并追加一条
  没有 `tool_call_id` 的 tool 消息「用户撤销了：<标题>」，下一轮作为不可信的 user 备注交给模型。

### 3. 必需的测试

`tests/test_agent_effects.py`（删除角色恢复关系 / 成员 / 职业且 id 不变、快照不受 identity map 与他项目影响、自引用与 RESTRICT 外键的父子恢复、
新建父记录后新增依赖 → 冲突、细纲 JSON 引用 → 冲突、原 id 被他项目占用 → 冲突、撤销新建大纲先解除既有章节、修改冲突整体原子、整轮回滚、
章节号平移的每个登记列与检索、删空章节恢复版本 / 分析 / 记忆 / 关联、删大纲恢复 deferred 关联不动正文、撤销插入章节不删之后写入的正文、
唯一冲突回滚部分重插、正文按版本恢复且之后有修改时冲突、新私有关系类型撤销不动预置、新关系 + 新类型先删子后删父、改指向后先恢复引用再删类型、
屏蔽记忆恢复后仍不召回、记忆派生行刷新）。

---
