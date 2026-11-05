# 任务上下文摘录：agent-tools-and-effects

来源：`.trellis/spec/backend/agent.md`，原文第78–286行。
源文件SHA256：`ebe014a890f86d1790ab4fe8575854953b47688d49c8740734eeca653d6cbc0b`。
这是2026-11-03为避免自动注入截断而保留的连续原文，不修改或降低原规范；涉及摘录外主题时按原索引读取相应章节。若原规范更新，以原文件为准。

---

## 场景：工具层（`app/services/agent/tools/`）——新增工具的检查清单

### 1. 签名

```python
ToolKind = Literal["read", "small", "delete", "task", "text", "external"]   # external 只用于 MCP 提案，注册表里没有这种工具
@dataclass class ToolCtx: db; project; user_id; task_context   # project_id 从 identity 读取（SAVEPOINT 回滚后不触发隐式 IO）
@dataclass class Tool:
    name; description; schema: type[BaseModel]; kind: ToolKind; operation; preview_operation=None; id_model=None
    parameters -> schema.model_json_schema()
    async def run(ctx, args) -> dict       # 仅读工具
    async def preview(ctx, args) -> dict   # 写工具：校验 + 对比，不落库
    async def execute(ctx, args) -> dict   # 写工具：在 SAVEPOINT 内执行
get_tool(name) / list_tools()   # tools/__init__.py 的静态白名单：READ + SKILL + WRITE + TEXT + TASK（共 51 个）
async def validate_references(ctx, args, *, id_model=None)   # tools/references.py
```

### 2. 契约

- **注册表是白名单**：只从 `read.py` / `skills.py` / `write.py` / `write_text.py` / `tasks.py` 的 `TOOLS` 列表构建，不按模型给的名字动态导入。
  `test_registry_has_only_explicit_tools` 固定清单，新增工具要同步改测试。
- **MCP 工具是动态工具，不进注册表**：每轮由 `mcp_tools.build_tool_map` 生成 `mcp__{插件}__{工具}` 名称并写入 `checkpoint.mcp_tool_map`，runner 在
  `get_tool` 查不到时只按这张映射表解析（模型编造的名称 →「工具不存在」）；每次模型请求的工具列表 = `list_tools()` + `mcp_tools.tool_specs(...)`。
  命名、30 项上限与解析复查见 skills-mcp.md。
- **kind 分级**：`read`（直接执行）；`small`（新增 / 修改）；`delete`（`delete_*`、`remove_org_member`、`remove_career_assignment`，预览带
  warning「删除会同时影响下列关联数据」）；`text`（仅 `replace_chapter_text`）；`task`（仅 `start_task`）；`external`（MCP 工具调用的提案，不可撤销、不自动执行）。kind 决定是否可自动执行，见下一场景。
- **参数**：Pydantic `extra="forbid"`（`EmptyArgs` / `arguments(name, schema, with_id=)` 生成），校验失败、`AppError` 一律转成工具结果
  `{"error": ...}`（`NotFoundError` 统一为 `{"error": "不存在"}`，不区分不存在与无权限；`ValidationError` 为 `{"error": "工具参数校验失败", "errors": [{field, message}]}`），
  **不抛异常、不中断这一轮**。
- **引用校验先于 schema**（`_validate` 先 `validate_references` 再 `model_validate`，嵌套在 `params` 里会被丢弃的越权 id 也要拦）：
  - `project_id` 必须等于当前项目，否则「不存在」；出现 `user_id` → 400「工具不能指定用户」；
  - 键 `id` 按 `id_model` 查；`references._MODELS` 中登记的 `*_id` 按对应模型查**属于当前项目**（`WritingStyle` 预设或本人、`AIConfig` 本人、
    `RelationshipType` 预置或本项目、`CareerStage` / `OrganizationMember` / `CharacterCareer` 经父表 join 到项目；`skill_id` → `Skill`
    按「全局或本人」查，不按项目）；`*_ids` 列表逐项查；
    `style` 为整数时按风格查；**未登记的 `*_id` / `*_ids` 一律 400「不支持的引用参数」**；递归检查嵌套对象，`stages` / `plans` 列表里的 `id`
    分别按 `CareerStage` / `ChapterPlan` 查。
- **`id_model`**：参数含顶层 `id` 的工具必须设置（或由所调服务自行按项目校验，如 `read_chapter`）；create 工具也设置，用来在预览差异里选出
  目标表的行作为卡片标题。
- **复用业务服务**：写工具调用现有 service 的校验和写入函数（`story_service` 的 owned 增删改、`organization_service`、`relationship_service`、
  `career_service`、`foreshadow_service`、`plan_service.sync_plans`、`project_service.update_project`、`chapter_service.update_chapter`），不另写规则。
  service 里的 `commit()` 经 `TransactionSession` 降级为 `flush()`、`rollback()` 为空操作，最终事务由提案服务持有。
- **预览**（无 `preview_operation` 时的默认实现）：`lock_project`（对项目行做无值变化的 UPDATE，串行化项目写入，并保证 SQLite 在 SAVEPOINT 外已有物理事务；
  显式保留 `updated_at`）→ `begin_nested()` → `effects.snapshot` → 执行 operation → flush → 再快照 → `diff` → **回滚 SAVEPOINT** 并 refresh 项目。
  返回 `{title, target, diff: [{field: "表.主键.列", label, before, after}], warnings, guards: [{table, pk, hash(完整旧行)}]}`。
  `preview_diff` 把本次新建行的 id 及引用它的外键替换为稳定占位符 `新增{table}:{n}`（PG 序列前进不会让同一提案永远对不上），隐藏 `id` /
  `project_id` / `pair_low` / `pair_high` / `_` 前缀列，新增行的 `sort` 仅在参数显式给出时显示。`guards` 让未直接改写的字段或隐藏的正文 hash
  变化也会使预览不同。`FIELD_LABELS` 给出中文列名，新增列要补。
- **执行**：`lock_project` → `begin_nested()` 内执行 → flush；`AppError` / `ValidationError` 回滚 SAVEPOINT 并返回 `{"error"}`。不 commit。
- **读工具**：列表统一 `paged`（`page` ≥ 1、`page_size` 1–50，默认 20，返回 `items / page / pages / total`）；`read_chapter` 每页 4000 字，返回
  `page / pages`，页码越界 400；`get_outline_tree` 梗概截到 300 字、细纲只给概要字段；`search_memories` / `search_knowledge`（`top_k` ≤ 10）走
  `retrieval_service.search`（`min_score=0.1`，embedder 由 `resolve_embedder` 给出，警告附在结果里）。
  Skill 读工具（`tools/skills.py`）：`list_skills`（`paged` 同样口径）、`load_skill(slug, page)`（正文每页 4000 字 + 参考资料目录）、
  `read_skill_reference(slug, index, page)`；slug 只在本人与全局 Skill 中解析，本人优先。
- **局部替换正文**（`write_text.py`）：`original` 1–2000 字、`replacement` 0–5000 字；`original` 在当前正文中**恰好出现一次**，否则
  400「原文未找到或不唯一，请扩大或缩小引用范围」；章节 `generating` → 409。预览带 `content_hash`（完整正文变化也要求重新确认）。执行：
  `SELECT ... FOR UPDATE` → `ensure_baseline`（最新版本内容与当前正文不同时再补一个 `baseline`）→ 条件 UPDATE（`status != generating` 且
  `content == 执行前正文`，影响 0 行 → 409「正文同时被其他操作修改」）→ 记 `source="agent"` 版本（note「智能体局部替换」）；结果含
  `before_version_id / after_version_id / before_hash / after_hash`，供撤销使用。正文 hash 变化后分析按现有规则变为 stale。
- **启动任务**（`tasks.py`）：`AllowedKind` 白名单（`chapter.generate / polish / revise / batch / analyze`、`outline.expand / continue`、
  `character.generate`、`organization.generate`、`career.generate`）。`request_params` 用对应请求 schema + `extra="forbid"` 重新校验，
  **强制 `project_id = ctx.project_id`**（覆盖模型给的值），章节类（非 batch）必填 `chapter_id`、`outline.expand` 必填 `outline_id`。
  预览直接调用真实 handler 的 `validate_and_claim`（在预览 SAVEPOINT 内，回滚后不留抢占），再校验 `spec.project_id`；预览含任务参数对比、
  warning「会消耗 AI 调用；已启动的任务只能取消，不能撤销」、所涉章节指纹 `chapter_state`（id / 章号 / 标题 / 原状态 / 正文 hash）、`ai_config_id`
  与 `skill`（Skill 快照的 `{id, name, hash(正文)}`，提案后 Skill 被修改时确认会 409）。
  执行用 `task_service.create_task(db, DeferredEngine(...), ...)`：任务行留在调用方事务里，`notify` 推迟到确认请求 commit 之后由真实引擎调用；
  创建后再查一次 `task.project_id`。函数 schema 必须列出 `params` 的字段（Gemini 不接受没有 properties 的空 object）。

### 3. 新增工具检查清单

1. 选对 kind；任何会删除、批量覆盖子记录或消耗 AI 的工具不能是 `small`（或在 `runner._can_auto_execute` 中排除，见 `update_chapter_plans`、带 `stages` 的 `update_career`）。
   访问项目外部系统的工具不要做成注册表工具，走 MCP 的 `external` 路径（不可撤销、默认确认）。
2. 参数 schema `extra="forbid"`，复用现有请求 schema；新的引用参数名登记到 `references._MODELS`，嵌套 id 列表登记到嵌套映射。
3. 带 `id` 的工具设置 `id_model`；不能访问 `ctx.project_id` 以外的数据。
4. 写入只调用现有 service；需要特殊预览时提供 `preview_operation`，返回值必须稳定（同一数据多次预览相等），否则确认会一直 409。
5. 被改动的表必须在 `effects.EFFECT_TABLES`（及备份注册表 `RULES`）里，否则撤销会漏数据；新增列的中文名补到 `FIELD_LABELS`。
6. 返回值可 JSON 序列化（`serialized`），大结果分页，不依赖结果截断。
7. 测试：preview 对比与 execute 结果、跨项目 / 跨用户 id「不存在」、参数错误作为工具结果、撤销恢复。

### 4. 必需的测试

`tests/test_agent_tools.py`（白名单（含 Skill 读工具、不含 `mcp__`）、CRUD 预览与执行、关系与自定义类型、项目字段白名单、成员 / 职业挂接、细纲与章节标题、读工具与分页、
检索不含他项目与屏蔽项、替换唯一匹配与 agent 版本、**每一层嵌套**的越权 id、新建 id 不影响预览稳定性、预览失败回滚且保留外层未提交写入、
执行不提交外层事务、任务预览用真实 handler 不抢占、白名单与项目强制绑定、目标或级联数据变化后预览刷新、预览不改项目时间戳）。

---

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
  `chapter_analyses`、`analysis_changes`、`chapter_versions`、`generation_logs`、`tasks`。规则取自备份注册表 `PROJECT_RULE` + `RULES`
  （**顺序即插入顺序，逆序即删除顺序**），另加 `tasks`。写工具新改动一张表时必须加进来。
- **列**：忽略 `updated_at` / `word_count`；`created_at` 必须保留（创建后不变，不影响冲突比较；撤销删除时靠它恢复原创建时间，见 `test_undo_delete_keeps_original_created_at`），但 `preview_diff` 必须跳过它，否则新建类提案的预览随秒变化，确认时误判 409，见 `test_preview_diff_ignores_created_at_of_inserted_rows`）；`chapters.content` 不进快照，只存 `_content_hash`，空白正文另存 `_empty_content`
  （唯一会被工具删除的章节是细纲同步删掉的空章节）；`chapter_versions` 只跟踪这些被删空章节的版本（正文修改产生的新版本属于历史，不撤销）；
  `generation_logs` / `tasks` 只取 `id / project_id / chapter_id`，只记录修改（章节删除导致的 SET NULL），不记录增删。
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
  之后只刷新 identity map 中受影响的实例（插入的 expunge、其他 refresh；**不能 `expire_all`**，调用方还持有会话 / 提案对象）。
- **正文撤销**：从 `before.version_id` 的版本恢复正文与字数，记 `source="restore"` 版本（note「撤销智能体修改，恢复自 vN」）。
- **派生数据**：`story_memories` 有变化时，删除插入行的检索单元（`retrieval_service.delete_refs`），对恢复的行 `memory_service.index_memories`
  重建关键词部分并 `invalidate`；返回 True，由 service 在 commit 后 `kb_index_service.enqueue_memory` 排队向量任务。
- **原子性**：`undo` 开头对项目行做无值变化 UPDATE（取项目写锁，并防止 SQLite legacy 模式下 RELEASE SAVEPOINT 提交外层事务）；还原中
  `IntegrityError` → 409 `undo_conflict`（「关联记录或唯一字段已变化」）。整轮撤销（`POST /turns/{id}/undo`）按 `seq` 倒序逐项 `undo`，
  全部在一个外层事务里，任一项冲突整体 rollback。
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

