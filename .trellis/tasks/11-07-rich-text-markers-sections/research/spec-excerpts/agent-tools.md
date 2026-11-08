<!-- 自动摘录自 .trellis/spec/backend/agent.md；逐行保留原文，使用时按行号回查完整规范。 -->

<!-- .trellis/spec/backend/agent.md:1-166 -->
# 项目智能体（`agent.turn`）：工具调用 / 提案与确认 / 效果快照与撤销

> 来源任务：`10-23-project-agent`（迁移 `0018`）。任务 / 订阅契约见 [error-handling.md](./error-handling.md)「后台任务与 SSE 契约」，
> 断点与待审核见 [batch-generate.md](./batch-generate.md)，模板注册与锁定变量见 [prompts-styles.md](./prompts-styles.md)，
> 表规则注册表见 [project-transfer.md](./project-transfer.md)，章节号平移见 [outline.md](./outline.md)。
> Skill 读工具 / 强制启用、MCP 动态工具与 `external` 提案（`10-23-skills-mcp`，迁移 `0019`）的完整契约见 [skills-mcp.md](./skills-mcp.md)，本文只记录与智能体主流程的接口。

代码位置：`app/ai/tools.py`（与 provider 无关的数据结构）、`app/ai/providers/{base,openai_compat,anthropic,gemini}.py`（`chat_tools_stream`）、
`app/services/agent/`（`tools/` 工具注册表、`effects.py` 差异快照与撤销、`context.py` 消息组装、`runner.py` 循环、`service.py` 会话 / 提案、
`transactions.py` 隔离级别、`mcp_tools.py` 本轮 MCP 工具映射与连接）、`app/services/task_engine/handlers/agent.py`、`app/api/v1/agent.py`、`app/schemas/agent.py`、`app/ai/prompts/agent.py`。

> Agent 设定读取、回执及传输受 [AI 可见性契约](./setting-ai-visibility.md) 约束。项目收紧后旧会话仍可读，`history_sealed` 禁止继续／重放；外部工具不绕过后续模型发送门禁。

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
  Pydantic 单值 `Literal[str]` 的 `const` 必须等价转换为单项 `enum`，保留同名业务字段；非文本常量返回 `AIProviderError`，不能静默丢弃约束。 对 `start_task.params.style` 的字符串／整数联合，仅 Gemini 声明改为字符串：`inherit`、`none` 或数字文本；Provider 在事件边界将合法数字文本转为整数，原样重放签名 parts，后续仍由原任务 schema 和风格归属校验。显式 null 不改写，沿用原执行校验。

### 4. 必需的测试

`tests/test_agent_providers.py`（各 SDK 假流：交错文本与多调用、usage、`stream_options` 重试一次、Anthropic 签名 thinking 往返与缓存 token、
Gemini 签名 / 函数 id 往返、schema 清洗且全部已注册工具的 schema 被 SDK 接受、错误映射、截断流 / 输出上限不暴露调用、关闭流释放 client、
基类不退化、跨 provider 非法参数）。`tests/helpers.FakeLLM.tool_script` / `push_tools`：每次 `chat_tools_stream` 消费一项（事件序列，可插入
`Exception` / `Gate`），记录 `tool_calls`（messages 深拷贝、tools、tool_choice），没写 `finish` 时自动补 `stop`，中途关闭计入 `aborted`。

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
get_tool(name) / list_tools()   # tools/__init__.py 的静态白名单：READ + SKILL + WRITE + TEXT + TASK（共 59 个）
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
- **引用校验先于 schema**（`_validate` 先 `validate_references` 再 `model_validate`，嵌套在 `params` 里会被丢弃的越权 id 也要拦）： `owned_reference` 在数据库绑定前要求 ID 为 1–2,147,483,647 的整数，再查项目/用户归属；越界返回可处理的工具错误，不让 SQLite/PG 整数溢出。
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
