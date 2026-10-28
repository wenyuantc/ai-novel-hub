# 项目智能体（`agent.turn`）：工具调用 / 提案与确认 / 效果快照与撤销

> 来源任务：`10-23-project-agent`（迁移 `0018`）。任务 / 订阅契约见 [error-handling.md](./error-handling.md)「后台任务与 SSE 契约」，
> 断点与待审核见 [batch-generate.md](./batch-generate.md)，模板注册与锁定变量见 [prompts-styles.md](./prompts-styles.md)，
> 表规则注册表见 [project-transfer.md](./project-transfer.md)，章节号平移见 [outline.md](./outline.md)。

代码位置：`app/ai/tools.py`（与 provider 无关的数据结构）、`app/ai/providers/{base,openai_compat,anthropic,gemini}.py`（`chat_tools_stream`）、
`app/services/agent/`（`tools/` 工具注册表、`effects.py` 差异快照与撤销、`context.py` 消息组装、`runner.py` 循环、`service.py` 会话 / 提案、
`transactions.py` 隔离级别）、`app/services/task_engine/handlers/agent.py`、`app/api/v1/agent.py`、`app/schemas/agent.py`、`app/ai/prompts/agent.py`。

---

## 场景：Provider 工具调用协议（`chat_tools_stream`）

### 1. 范围 / 触发条件

新增 provider、修改三家的工具调用实现、改动 `AgentMessage` / `ToolCall` 结构，或在其他功能里复用工具调用时。

### 2. 签名

```python
# app/ai/tools.py
class ToolSpec(TypedDict): name: str; description: str; parameters: dict          # JSON Schema（object）
class ToolCall(TypedDict): id: str; name: str; arguments: str; provider_metadata: NotRequired[dict]   # arguments = JSON 原文
class AgentMessage(TypedDict, total=False): role: system|user|assistant|tool; content; tool_calls; tool_call_id; name
ToolChoice = Literal["auto", "none"]
AgentEvent = tuple[Literal["text", "tool_call", "usage", "finish"], dict]
def native_metadata(message, provider) -> dict      # 只取 provider_metadata.provider == 当前 provider 的块
def history_arguments(raw: str) -> dict             # 非法 JSON / 非对象 → {}

# app/ai/providers/base.py
TOOLS_UNSUPPORTED = "当前模型或接口不支持工具调用，请在「模型配置」中换用支持工具调用（function calling）的模型"
async def chat_tools_stream(self, messages, model, tools, *, tool_choice="auto", max_tokens=None,
                            thinking="off", temperature=None) -> AsyncIterator[AgentEvent]   # 基类直接抛 AIProviderError(TOOLS_UNSUPPORTED)
def describe_tools_error(exc) -> str
```

### 3. 契约

- **事件**：`("text", {"text"})` 随流产出；`("usage", {"input_tokens", "output_tokens"})`；`("tool_call", ToolCall)` **只在整条流正常结束后**
  产出（绝不产出半截参数）；最后是 `("finish", {"reason"})`。流没有正常结束（OpenAI 无 `finish_reason`、Anthropic 无 `message_stop` 或有
  tool_use 块没 `content_block_stop`、Gemini 无 `finish_reason`）→ `AIProviderError("模型工具调用流意外中断，请重试")`；有工具调用但停止原因
  是输出长度等（OpenAI 非 `tool_calls|stop`、Anthropic 非 `tool_use|end_turn`、Gemini 非 `STOP`）→ `"模型工具调用未完整生成，请提高最大输出长度后重试"`。
- **基类不退化**：默认实现抛 `TOOLS_UNSUPPORTED`，绝不用文本模拟工具调用。runner 收到后任务 failed，`error` 即该提示。
- **错误映射** `describe_tools_error`：只有报错文本同时提到 `tool` / `function`、含能力拒绝词（`unsupported` / `not support` / `not available` /
  `not implemented` / `unrecognized request argument` / `unknown parameter`）、**不**含载荷错误词（`schema` / `tool_call_id` / `tool_use_id` /
  `tool_result`）、且状态码为空或 400 / 404 / 422 时才映射为 `TOOLS_UNSUPPORTED`；其余走 `describe_error`（保留具体原因，便于排查历史协议问题）。
  三家都是 `except AIProviderError: raise / except Exception → AIProviderError(describe_tools_error(exc))`，`finally` 关闭流与 client。
- **`provider_metadata`**：每条 assistant 响应只在**第一个** tool_call 上保存完整原生块（Anthropic `{"provider": "anthropic", "content": [全部 content
  block]}`，含 thinking / signature；Gemini `{"provider": "gemini", "parts": [...]}`，含 thought signature），随 `agent_messages.tool_calls` 持久化，
  下一次请求同 provider 时原样重放。它不是工具参数，`MessageOut` 与 `/generation-logs` 都去掉它（只返回 `id / name / arguments`）。
- **跨 provider 历史**：`native_metadata` 只恢复当前 provider 的块；换了 provider 时用通用字段重建，参数经 `history_arguments` 解析——别的
  provider 产生的非法 JSON 当时已得到工具结果「工具参数必须是合法的 JSON 对象」，这里只需保持协议结构，不能让之后每轮都失败。

| | openai_compat | anthropic | gemini |
|---|---|---|---|
| 请求 | `chat.completions.create(stream=True, tools=[{"type": "function", "function": spec}], tool_choice="auto"\|"none", stream_options={"include_usage": True})`；`thinking != off` → `reasoning_effort` | `messages.stream(tools=[{name, description, input_schema}], tool_choice={"type": auto\|none})`；system 段合并；`temperature` 仅在 thinking off 时传 | `generate_content_stream`，`FunctionDeclaration(parameters=_gemini_schema(...))`（没有 properties 时传 None），`FunctionCallingConfig(mode=AUTO\|NONE)`，`automatic_function_calling.disable=True`、`automatic_continuation=False` |
| 兼容重试 | 创建请求时 400 且报错含 `stream_options` → 去掉该参数重试一次（只在流开始前；流中途失败不重放） | — | — |
| 参数累积 | 按 `delta.tool_calls[i].index` 拼接 id / name / arguments；缺 id 或 name → 报错 | `content_block_start(tool_use)` + `input_json_delta` 按块拼接；参数为空时用块的初始 `input` | 一个 part 就是完整调用；`will_continue` / `partial_args` → 报错；缺 name → 报错 |
| 调用 id | 模型给出 | 模型给出 | `function.id`，没有时 `call_{uuid4 hex}_{n}`（历史可能被摘要压缩，不能按序号生成） |
| 历史 | assistant 带 `tool_calls`、tool 带 `tool_call_id` | tool → user 的 `tool_result` 块；**相邻同角色消息合并**（user / assistant 必须交替）；有原生块时整段替换 | assistant → `model` 角色 `function_call` part；tool → `user` 角色 `function_response`（带 name 与原生 id，content 解析为对象，非对象包成 `{"result": ...}`）；相邻同角色合并 |
| usage | `chunk.usage`（prompt / completion） | 最终消息：input + cache_creation + cache_read；output | prompt；candidates + thoughts |

- **Gemini schema 清洗** `_gemini_schema`：展开本地 `$ref`（外部或循环引用 → 报错）；删除 `additionalProperties` / `$schema` / `$id` / `$defs` /
  `definitions` / `title` / `default` / `examples`（只删关键字，不删同名业务属性）；`exclusiveMinimum/Maximum` → `minimum/maximum`（整数 ±1，
  其他按包含边界，执行前仍由 Pydantic 严格校验）；`anyOf` 含 null → `nullable: true`（只剩一个分支时展开）。

### 4. 必需的测试

`tests/test_agent_providers.py`（各 SDK 假流：交错文本与多调用、usage、`stream_options` 重试一次、Anthropic 签名 thinking 往返与缓存 token、
Gemini 签名 / 函数 id 往返、schema 清洗且全部已注册工具的 schema 被 SDK 接受、错误映射、截断流 / 输出上限不暴露调用、关闭流释放 client、
基类不退化、跨 provider 非法参数）。`tests/helpers.FakeLLM.tool_script` / `push_tools`：每次 `chat_tools_stream` 消费一项（事件序列，可插入
`Exception` / `Gate`），记录 `tool_calls`（messages 深拷贝、tools、tool_choice），没写 `finish` 时自动补 `stop`，中途关闭计入 `aborted`。

---

## 场景：工具层（`app/services/agent/tools/`）——新增工具的检查清单

### 1. 签名

```python
ToolKind = Literal["read", "small", "delete", "task", "text"]
@dataclass class ToolCtx: db; project; user_id; task_context   # project_id 从 identity 读取（SAVEPOINT 回滚后不触发隐式 IO）
@dataclass class Tool:
    name; description; schema: type[BaseModel]; kind: ToolKind; operation; preview_operation=None; id_model=None
    parameters -> schema.model_json_schema()
    async def run(ctx, args) -> dict       # 仅读工具
    async def preview(ctx, args) -> dict   # 写工具：校验 + 对比，不落库
    async def execute(ctx, args) -> dict   # 写工具：在 SAVEPOINT 内执行
get_tool(name) / list_tools()   # tools/__init__.py 的静态白名单：READ + WRITE + TEXT + TASK
async def validate_references(ctx, args, *, id_model=None)   # tools/references.py
```

### 2. 契约

- **注册表是白名单**：只从 `read.py` / `write.py` / `write_text.py` / `tasks.py` 的 `TOOLS` 列表构建，不按模型给的名字动态导入。
  `test_registry_has_only_explicit_tools` 固定清单，新增工具要同步改测试。
- **kind 分级**：`read`（直接执行）；`small`（新增 / 修改）；`delete`（`delete_*`、`remove_org_member`、`remove_career_assignment`，预览带
  warning「删除会同时影响下列关联数据」）；`text`（仅 `replace_chapter_text`）；`task`（仅 `start_task`）。kind 决定是否可自动执行，见下一场景。
- **参数**：Pydantic `extra="forbid"`（`EmptyArgs` / `arguments(name, schema, with_id=)` 生成），校验失败、`AppError` 一律转成工具结果
  `{"error": ...}`（`NotFoundError` 统一为 `{"error": "不存在"}`，不区分不存在与无权限；`ValidationError` 为 `{"error": "工具参数校验失败", "errors": [{field, message}]}`），
  **不抛异常、不中断这一轮**。
- **引用校验先于 schema**（`_validate` 先 `validate_references` 再 `model_validate`，嵌套在 `params` 里会被丢弃的越权 id 也要拦）：
  - `project_id` 必须等于当前项目，否则「不存在」；出现 `user_id` → 400「工具不能指定用户」；
  - 键 `id` 按 `id_model` 查；`references._MODELS` 中登记的 `*_id` 按对应模型查**属于当前项目**（`WritingStyle` 预设或本人、`AIConfig` 本人、
    `RelationshipType` 预置或本项目、`CareerStage` / `OrganizationMember` / `CharacterCareer` 经父表 join 到项目）；`*_ids` 列表逐项查；
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
- **局部替换正文**（`write_text.py`）：`original` 1–2000 字、`replacement` 0–5000 字；`original` 在当前正文中**恰好出现一次**，否则
  400「原文未找到或不唯一，请扩大或缩小引用范围」；章节 `generating` → 409。预览带 `content_hash`（完整正文变化也要求重新确认）。执行：
  `SELECT ... FOR UPDATE` → `ensure_baseline`（最新版本内容与当前正文不同时再补一个 `baseline`）→ 条件 UPDATE（`status != generating` 且
  `content == 执行前正文`，影响 0 行 → 409「正文同时被其他操作修改」）→ 记 `source="agent"` 版本（note「智能体局部替换」）；结果含
  `before_version_id / after_version_id / before_hash / after_hash`，供撤销使用。正文 hash 变化后分析按现有规则变为 stale。
- **启动任务**（`tasks.py`）：`AllowedKind` 白名单（`chapter.generate / polish / revise / batch / analyze`、`outline.expand / continue`、
  `character.generate`、`organization.generate`、`career.generate`）。`request_params` 用对应请求 schema + `extra="forbid"` 重新校验，
  **强制 `project_id = ctx.project_id`**（覆盖模型给的值），章节类（非 batch）必填 `chapter_id`、`outline.expand` 必填 `outline_id`。
  预览直接调用真实 handler 的 `validate_and_claim`（在预览 SAVEPOINT 内，回滚后不留抢占），再校验 `spec.project_id`；预览含任务参数对比、
  warning「会消耗 AI 调用；已启动的任务只能取消，不能撤销」、所涉章节指纹 `chapter_state`（id / 章号 / 标题 / 原状态 / 正文 hash）与 `ai_config_id`。
  执行用 `task_service.create_task(db, DeferredEngine(...), ...)`：任务行留在调用方事务里，`notify` 推迟到确认请求 commit 之后由真实引擎调用；
  创建后再查一次 `task.project_id`。函数 schema 必须列出 `params` 的字段（Gemini 不接受没有 properties 的空 object）。

### 3. 新增工具检查清单

1. 选对 kind；任何会删除、批量覆盖子记录或消耗 AI 的工具不能是 `small`（或在 `runner._can_auto_execute` 中排除，见 `update_chapter_plans`、带 `stages` 的 `update_career`）。
2. 参数 schema `extra="forbid"`，复用现有请求 schema；新的引用参数名登记到 `references._MODELS`，嵌套 id 列表登记到嵌套映射。
3. 带 `id` 的工具设置 `id_model`；不能访问 `ctx.project_id` 以外的数据。
4. 写入只调用现有 service；需要特殊预览时提供 `preview_operation`，返回值必须稳定（同一数据多次预览相等），否则确认会一直 409。
5. 被改动的表必须在 `effects.EFFECT_TABLES`（及备份注册表 `RULES`）里，否则撤销会漏数据；新增列的中文名补到 `FIELD_LABELS`。
6. 返回值可 JSON 序列化（`serialized`），大结果分页，不依赖结果截断。
7. 测试：preview 对比与 execute 结果、跨项目 / 跨用户 id「不存在」、参数错误作为工具结果、撤销恢复。

### 4. 必需的测试

`tests/test_agent_tools.py`（白名单、CRUD 预览与执行、关系与自定义类型、项目字段白名单、成员 / 职业挂接、细纲与章节标题、读工具与分页、
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
  未知工具 →「工具不存在」；读工具立即 `run` 并写 tool 消息；写工具先检查本轮提案数，≥ 50 → `{"error": "单轮最多 50 项写操作，请分批进行"}`
  （不生成提案，作为与 8 次上限无关的防御）；否则 `preview`，出错直接作为工具结果，成功则建 `agent_changes(status=proposed, seq=本轮序号)`。
- **自动执行**：同时满足才执行——会话 `auto_execute`；本批有待确认提案；`checkpoint.auto_execute_used` 未置；**本轮全部提案就是这一批**（首个写批次）；
  ≤ 3 项；每项 `kind ∈ {small, text}` 且不是 `update_chapter_plans`、不是带 `stages` 的 `update_career`。先置 `auto_execute_used` 并保存断点，再逐项
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

### 4. 必需的测试

`tests/test_agent_runtime.py`（读后作答 + usage、确认续跑 / 拒绝 / 重复确认、自动执行阈值 3、第二批需确认、首批需确认时之后也不自动、含删除的批次、
整体同步不自动、8 次上限且每个调用都有结果、50 项上限独立于工具额度、续跑失败重试不重复写、取消后重试保留已提交结果、取消后新一轮补全协议、
排队中取消的重试保留原提案、日志保留与配置删除、重启中断后重试、不支持工具的提示）；`tests/test_agent_api.py`（会话 CRUD 与偏好继承、
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
  （整轮撤销自动排除 task）；有运行中（非 `paused`）轮次时 → 409「请等待智能体暂停或结束后撤销」。撤销成功后标记 `undone` 并追加一条
  没有 `tool_call_id` 的 tool 消息「用户撤销了：<标题>」，下一轮作为不可信的 user 备注交给模型。

### 3. 必需的测试

`tests/test_agent_effects.py`（删除角色恢复关系 / 成员 / 职业且 id 不变、快照不受 identity map 与他项目影响、自引用与 RESTRICT 外键的父子恢复、
新建父记录后新增依赖 → 冲突、细纲 JSON 引用 → 冲突、原 id 被他项目占用 → 冲突、撤销新建大纲先解除既有章节、修改冲突整体原子、整轮回滚、
章节号平移的每个登记列与检索、删空章节恢复版本 / 分析 / 记忆 / 关联、删大纲恢复 deferred 关联不动正文、撤销插入章节不删之后写入的正文、
唯一冲突回滚部分重插、正文按版本恢复且之后有修改时冲突、新私有关系类型撤销不动预置、新关系 + 新类型先删子后删父、改指向后先恢复引用再删类型、
屏蔽记忆恢复后仍不召回、记忆派生行刷新）。

---

## 场景：`agent.turn` 运行、暂停与续跑

### 1. 签名

```python
# 路由前缀 /api/v1/projects/{pid}/agent（全部校验项目归属，他人 404）
GET|POST /conversations                       # POST {title?} → 201；auto_execute 继承本项目最近一个会话
GET|PATCH|DELETE /conversations/{cid}         # 详情含 messages / changes / active_task；PATCH {title?, auto_execute?}；DELETE 204
GET /conversations/{cid}/generation-logs      # 本会话的 agent.turn 生成记录（去掉 provider_metadata）
POST /conversations/{cid}/messages            # {content 1–8000（去空白）, page_context?, ai_config_id?} → 202 {message, task}
POST /changes/{id}/confirm | reject | undo    # → ChangeOut
POST /turns/{task_id}/confirm-all | reject-all | undo   # → {changes, task, error?}
```

### 2. 契约

- **创建**（`AgentHandler.validate_and_claim`）：会话归属 + 会话行 `FOR UPDATE` + 已有 open 轮次 → 409；`resolve_config` + `build_provider`；
  校验页面上下文后写入 user 消息（会话无标题时取前 40 字），`TaskSpec(resource_key=f"agent:{cid}", chapter_id=None, title=f"智能体：{前 20 字}",
  input={request, ai_config_id, message_id})`。`resource_key` 只挡 queued / running；`paused` 由 service 层 `active_turn`（`OPEN_TASK_STATUSES`）挡住。
- **每次运行**（含续跑 / 重试）：先产出 `reset` 与本轮已持久化的 assistant 文本（`delta`），断点有 `finished_message_id` 直接 `done`；
  有未处理的 assistant 响应（`assistant_message_id != processed_assistant_id`）先处理其工具调用；之后 `context.build_messages` →
  `chat_tools_stream` → 写生成记录（每次模型请求一条 `kind="agent.turn"`，`params = {conversation_id, round, tool_calls, prompt: prompt_meta}`，
  token 用量；摘要请求另记一条 `params.stage = "summary"`）→ **先落库 assistant 消息与断点，再处理调用**。无调用 → `done {message_id, usage}`。
- **断点** `tasks.checkpoint`：`{round, assistant_message_id, processed_assistant_id, allowed_calls, tool_calls_used, pending_change_ids,
  auto_execute_used, usage(累计), finished_message_id}`。每推进一步单独 commit；已有 tool 消息的 `tool_call_id` 跳过，已有提案按状态并入，
  所以续跑 / 重试不会重复写消息或重复执行。
- **paused / resume**：待确认时 runner 产出 `paused`（任务 `paused`，不占并发名额）；全部处理完由 service 自动 `resume_task`，同一任务 id 从断点继续，
  先为每个提案补 tool 消息（执行结果 /「用户拒绝了该修改」/ 失败原因），再继续循环。任务面板不对 `agent.turn` 显示「继续」。
- **取消 / 中断 / 重试**：`on_abort` 不还原任何东西——每项效果与提案状态同事务提交，已执行的保留、可逐项撤销。重试（`validate_retry`）
  每个原任务只允许一次（409「此轮已有重试任务，请使用最新任务继续」），沿用原消息（不建第二条 user 消息），`input.retry_source_id` 指向消息实际归属的任务；
  `build_job` 把旧任务的消息与提案的 `turn_task_id` 改到新任务，断点由 `create_task` 复制。
- **事件**：`reset`、`delta`、`stage`（「调用工具：X」「等待确认 N 项修改」）、自定义 `tool_start {call_id, name, label}`、`tool_end {call_id, ok, summary}`、
  `proposal {change}`、`executed {change}`、`paused {reason, change_ids}`、`done {message_id, usage}`、`error {detail}`。
- **消息组装**（`context.build_messages`）：
  - 预算 = `(context_window_k or 32) × 1000 × 0.6`，按 JSON 字符数近似；超出且多于一组时把最早一半「组」（assistant + 其 tool 结果为一组，不拆开）
    用 `agent.summarize` 压缩进 `conversation.summary`、`summarized_until`，之后只发「较早对话的摘要」+ 未压缩消息；
  - 最新 user 消息始终保留（过长时截断并注明）；其余从新到旧整组放入，放不下的第一组用 `_fit_group` 只裁 content（不裁工具参数或签名），协议本身放不下 →
    400「工具调用上下文超过模型预算，请换用更大上下文的模型或开启新会话」；
  - 历史里缺结果的调用补合成结果：提案已撤销 →「用户已撤销该修改」，有 `change.result` 用它，否则「这次工具调用未完成，请重新查询状态，不要假定操作已执行」；
  - 撤销备注（无 `tool_call_id` 的 tool 行）转成 user 角色的不可信文本，放在对应工具结果组之后（不冒充原生 tool_result）。
- **页面上下文** `validate_page_context`：`route` 须是 `/projects/{pid}` 或以 `/projects/{pid}/` 开头；`chapter_id / outline_id / character_id` 必须属于本项目；
  `selection`（≤ 2000）只在章节有效且确实是正文子串时保留；其余丢弃。

### 3. 必需的测试

见上两节的 runtime / api 测试；`tests/test_agent_context.py`（工具结果合法 JSON、有界且带标记；覆盖模板删不掉安全规则；摘要保留最新 user 与协议组；
损坏覆盖时安全规则提回 system 且项目数据为不可信；带签名的超大组报错而不超预算；页面 id 与选区按项目过滤；撤销备注不拆开工具结果组）；
`tests/test_migrations.py::test_0018_agent_foreign_keys_and_downgrade`。

---

## 场景：安全规则

- **锁定的安全规则**：`app/ai/prompts/agent.py::SAFETY_RULES` 是 `agent.system` 的锁定变量 `safety_rules`（覆盖必须原样出现一次）。渲染后若没有任何
  system 消息包含它（旧的损坏覆盖被通用兜底追加到 user 段），`build_messages` 把它提升为第一条 system 消息并从其他消息里删掉。
- **不可信数据**：项目信息、页面上下文、摘要、撤销备注都用 `untrusted_text` 包在【不可信数据开始】/【不可信数据结束】之间（文本内的同名标记先转成全角
  `［…］`，防止自行关闭数据区）；工具结果统一 `tool_result` = `{"untrusted": true, "data": ...}`，序列化后 ≤ 8000 字，超出时 `data` 变为截断后的 JSON
  字符串并附 `warning: "内容过长已截断，请用分页参数读取"`，外层始终是合法 JSON。
- **项目绑定**：工具只用 `ctx.project_id`；所有引用经 `validate_references`；`start_task` 白名单 + 服务端强制 `project_id` + handler 校验 + 创建后复查。
  参数中出现 `user_id` 直接拒绝；`ai_config_id` 只接受本人的配置，写作风格只接受预设或本人的。
- **原生元数据不外泄**：thinking / signature 只用于协议重放，不出现在消息详情、生成记录接口或前端。
- **正文只经两条路径修改**：唯一匹配的局部替换（`agent` 版本，可撤销），或已注册的生成任务（只能取消）。

---

## 常见错误

```python
# 错误：工具里直接写模型，绕过业务服务、版本与历史
character.personality = args["personality"]; await db.commit()
# 正确：调用 story_service.update_owned（经 TransactionSession 降为 flush），由 execute_change 统一快照与提交

# 错误：确认时直接执行已存储的参数，不比较预览——用户确认的对比与实际写入可能不同
await tool.execute(ctx, change.arguments)
# 正确：execute_change 先重新 preview，不相等时 409 preview_changed

# 错误：新增 *_id 参数却不登记 references._MODELS——会被 400「不支持的引用参数」拒绝；
#       反过来若改成放行，又会成为跨项目访问的入口
```

- 预览里出现随数据库序列变化的值（新 id、时间戳）→ 每次确认都 409。新值用占位符，时间戳列不进差异。
- 在 provider 里边收边产出 `tool_call`，或流中途失败后重放请求——会执行半截参数或重复的工具。
- 撤销时按任意顺序删除 / 插入，或用 ORM `delete(obj)` 触发级联——会误删既有关联或撞 NO ACTION 外键；按注册表顺序用 Core 语句。
- 在 runner 里吞掉 `CancelledError`，或在 `on_abort` 里回滚已执行的提案——已提交的效果应保留并可撤销。
- 写工具新改动一张表却没加进 `EFFECT_TABLES`——执行看起来成功，撤销却漏掉这张表。
