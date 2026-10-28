# 技术设计：项目智能体

## 1. 模块

- `app/ai/providers/`：基类新增工具调用接口，三家 Provider 分别实现。
- `app/ai/tools.py`：定义 `ToolSpec`、`ToolCall`、`AgentEvent` 等与具体 Provider 无关的数据结构。
- `app/services/agent/`
  - `tools/`：工具注册表。`base.py` 是协议，`read.py` 放读工具，`write_*.py` 按领域拆分写工具，`tasks.py` 放启动任务的工具。
  - `effects.py`：表差异快照、撤销与冲突检测。
  - `context.py`：组装消息历史、上下文预算、压缩摘要、页面上下文。
  - `runner.py`：`agent.turn` 任务的循环，负责工具调用、提案、暂停和续跑。
  - `service.py`：会话与消息的增删查，以及提案的确认、拒绝、撤销。
- `app/services/task_engine/handlers/agent.py`、`app/api/v1/agent.py`、`app/schemas/agent.py`。
- 提示词：
  - `agent.system`：system 段可以覆盖，锁定变量 `safety_rules` 必须原样出现一次。
  - `agent.summarize`：压缩历史用。

## 2. Provider 工具调用

### 2.1 接口（`base.py`）

```python
class ToolSpec(TypedDict): name: str; description: str; parameters: dict  # JSON Schema（object）
class ToolCall(TypedDict): id: str; name: str; arguments: str              # arguments 为 JSON 原文；另有可选 provider_metadata
class AgentMessage(TypedDict, total=False):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall]   # assistant
    tool_call_id: str            # tool
    name: str                    # tool（Gemini 需要函数名）

# 事件：("text", {"text"}) | ("tool_call", ToolCall) | ("usage", {"input_tokens", "output_tokens"}) | ("finish", {"reason"})
def chat_tools_stream(self, messages: list[AgentMessage], model: str, tools: list[ToolSpec], *,
                      tool_choice: Literal["auto", "none"] = "auto", max_tokens=None, thinking="off",
                      temperature=None) -> AsyncIterator[tuple[str, dict]]
```

- 基类的默认实现直接抛 `AIProviderError(TOOLS_UNSUPPORTED)`，文案为「当前模型或接口不支持工具调用，请在「模型配置」中换用支持工具调用（function calling）的模型」。
- `tool_call` 事件在单个调用的参数接收完整后才产出，不产出半截的参数。
- `provider_metadata` 保存并原样重放 Gemini thought signature、Anthropic thinking/signature 等 Provider 要求的块信息；只用于对应 Provider，不作为工具参数，随 `tool_calls` JSON 持久化。

### 2.2 三家实现

- **openai_compat**：
  - 用 `chat.completions.create(stream=True, tools=[{"type":"function","function":spec}], tool_choice=..., stream_options={"include_usage": True})`。
  - 按 `delta.tool_calls[i].index` 累积 `id`、`name`、`arguments`，在 `finish_reason` 时产出。
  - 历史中 assistant 消息带 `tool_calls`，tool 消息带 `tool_call_id`。
  - 有些兼容接口不接受 `stream_options`：遇到 400 且报错里提到 `stream_options` 时，去掉这个参数重试一次。
  - 报错里提到 tools 或 function，或者返回 404、400 且含 `tool`，统一映射为 TOOLS_UNSUPPORTED。
- **anthropic**：
  - 用 `messages.stream(tools=[{"name","description","input_schema"}], tool_choice={"type":"auto"|"none"})`。
  - 处理 `content_block_start`（type 为 tool_use）和 `input_json_delta`，按块累积；`message_delta.usage` 产出 usage 事件。
  - 历史中 assistant 消息转成 `tool_use` 块，tool 结果合并进下一条 user 消息的 `tool_result` 块。连续的 tool 消息合成一条 user 消息，因为 Anthropic 要求 user 和 assistant 交替出现。
- **gemini**：
  - 用 `generate_content_stream`，`tools=[types.Tool(function_declarations=[...])]`，`tool_config` 设为 `AUTO` 或 `NONE`。
  - `function_call` 没有 id，用 `f"call_{n}"` 生成。
  - 历史中 assistant 转成 `model` 角色的 `function_call` part，tool 结果转成 `user` 角色的 `function_response` part（带 name）。
  - 参数 schema 先经过 `_gemini_schema()` 清洗：去掉 `additionalProperties`、`$schema` 等 Gemini 不支持的字段。
- 三家的错误都映射成 `AIProviderError`。`follow_redirects` 和 SSRF 设置沿用 `build_provider`。

### 2.3 测试用的 FakeLLM

`tests/helpers` 中的 FakeLLM 新增 `tool_script`：一个列表，每次调用消费一项，每项是事件序列（文本、工具调用）。同时记录收到的 messages 和 tools，用来断言工具结果是否回传、不可信标记是否存在、历史是否被压缩。三家 Provider 的协议转换用各自 SDK 的假响应做单元测试，不发真实请求。

## 3. 数据（迁移 `0018_project_agent`）

开工前先确认编号；与封面任务冲突时顺延。

- **`agent_conversations`**：
  - `id`；`user_id`（CASCADE）；`project_id`（CASCADE，index）；`title`（String 200，默认取第一条消息的前 40 字）。
  - `auto_execute`（bool，默认 false；新会话继承该项目最近一个会话的值）。
  - `summary`（Text）；`summarized_until`（int，最后一条被压缩的消息 id，可为空）。
  - `last_message_at`；时间戳。
- **`agent_messages`**：
  - `id`；`conversation_id`（CASCADE，index）；`turn_task_id`（tasks，SET NULL）。
  - `role`：`user` / `assistant` / `tool`。
  - `content`（Text）；`tool_calls`（JSON，assistant 用）；`tool_call_id`、`tool_name`（tool 用）。
  - `page_context`（JSON，user 用）；`usage`（JSON）；时间戳。
- **`agent_changes`**（写提案和执行记录）：
  - `id`；`conversation_id`（CASCADE）；`project_id`；`turn_task_id`；`tool_call_id`；`tool_name`。
  - `arguments`（JSON）；`kind`：`small` / `delete` / `task` / `text`；`preview`（JSON：`{title, target, diff: [{field, label, before, after}], warnings}`）。
  - `status`：`proposed` / `executing` / `executed` / `rejected` / `failed` / `undone`。
  - `result`（JSON，返回给模型）；`effects`（JSON，见第 5 节）；`error`。
  - `executed_at`、`undone_at`、`seq`；时间戳。
- **项目备份**：在 `project_transfer/registry.py` 中，三张表登记到 `NOT_EXPORTED`（会话属于运行时数据，不跨实例迁移），反射测试要能通过。
- **`VersionSource`**：增加 `agent`。

## 4. 工具层

### 4.1 协议

```python
class Tool(Protocol):
    name: str; description: str; parameters: dict; kind: Literal["read", "small", "delete", "task", "text"]
    async def run(self, ctx: ToolCtx, args: dict) -> dict          # 读工具：返回结果
    async def preview(self, ctx: ToolCtx, args: dict) -> Preview    # 写工具：校验参数并生成对比，不落库
    async def execute(self, ctx: ToolCtx, args: dict) -> dict       # 写工具：执行并返回结果（新 id 等）
```

- `ToolCtx` 包含 `db`、`project`（已校验归属）、`user_id` 和 TaskContext 中的依赖。
- 每个工具只能通过 `ctx.project.id` 访问数据。参数里的各类 id 一律按"属于当前项目"去查，查不到就返回 `{"error": "不存在"}`，不区分"不存在"和"无权限"。
- 参数用 Pydantic 校验，失败时把错误作为工具结果返回给模型，不中断这一轮。
- 写工具的 `preview` 和 `execute` 复用现有服务的校验和写入函数，不另写一套业务规则。

### 4.2 清单

**读工具**（`kind=read`，直接执行）：
- `get_project_overview`：项目字段、统计、写作风格。
- `list_characters(query?)`、`get_character(id)`。
- `list_organizations`、`get_organization(id)`（含成员）。
- `list_relationships(character_id?)`。
- `list_careers`（含阶段）、`list_world_settings(category?)`。
- `get_outline_tree`（卷、大纲、细纲概要）、`get_outline(id)`（含细纲）。
- `list_chapters`（章号、标题、字数、状态、分析状态）。
- `read_chapter(id, page=1)`：每页 4000 字，返回 `page`、`pages`。
- `get_chapter_analysis(chapter_id)`。
- `list_foreshadows(status?)`。
- `search_memories(query, top_k≤10)`、`search_knowledge(query, top_k≤10)`：复用 retrieval。
- `list_writing_styles`、`list_active_tasks`。

**写工具**：

| 工具 | kind | 复用 |
|---|---|---|
| `update_project`（简介、主题、标签、卖点、读者、主题材、目标字数、默认风格） | small | project_service.update_project |
| `create_character` / `update_character` / `delete_character` | small / small / delete | story_service 的 owned 增删改（角色） |
| `create_organization` / `update_organization` / `delete_organization`、`set_org_member` / `remove_org_member` | small，删除类为 delete | organization_service |
| `create_relationship` / `update_relationship` / `delete_relationship` | small，删除为 delete | relationship_service |
| `create_career`（含阶段） / `update_career` / `delete_career`、`assign_career` / `remove_career_assignment` | small，删除类为 delete | career_service |
| `create_world_setting` / `update_world_setting` / `delete_world_setting` | small，删除为 delete | story_service（世界观） |
| `create_outline` / `update_outline` / `delete_outline`、`update_chapter_plans(outline_id, plans)` | small，删除为 delete | outline_service、plan_service（含章节号平移） |
| `create_foreshadow` / `update_foreshadow` / `delete_foreshadow` | small，删除为 delete | foreshadow_service |
| `update_chapter_meta(id, title)` | small | chapter_service.update_chapter（仅标题） |
| `replace_chapter_text(chapter_id, original, replacement)` | text | 见 4.4 |
| `start_task(kind, params)` | task | task_service.create_task |

- `start_task` 允许的 kind：`chapter.generate`、`chapter.polish`、`chapter.revise`、`chapter.batch`、`chapter.analyze`、`outline.expand`、`outline.continue`、`character.generate`、`organization.generate`、`career.generate`。
- 每种 kind 的 params 都必须带当前项目，由对应 handler 的 `validate_and_claim` 校验。
- preview 列出任务标题和主要参数，并提示"会消耗 AI 调用"。

### 4.3 确认分级

- 一轮中所有写调用都先调 `preview`，结果存为 `agent_changes(status=proposed)`。
- **自动执行条件**：会话开启了 `auto_execute`，本轮首次写操作批次的提案数 ≤ 3，且全部是 `small` 或 `text`。整批预览完成后统一判断，满足时逐条 `execute`，断点记录 `auto_execute_used`。同轮后续写操作始终等待确认。
- **边界说明（2026-10-27）**：模型可能先建角色、拿到 id 后再提出关系，无法提前预知整轮写操作总数。自动执行最多发生在首批，后续追加不自动执行，也不回滚先前已完成的小修改。
- 不满足自动执行条件时，本轮全部提案等待确认：runner 产出 `paused`，载荷为 `{reason: "confirm", change_ids}`。
- 本轮写提案超过 50 条时，超出的部分不生成提案，直接给模型返回工具结果 `{"error": "单轮最多 50 项写操作，请分批进行"}`。

### 4.4 局部替换正文

- 参数：`original` 为 1–2000 字，必须在当前正文中**恰好出现一次**；`replacement` 为 0–5000 字。
- 出现 0 次或多次时，preview 报错，并返回给模型"原文未找到或不唯一，请扩大或缩小引用范围"。
- 执行：
  - 在章节写锁内再校验一次；
  - 替换后保存为来源是 `agent` 的版本；
  - 正文 hash 变化后，分析会按现有规则标记为过期。
- 章节正在生成时拒绝执行，复用现有的章节锁判定。

## 5. 效果快照与撤销（`effects.py`）

- **差异范围**：`project_transfer.registry.RULES` 中会被写工具改动的表，即 `EFFECT_TABLES`：
  - `projects`、`characters`、`organizations`、`organization_members`、`relationships`、`relationship_history`；
  - `careers`、`career_stages`、`character_careers`、`career_history`、`world_settings`；
  - `outlines`、`chapter_plans`、`chapters`（不含 `content`）；
  - `foreshadows`、`foreshadow_characters`、`foreshadow_history`、`story_memories`、`chapter_characters`。
- **忽略的列**：`updated_at`、`created_at`，以及派生列（如 `word_count`）。
- **执行流程**：
  1. 执行前用每张表的 `scope(pid)` 读一次快照：`{table: {pk: row_dict}}`。
  2. 执行工具，然后 `flush`。
  3. 再读一次，求差异，得到 `effects = [{table, pk, before|None, after|None}]`。
  4. 与业务写入在同一个事务里提交。
- **成本控制**：只读当前项目的行。章节表排除 `content` 列；`story_memories` 只取差异需要的列。单次执行读两遍，可以接受，因为写操作都经用户确认，频率低。
- **撤销**（单条或整轮；整轮按 `seq` 倒序撤销，在同一个事务里全部成功或全部失败）：
  1. 冲突检测：对每条 effect 读取当前行，比较的是 effect 关心的列，不比较时间戳。
     - 原来是插入：当前行必须仍然存在，且等于 `after`。
     - 原来是删除：该主键必须仍然不存在。
     - 原来是修改：当前行必须等于 `after`。
     - 任一条不满足，返回 409 `{code: "undo_conflict", conflicts: [{table, label}]}`，不做任何修改。
  2. 还原：在外键依赖顺序上，删除插入出来的行、按原 id 重新插入被删除的行、把修改过的行改回 `before`。依赖顺序用 registry 中的顺序，删除时反向；deferred 外键沿用备份导入的处理方式。
  3. 派生数据：恢复的记忆和知识库条目调用 `retrieval_service` 的 upsert 或 delete 重建关键词部分；有向量模型时排队记忆向量任务。
  4. 被撤销的提案标记为 `undone`。在会话中追加一条 tool 角色的系统备注（"用户撤销了：…"），下一轮模型就能知道。
- **局部替换正文的撤销**：当前正文 hash 等于执行后的 hash 时，恢复到执行前的版本内容，并保存来源为 `restore` 的版本；否则返回冲突。
- **启动任务**：不能撤销。提案卡片上显示"取消任务"，调用现有的取消接口。

## 6. 运行循环（`runner.py`，任务 kind 为 `agent.turn`）

- **创建**：`POST /projects/{pid}/agent/conversations/{cid}/messages`，请求体 `{content ≤ 8000, page_context?, ai_config_id?}`。
  - 写入一条 user 消息，然后创建任务。
  - `resource_key = agent:{cid}`：同一会话同时只能有一轮在跑。同一会话里有排队、运行中或暂停的轮次时返回 409。
  - 任务标题为"智能体：{消息前 20 字}"。
- **每次运行（包括续跑）**：
  1. 组装消息：
     - system：渲染 `agent.system`，其中 `safety_rules` 为锁定变量，`project_info` 用现有的 `project_info_block`，`page_hint` 取自本轮 `page_context` 经校验后的说明。
     - 历史：已有摘要加上未压缩的消息。
  2. **预算**：取 `context_window_k`（未配置时按 32k 算）× 1000 × 0.6 作为历史预算，按字符数近似 token 数（偏保守）。历史超出预算时，先调用一次 `agent.summarize`，把最早的一半未压缩消息压成摘要，写入 `conversation.summary` 和 `summarized_until`，再继续。
  3. **工具结果包装**：`{"untrusted": true, "data": ...}`，序列化后上限 8000 字，超出就截断并附说明"内容过长已截断，请用分页参数读取"。项目数据在 system 中用【不可信数据开始】和【不可信数据结束】包裹。
  4. **循环**：每轮最多 8 次工具调用（按工具调用个数累计）。
     - 用 `chat_tools_stream(tool_choice="auto")` 流式输出文本，通过 `delta` 事件推到面板。
     - 收到工具调用后：读工具直接执行；写工具生成提案。
     - 提案需要确认时：先把 assistant 消息（含 tool_calls）和已完成的 tool 结果落库，断点记下待处理的提案，产出 `paused`。
     - 达到 8 次后，最后一次请求用 `tool_choice="none"` 要求模型直接作答。
  5. **结束**：assistant 的最终文本落库，产出 `done {message_id, usage}`。
- **续跑**：确认或拒绝完本轮所有提案后，由 service 调用 `task_service.resume_task`。runner 从断点继续：
  - 为每个提案补一条 tool 消息，内容为执行结果、"用户拒绝了该修改"或失败原因；
  - 然后继续循环，已用的工具调用次数在断点中累计。
- **断点（checkpoint）**：`{tool_calls_used, pending_change_ids, assistant_message_id}`。重启后任务转为中断状态；重试时从断点继续。已落库的消息不重复写，`tool_call_id` 已有对应 tool 消息的就跳过。
- **生成记录**：每次模型请求写一条 `kind=agent.turn` 的记录，`params` 中包含 `{conversation_id, round, tool_calls, prompt: prompt_meta}`，并记录 token 用量。`kind` 的长度是 32，可以容纳。
- **事件**：`delta`、`stage`（例如"调用工具：get_character""等待确认 3 项修改"），以及自定义事件：
  - `tool_start {call_id, name, label}`
  - `tool_end {call_id, ok, summary}`
  - `proposal {change}`
  - `executed {change}`

  前端据此实时更新时间线。

## 7. API（前缀 `/api/v1/projects/{pid}/agent`，都要校验项目归属，否则 404）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET / POST | `/conversations` | 列表 / 新建（`{title?}`） |
| GET / PATCH / DELETE | `/conversations/{cid}` | 详情（消息、提案）/ 改标题和 `auto_execute` / 删除。有活动轮次时不能删除，返回 409 |
| POST | `/conversations/{cid}/messages` | 发送消息，返回 202 `{message, task}` |
| POST | `/changes/{id}/confirm` | 执行；数据变化时返回 409 `{code: "preview_changed", change}` |
| POST | `/changes/{id}/reject` | 拒绝 |
| POST | `/turns/{task_id}/confirm-all`、`/turns/{task_id}/reject-all` | 按 `seq` 依次处理，遇到第一个失败就停止并返回结果 |
| POST | `/changes/{id}/undo`、`/turns/{task_id}/undo` | 撤销；冲突时返回 409 `{code: "undo_conflict", conflicts}` |

- 确认和执行都在请求内同步完成，用 `SELECT ... FOR UPDATE` 把状态原子地切到 `executing`，防止重复点击。
- 本轮所有提案都处理完后，自动调用 resume。

## 8. 前端

- **入口**：`ProjectLayout` 右侧加 `AgentPanel`，懒加载。
  - 折叠状态和宽度存在 localStorage，宽度范围 320–720px，可以拖拽调整。
  - 手机上改为抽屉，从顶栏按钮打开。
- **面板结构**：
  - 会话下拉框，含新建、重命名、删除；"自动执行小修改"开关，修改后 PATCH 到服务端。
  - 时间线：
    - 用户消息；
    - 助手的流式文本，用 markdown 渲染，复用现有的安全渲染方式，不能使用 `dangerouslySetInnerHTML`；
    - 工具步骤，可折叠，显示中文名称和摘要；
    - 提案卡片：逐字段对比表，长文本复用现有的 diff 组件；按钮为确认、拒绝，已执行的显示撤销，启动任务的显示"查看任务 / 取消"。
  - 一轮有多个待确认项时，显示"全部确认（N）""全部拒绝"。
  - 冲突（409）时弹出提示，并刷新对应卡片。
- **当前页面上下文**：发送消息时附带 `{route, chapter_id?, outline_id?, character_id?, selection?}`。这些值由路由参数和编辑器选区（≤ 2000 字）组成；服务端会校验它们是否属于当前项目，不属于的丢弃。
- **数据同步**：
  - 订阅任务流，复用 `useTaskStream`，处理上面的自定义事件。
  - 一轮结束或有修改被执行后，按资源类型使相关页面的数据失效并重新加载（角色、大纲、章节等），复用现有 store 的刷新方法。
- **类型**：`types.ts` 中加入会话、消息、提案、效果的镜像类型；`TaskKind` 加 `agent.turn`；版本来源加 `agent`；生成记录类型加 `agent.turn`。

## 9. 兼容与回滚

- 迁移只新增三张表；downgrade 时删除这三张表，并把 `agent` 版本来源改记为 `manual`。
- Provider 新增的工具调用接口不影响现有的 `generate_stream`。
- 关闭功能只需前端不挂载面板，没有设置开关。
