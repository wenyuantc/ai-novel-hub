# Research: MuMuAINovel 提示词、AI 能力扩展与内容导入

- **Query**: 调研 MuMuAINovel 的提示词模板、提示词工坊、Skill、项目智能体、MCP 插件、拆书导入、项目封面、设置/系统设置、AI Provider/Client 的功能与实现思路（只调研功能与交互，GPL v3 不复制代码）
- **Scope**: 外部仓库内部调研（`/Users/wenyuantc/IdeaProjects/my/MuMuAINovel`）
- **Date**: 2026-10-23

以下路径如无特别说明，均相对于 `MuMuAINovel/backend/app` 或 `MuMuAINovel/frontend/src`。

---

## 1. 提示词模板（PromptTemplates）

**用户可见行为**
- 路由 `/prompt-templates`（`App.tsx:55`）。按分类 Tabs 展示（"全部 (N)" + 每个分类计数），卡片分两种：系统默认（灰色头部，始终启用）和已自定义（紫色头部，可开关启用）。
- 点"编辑"系统模板时会创建"仅当前账户生效的自定义副本"；已自定义的可以"重置"回系统默认。
- 支持整体导出（JSON）和导入；导入后会弹窗列出"内容与系统默认不一致、已转为自定义"的模板。
- 页内有使用说明，变量用 `{variable_name}` 占位。前端编辑弹窗只有名称/描述/内容三个字段，没有调用 preview 接口（`pages/PromptTemplates.tsx:517-574`）。

**数据模型** `models/prompt_template.py`，表 `prompt_templates`
- `user_id`、`template_key`（与系统常量名一致，如 `WORLD_BUILDING`）、`template_name`、`template_content`、`description`、`category`、`parameters`（JSON 字符串，变量名列表）、`is_active`、`is_system_default`
- 唯一索引 `(user_id, template_key)`：每个用户每个 key 最多一份覆盖。

**API**（前缀 `/prompt-templates`，`api/prompt_templates.py`）
- `GET ""` 只返回用户自定义；`GET /categories` 把用户覆盖和未覆盖的系统默认合并后按分类分组（系统模板临时构造对象，`id=template_key`）
- `GET /system-defaults`、`GET /{key}`、`POST ""`、`PUT /{key}`、`DELETE /{key}`、`POST /{key}/reset`
- `POST /export`：导出版本 `2.0`，每项带 `is_customized` 和 `system_content_hash`（SHA256 取前 16 位，`api/prompt_templates.py:28-30`）
- `POST /import`：智能导入（`:443-590`）。标记为系统默认且内容和当前系统默认完全一致时，删除用户覆盖、回落到系统默认；内容不同则转成自定义；标记为自定义的直接 upsert。
- `POST /{key}/preview`：用 `PromptService.format_prompt` 加传入的参数渲染，缺参数时返回错误信息。

**实现思路：模板变量体系**
- 系统模板写成 `PromptService` 的类常量（`services/prompt_service.py`，约 3158 行），多数采用 "RTCO" 风格的 XML 分段（`<system>/<task>/<input priority="P0">/<guidelines>/<output>/<constraints>`）。
- 变量就是 Python `str.format` 的 `{name}` 占位（`format_prompt`，`:2610-2626`），没有模板引擎，也没有条件或循环。
- 元信息集中写在 `get_all_system_templates()` 的 `template_definitions` 字典里（`:2863-3138`），每项含 name/category/description/parameters，约 40 个模板。分类有：封面生成、世界构建、拆书导入、角色生成、大纲生成、章节创作（1-N / 1-1 两种模式，各分首章和后续章）、章节重写、情节分析、情节展开、MCP 测试、MCP 增强、自动角色引入、自动组织引入、灵感模式（9 个）等。
- 运行时取模板：`get_template_with_fallback(key, user_id, db)`（`:2793-2861`）。先查 `user_id + key + is_active=True` 的覆盖，没有就 `getattr(cls, key)`。调用方再 `.format(**ctx)`。
- Skill 也会被并入系统模板列表（`get_all_system_templates` 末尾调用 `get_all_skills_cached()`），key 为 `SKILL_XXX`，参数为 `["user_input"]`，所以 Skill 内容也能在模板页按用户覆盖。
- 写作风格不进模板变量，而是拼进 system prompt（`WritingStyleManager.apply_style_to_prompt` 只在末尾追加"请直接输出章节正文"，`:7-26`）。

**外部依赖**：无，纯 DB + 字符串。

**缺陷 / 可改进点**
- `str.format` 遇到模板里的字面量 `{}`（例如 JSON 示例）就会抛 KeyError 或 IndexError，系统模板需要手写 `{{ }}` 转义。用户自定义模板时很容易写坏，且只有调用时才报错。
- `parameters` 只是展示用的名单，保存时不校验模板里用到的变量是否在名单内，也不校验必需变量是否保留。
- 前端没有接入 preview 接口，用户看不到渲染结果；也没有版本历史或与系统默认的 diff。系统默认升级后，用户覆盖不会收到提醒（hash 只在导出时用到）。

---

## 2. 提示词工坊（PromptWorkshop，社区分享）

**用户可见行为**
- 项目内路由 `/project/:id/prompt-workshop`（`App.tsx:73`），有 3 个 Tab：浏览工坊、我的提交、管理（仅云端管理员可见，`pages/PromptWorkshop.tsx:1040-1051`）。
- 浏览：分类、搜索（名称/描述 ilike）、标签筛选，排序支持 newest / popular（点赞）/ downloads，分页。卡片上可以查看详情、"导入到本地"、点赞。
- 分享：弹窗"分享提示词到工坊"，填名称、描述、内容、分类、标签、作者显示名，可选匿名。提交后进入待审核。
- 我的提交：查看状态（pending/approved/rejected）和审核备注，可撤回。
- 管理端：统计卡（总提示词/官方/待审核/总下载/总点赞），审核提交（通过时可改分类和标签），直接"添加官方提示词"，编辑或删除条目。
- 分类常量 `constants/prompt_categories.py`：general/fantasy/martial/romance/scifi/horror/history/urban/game/other，另有热门标签建议列表。

**数据模型** `models/prompt_workshop.py`
- `PromptWorkshopItem`：name、description、prompt_content、category、tags(JSON)、author_id（格式 `实例ID:用户ID`）、author_name、source_instance、is_official、download_count、like_count、status(active/hidden/deprecated)
- `PromptSubmission`：submitter_id、submitter_name、source_instance、name/description/prompt_content/category/tags、author_display_name、is_anonymous、status(pending/approved/rejected)、reviewer_id、review_note、reviewed_at、workshop_item_id
- `PromptWorkshopLike`：user_identifier + workshop_item_id，二者联合唯一

**API**（前缀 `/prompt-workshop`）：`GET /status`、`GET /items`、`GET /items/{id}`、`POST /items/{id}/import`、`POST /items/{id}/like`、`POST /items/{id}/download`、`POST /submit`、`GET /my-submissions`、`DELETE /submissions/{id}`、`/admin/submissions`、`/admin/submissions/{id}/review`、`/admin/items` CRUD、`/admin/stats`

**实现思路：社区分享怎么做的（中心化云端 + 客户端代理）**
- 同一套代码靠 `WORKSHOP_MODE` 区分角色（`config.py:139-141`）：`server` 是中央云端（官方地址 `https://mumuverse.space:1566`），`client` 是自部署实例（默认）。
- 每个 client 实例首次启动时生成 12 位 `INSTANCE_ID`，持久化到 `.instance_id` 文件（`config.py:158-188`）。用户全局标识写成 `"{INSTANCE_ID}:{user_id}"`。
- client 模式下，每个 API 都通过 `services/workshop_client.py`（httpx，超时 30s）转发到云端，请求头带 `X-Instance-ID` 和 `X-User-ID`。server 模式下直接读写本地表。
- 云端 `AuthMiddleware` 遇到 `/api/prompt-workshop` 路径且带 `X-Instance-ID` 的请求时，视为代理请求，直接把 `X-User-ID` 当作用户身份（`middleware/auth_middleware.py:25-44`）。
- "导入到本地"的落点是**写作风格**（`WritingStyle`，style_type=custom），不是提示词模板（`api/prompt_workshop.py:284-350`）。也就是说，工坊分享的是写作风格类提示词。导入的同时通知云端下载数 +1。
- 审核通过时，从 submission 复制出一条 item（匿名时 author 为空），并回写 `workshop_item_id`。

**外部依赖**：作者运营的云端服务（client 默认依赖它），httpx。

**缺陷 / 可改进点**
- 代理请求的身份完全信任 `X-Instance-ID`/`X-User-ID` 请求头，云端没有签名或令牌校验，任何人都能伪造身份点赞、提交、撤回他人的提交（`auth_middleware.py:28-38`）。
- 点赞计数用读-改-写的 `like_count += 1`，并发下不准确；导入时下载计数也走同样方式。
- 工坊只覆盖写作风格，不能分享提示词模板或 Skill，导出导入的 JSON 和工坊是两套机制。
- 云端不可达时直接返回 503，没有本地缓存。

---

## 3. Skill（SkillChat / SkillManage）

**用户可见行为**
- `/project/:id/skill-chat`：左侧按分类列出 Skill，选中后进入对话（SSE 流式输出）。历史只保存在前端 state，不落库（`pages/SkillChat.tsx`，最多回传最近 20 条）。
- `/project/:id/skill-manage`：表格列出所有 Skill，可查看 SKILL.md 原文、编辑（显示名/分类/描述/触发词/工作流指令/参考资料 JSON）、新建、删除、刷新缓存（`pages/SkillManage.tsx`）。
- 章节生成接口可以带 `skill_key`，把 Skill 工作流注入章节生成的 system prompt（`api/chapters.py:1460, 1744-1760`）。
- 项目智能体会按触发词自动匹配 Skill（见第 4 节）。

**存储形态（文件系统，不在 DB）** `backend/app/skills/<name>/SKILL.md` + `references/*.md`
- 内置 7 个：story-long-write / story-long-analyze / story-long-scan / story-short-write / story-short-analyze / story-short-scan / story-deslop（去 AI 味）。
- SKILL.md 由 YAML frontmatter（name、description、可选 display_name/category/triggers）和 Markdown 工作流正文组成。开发说明见 `skills/README_HOW_TO_ADD_SKILL.md`。

**加载逻辑** `services/skill_loader.py`
- 扫描目录，`template_key = "SKILL_" + name.upper().replace('-', '_')`（`:69-70`）。
- display_name 默认取 description 第一行第一个句号之前的内容。分类按名称推断：含 long 归"Skill·长篇"，含 short 归"Skill·短篇"，含 deslop 归"Skill·润色"（`:80-89`）。
- 触发词：优先用结构化的 `triggers`，否则从 description 里提取 `/xxx` 和「xxx」，并强制包含 `/{name}`（`:92-109`）。
- `content` = 正文 + "附录：参考资料知识库"（把全部 references 整段拼接进去，`:209-217`），并以全局变量 `_skills_cache` 缓存。
- `get_skill_by_trigger`（`:242-300`）：输入等于某触发词或以它开头时命中；否则按硬编码关键词表（如"拆书"、"去ai味"、"润色"等）做子串匹配。注意这个函数每次都重新 `load_skills()`，不走缓存。

**API**（前缀 `/api/skills`）：`GET /list`、`POST /match`、`POST /chat`（SSE）、`GET /detail/{key}`、`POST /create`、`PUT /update/{key}`、`DELETE /delete/{key}`、`POST /refresh-cache`
- chat 的做法：Skill content 作为 system_prompt，历史拼成"以下是之前的对话历史：…用户最新消息:"放进单条 prompt，`auto_mcp=False`，使用用户的 AI 配置，SSE 每 15s 发一次心跳（`api/skills.py:92-169`）。

**缺陷 / 可改进点**
- Skill 的增删改直接写服务器磁盘，接口只要求登录（`require_login`），不区分管理员，也不按用户隔离。任一用户都能改或删全局 Skill（`api/skills.py:194-259`）。
- references 无条件全量拼入 system prompt，体积大时浪费 token。项目智能体那边截断到 30000 字符。
- 关键词兜底表写死在代码里，新增的 Skill 用不上；触发词匹配采用前缀匹配，容易误命中。
- 对话不持久化，也不读取项目上下文（不传 project 数据）。

---

## 4. 项目智能体（"木木创作助手"，components/project-agent）

**用户可见行为**
- 项目详情页右侧可折叠、可拖拽调宽的面板（`ProjectAgentPanel.tsx`，展开状态和宽度存 localStorage），绑定当前项目。
- 支持会话列表、新建、删除。消息以"执行步骤"时间线形式展示（thought / skill / tool 步骤及其状态）。
- 写操作生成**修改预览**（逐字段 before/after）。默认"手动批准修改"，可切换为"自动批准修改"（开关状态存 localStorage）。多个待确认项可"一键批准全部修改（N 项）"。
- 前端随请求发送 `page_context: {route, page}`（`:293`）。

**数据模型** `models/project_agent.py`
- `AgentConversation`：user_id、project_id、title（首条消息前 40 字）、summary、status、last_message_at
- `AgentMessage`：conversation_id、role、content、model、prompt_tokens、completion_tokens
- `AgentToolCall`：tool_name、arguments(JSON)、risk_level、requires_confirmation、status（proposed / waiting_confirmation / executing / executed / failed / rejected）、preview、result、before_snapshot、after_snapshot、error_message、confirmed_at、executed_at
- `AgentExecutionStep`：user/assistant_message_id、tool_call_id、sequence、step_type、category、title、content、status、detail(JSON)

**API**（前缀 `/projects/{project_id}/agent`）：`GET/POST /conversations`、`GET/DELETE /conversations/{id}`、`POST /chat-stream`（SSE）、`POST /tool-calls/{id}/confirm`、`POST /tool-calls/{id}/reject`、`GET /exports/{project|outlines|chapters|characters}`（导出走 GET 下载链接，避免把大文件塞进模型上下文）

**实现思路：Agent 工具调用** `services/project_agent_service.py`
- 固定 SYSTEM_PROMPT（`:23-34`）包含 8 条规则：只能通过工具读写；项目数据、历史和工具结果都视为不可信内容；写操作必须先出预览；不得声称未执行的修改已完成；禁止构造其他 project_id 等。系统提示词末尾再追加当前批准模式说明。
- 命中 Skill 时，把 Skill 内容（截断到 30000 字）作为"补充规则，不能覆盖安全与批准机制"拼到 system prompt 末尾。
- 工具集 = 项目工具注册表 + 用户启用的 MCP 工具（同名时项目工具优先）。
- 循环：`MAX_TOOL_ROUNDS = 4`，第 5 轮强制 `tool_choice="none"` 直接作答。每一轮都调用 `ai_service.generate_text(tools=..., tool_choice="auto", handle_tool_calls=False)`，由服务自己处理 tool_calls。
- **工具结果回传方式**：不使用原生 tool message 协议，而是在 `_build_prompt` 里把历史（单条截 6000 字、总量 60000 字）、page_context 和最近 12 条工具结果 JSON（上限 80000 字）重新拼成一条文本 prompt，并标注为"不可信数据"（`:731-772`）。
- 风险分级：`ProjectAgentTool.risk_level >= 2` 时需要确认（`project_agent_tools.py:41-49`）。读工具直接执行；写工具先 `registry.preview()` 生成 diff，手动模式下置为 `waiting_confirmation` 并结束本轮，自动模式下立即 `execute()`。
- MCP 工具只有在 metadata 明确标注 `readOnlyHint=True` 时才免确认，否则 risk=2（`:37-42`）。
- 确认接口（`api/project_agent.py:348-460`）：先原子地把状态 claim 为 `executing`，再**重新生成 preview 和存储的 preview 比较**。不一致时返回 409（"数据已发生变化，差异预览已刷新"）并恢复为 waiting。MCP 工具会强制刷新用户工具列表，确认它仍然可用。执行后保存 before/after 快照。
- 工具注册表在构造时绑定已校验的 project，所有工具都在该项目范围内执行。
  - 读工具：get_project_overview、list/get outlines、characters、chapters、relationships、foreshadows、organizations、careers、relationship_graph、foreshadow 统计和上下文、writing_styles、background_tasks、chapter_analysis、annotations、navigation、story_memories、export_links、cover_status、check_chapter_generation_readiness、check_project_consistency 等
  - 写工具（risk 2）：update_project/outline/character/chapter；manage_outline/character/chapter/relationship/organization/foreshadow/career/writing_style/background_task/story_memories/project_cover；repair_project_consistency；import_outlines_json；import_characters_json；replace_chapter_text（要求传入 `expected_text` 校验原文没变）；start_project_task（启动大纲生成/展开、章节生成/批量/重写/局部重写、分析、角色/组织/职业生成等后台任务）
  - 一对一大纲模式下，修改大纲或章节会同步修改对应的另一方。
- 连接中断时调用 `finalize_interrupted_turn` 收尾。

**缺陷 / 可改进点**
- 工具结果靠"重拼文本 prompt"回传，每轮重复发送全部历史和工具结果，token 开销大；也用不上模型原生的多轮 tool-calling。
- before_snapshot 已保存，但前端没有"撤销"入口（grep 未发现 undo）。
- 自动批准模式只在前端 localStorage 保存，后端只接收 `auto_approve` 参数。

---

## 5. MCP 插件（MCPPlugins，backend/app/mcp）

**用户可见行为**
- 路由 `/mcp-plugins`。用户粘贴标准的 `{"mcpServers": {...}}` JSON 来添加插件（取第一个 key 作为插件名），并选择分类：search / analysis / filesystem / database / api / generation / general。
- 列表中可以启用/停用、测试（AI 驱动的测试）、查看工具列表、编辑（回填成 mcpServers JSON）、删除；还有指标、缓存、会话统计和清缓存。

**数据模型** `models/mcp_plugin.py`：plugin_name（与 user_id 联合唯一）、display_name、description、plugin_type（http/stdio/streamable_http/sse）、server_url、command、args、env、headers、config、tools(JSON)、enabled、status（active/inactive/error/pending）、last_error、last_test_at、category、sort_order

**API**（前缀 `/mcp/plugins`）：CRUD、`POST /simple`（JSON 配置 upsert）、`/{id}/toggle`、`/{id}/test`、`/{id}/status`、`/{id}/tools`、`/metrics`、`/cache/stats`、`/sessions/stats`、`/cache/clear`、`POST /call`

**实现思路：MCP 接入方式**
- 使用官方 Python `mcp` SDK：`ClientSession` + `streamablehttp_client` / `sse_client`（`mcp/facade.py:38-40, 299-385`）。只支持远程 HTTP/SSE。
- `MCPClientFacade` 是单例，会话池以 `user_id:plugin_name` 为 key，带按用户的锁和后台清理/健康检查循环。参数见 `mcp/config.py`：最多 1000 个客户端，TTL 1h，空闲 30min，健康检查 60s，错误率 0.7 判为严重，工具缓存 10min，重试 3 次，调用超时 60s。
- 创建或更新插件时把状态置为 `pending`，再用 `asyncio.create_task` 在后台注册连接，不阻塞请求。
- URL 经 `validate_public_http_url` 校验（防 SSRF，`api/mcp_plugins.py:36-41`）。
- 工具暴露给模型：`format_tools_for_openai` 把工具名改写为 `{plugin_name}_{tool_name}`，inputSchema 直接作为 parameters（`facade.py:941-970`）。调用时用 `parse_function_name` 按第一个 `_` 拆回。
- `services/mcp_tools_loader.py`：单例，按用户缓存工具 5 分钟，并单独存工具 metadata（比如 readOnlyHint，只给服务端判断用，不发给模型）。
- 普通 AI 调用（`services/ai_service.py`）默认 `auto_mcp=True`：只要用户有启用的插件，就自动带上工具。`_handle_tool_calls` 最多跑 `max_rounds=2` 轮，把结果用 `build_tool_context(format="markdown")` 拼到原 prompt 后面再请求一次。大纲/世界观等模板里有 `mcp_references` 变量，另有 `MCP_WORLD_BUILDING_PLANNING` / `MCP_CHARACTER_PLANNING` 规划模板。
- 测试：`services/mcp_test_service.py` 用 `MCP_TOOL_TEST` 模板让 LLM 选一个工具并实际调用。

**外部依赖**：`mcp` Python SDK、anyio。

**缺陷 / 可改进点**
- 模型和前端接受 `stdio` 类型并保存 command/args/env，但运行时只支持 HTTP 类，stdio 会记录"暂不支持的插件类型"（`api/mcp_plugins.py:159`）。
- 插件名本身含 `_` 时，`parse_function_name` 按第一个 `_` 拆分会拆错。
- `auto_mcp` 默认开启，所有生成调用都会带上工具，可能干扰 JSON 输出，也增加延迟。
- 会话池是进程内状态，多 worker 部署时不共享。

---

## 6. 拆书导入（BookImport）

**用户可见行为**（`pages/BookImport.tsx`，Steps：上传文件 → 解析中 → 预览修改 → 生成导入）
- 上传 .txt（不超过 50MB）。解析范围有两种：`tail`（截取末尾 N 章，N 须为 5 的倍数且不小于 5，超过 50 按整本处理）和 `full`（整本）。
- 预览页可以修改 AI 反推的项目信息（简介、主题、类型、叙事视角、目标字数）、章节和大纲，并显示警告（章节过短 <300 字、过长 >12000 字、标题重复、已按配置截断）。
- 确认后 SSE 推送进度：建项目 → 导入大纲 → 导入章节 → AI 生成世界观 → 职业体系 → 角色/组织。失败的步骤可以"重试"或"跳过"。
- 只支持新建项目（传 project_id 会被拒绝）；import_mode 有 append/overwrite 两种。

**API**（前缀 `/book-import`）：`POST /tasks`（multipart 上传）、`GET /tasks/{id}`、`GET /tasks/{id}/preview`、`POST /tasks/{id}/apply`、`POST /tasks/{id}/apply-stream`（SSE）、`POST /tasks/{id}/retry-stream`（SSE，body `{steps:[world_building|career_system|characters]}`）、`DELETE /tasks/{id}`

**数据结构**（`schemas/book_import.py`）：ProjectSuggestion、BookImportChapter{title, content, summary, chapter_number, outline_title}、BookImportOutline{title, content, order_index, structure}、BookImportWarning{code, message, level}。任务对象 `_BookImportTask` **存在内存 dict 里**（`services/book_import_service.py:85-125`），不落库。

**实现思路：拆书流程与分块策略**
1. 解码（`services/txt_parser_service.py`）：依次尝试 utf-8 → utf-8-sig → gb18030 → gbk → big5，最后用 utf-8(ignore) 兜底。
2. 清洗：统一换行，去 BOM，全角空格转两个半角空格，去行尾空白，4 个以上连续空行压成 3 个。
3. 切章，规则优先：
   - 强模式正则：`^第[一二…万零〇两\d]+[章节回卷集部篇].*$`、`^chapter\s*\d+`、`^chap\.\s*\d+`
   - 弱模式：行长不超过 25、不含标点、前后都是空行
   - 第一个标题前的正文不少于 200 字时保留为"前言"章
   - 一个标题都识别不到时走固定窗口兜底：3000–5000 字一块，在 `。！？!?\n` 处断开
4. 选章：按 tail/full 截取，并重新编号为 1..N。
5. 摘要：规则截取正文前 120 字，不调用 AI（`_build_summary`，`:2236`）。
6. AI 反推项目信息：只取前 3 章、每章前 2000 字，用 `BOOK_IMPORT_REVERSE_PROJECT_SUGGESTION` 模板生成。失败时有规则兜底，通过关键词检测 genre/theme/叙事视角。
7. AI 反推大纲：**每批 5 章**，每章输入"摘要 + 正文前 2200 字"，用 `BOOK_IMPORT_REVERSE_OUTLINES` 模板输出与 OUTLINE_CREATE 一致的结构（`:1256-1373`）。失败时用 fallback 结构补齐。
8. 世界设定的 time_period/location/atmosphere/rules 也有基于关键词的规则推断（`_derive_world_settings`）。
9. apply 时写库：项目 → 大纲 → 章节 → 默认写作风格；之后复用向导的生成流程补世界观、职业、角色/组织，`wizard_step` 置为 3。

**缺陷 / 可改进点**
- 任务状态在进程内存里，重启后丢失，多 worker 下找不到任务；也没有 TTL 清理逻辑（未确认，未看到清理代码）。
- 只支持 TXT，不支持 epub/docx；不能导入到已有项目。
- 反推大纲时每章只看前 2200 字，长章节后半部分的情节会丢失；摘要是机械截断，不是真正的摘要。
- 弱标题规则（短行加前后空行）在对话体文本里容易误判。

---

## 7. 项目封面（简要，另有任务跟进）

- API：`POST /projects/{id}/cover/generate`（body `overwrite`）、`GET /projects/{id}/cover/download`（`api/project_covers.py`）；设置页另有 `POST /settings/cover/test`。
- Project 字段：cover_image_url、cover_prompt、cover_status（none/generating/ready/failed，带 CHECK 约束）、cover_error、cover_updated_at。
- Settings 字段：cover_api_provider、cover_api_key、cover_api_base_url、cover_image_model、cover_enabled。
- Provider：`services/cover_providers/` 下有 base、gemini、grok 三个。grok 走 OpenAI 风格的 `/images/generations`，兼容 b64 和 url 两种返回。提示词模板 `NOVEL_COVER_PROMPT_TEMPLATE`（2:3 竖版，必须含标题文字、无 logo/水印，description 截前 300 字），可以被用户覆盖。
- 智能体工具 `manage_project_cover`（generate/clear）和 `get_project_cover_status` 也能操作封面。

---

## 8. 设置（Settings）与系统设置（SystemSettings）

**用户可见行为（Settings.tsx）**
- 分为"文本模型配置"和封面图片配置等 Tab。Provider 选项：MuMuのAPI（内置中转，实际是 openai 兼容）、Xiaomi MiMo（内置，默认模型 mimo-v2.5）、OpenAI Compatible、Google Gemini；Anthropic 选项在前端被注释掉了（`pages/Settings.tsx:314-328`）。封面 Provider 有 mumu / gemini / grok。
- 字段：API Key、Base URL、模型（可点击从服务端拉取模型列表）、temperature（0–2）、max_tokens、关闭思考（disable_thinking）、全局 system_prompt。
- 测试连接、检测 Function Calling 支持。
- **API 预设**：保存多套配置，可以切换激活、从当前配置创建、单独测试；可以给"章节分析"指定专用预设。

**数据模型** `models/settings.py`（每用户一行）：api_provider、api_key、api_base_url、llm_model、temperature、max_tokens、system_prompt、disable_thinking、cover_*、smtp_*（系统级，仅管理员）、email_auth_enabled、email_register_enabled、verification_code_ttl_minutes、verification_resend_interval_seconds、`preferences`（Text 类型的 JSON，里面放 `api_presets: {presets:[{id:"preset_<ms>", name, config{...}}], version}` 和 `chapter_analysis_preset_id`）

**API**（前缀 `/settings`）
- `GET/POST/PUT/DELETE ""`：首次 GET 时用 .env 默认值建一行
- `GET /models`：openai 兼容走 `{base}/models`，anthropic 走 `/v1/models`，gemini 走 `/models?key=` 并只保留支持 generateContent 的模型
- `POST /check-function-calling`：发一个 get_weather 测试工具，看是否返回 tool_calls
- `POST /test`
- `/presets` CRUD、`/presets/{id}/activate`、`/presets/{id}/test`、`/presets/from-current`、`PUT /presets/usage/chapter-analysis`
- `/system/smtp` GET/PUT/test（require_admin）
- 按用途取 AI 服务：`get_user_ai_service_from_db_by_usage(usage="chapter_analysis")`（`api/settings.py:247-290`）

**SystemSettings.tsx**（管理员）：邮件服务配置（服务商/主机/端口/用户名/授权码/发件人/SSL/TLS）、注册与验证码策略、公告管理（标题/级别/摘要/Markdown 正文/状态 draft/published/hidden/发布时间/过期时间/置顶）。

**缺陷 / 可改进点**
- `api_key`、`cover_api_key`、`smtp_password` 以明文 String 存在 DB 里，但前端 tooltip 写着"将加密存储"（`Settings.tsx:1303`）。另外 `SettingsResponse` 继承的 base schema 含 `api_key`，GET 接口会原样返回完整密钥（`schemas/settings.py:12, 37`）。
- 预设存在 preferences 的 JSON 文本里，没有独立表，并发写可能互相覆盖。
- 模型按"用途"路由只做了章节分析一项，其他用途都用激活配置。

---

## 9. AI Provider / Client 层（services/ai_providers、services/ai_clients）

- 分两层：`ai_clients/*`（openai / anthropic / gemini 的 HTTP 层）+ `ai_providers/*`（`BaseAIProvider.generate/generate_stream`，签名含 prompt、model、temperature、max_tokens、system_prompt、tools、tool_choice）；`AIService`（`services/ai_service.py`）做统一编排，负责 MCP 注入、工具轮次和指标统计（`ai_metrics.py`）。
- `normalize_provider`：`mumu` 映射为 `openai`；`xiaomi_mimo` 在 API 层解析为 openai 并填默认 base_url（`api/settings.py` 的 `resolve_runtime_ai_config`）。
- OpenAI client：httpx 连接池（按 key 复用），重试 3 次、指数退避（401/403/404 不重试），全局信号量限制并发 5，超时 connect 90s / read 300s（`services/ai_config.py`）。能把 SSE 形式的非流式响应也解析出来，合并流式 tool_call 增量；用 `split_content_and_reasoning` 分离 `<think>` 等推理内容，正文为空时丢弃推理文本，避免污染 JSON。`disable_thinking` 通过 `extra_body.chat_template_kwargs={"enable_thinking": False}` 注入（适配 vLLM/Qwen）。
- Anthropic client 使用官方 `anthropic` SDK（AsyncAnthropic），并把 OpenAI function 格式的工具转换为 Anthropic 格式。
- Gemini client 直接用 httpx 调 `:generateContent` / `:streamGenerateContent?alt=sse`，API key 放在 query 参数里；会把 JSON Schema 转成 Gemini 的 schema。
- 外部依赖：httpx、anthropic SDK。

---

## 关联本项目

- 本项目任务目录已有 `research/current-inventory.md`（现状盘点）；最近一次提交 `0701f81` 已把模型配置拆为文本模型和图片模型，对应 MuMu 的 `llm_*` 与 `cover_*` 两组配置。
- 本次未阅读 `.trellis/spec/`，与 spec 的对齐留给实现阶段。

## Caveats / Not Found

- 未逐行阅读 `prompt_service.py` 中每个模板的全文，只确认了变量列表和结构风格。
- `ai_service._handle_tool_calls` 的轮数默认值是从函数签名 `max_rounds: int = 2` 读到的，调用方是否覆盖没有全部核对。
- 未确认拆书内存任务是否有过期清理（没有搜到相关代码）。
- 未运行 MuMuAINovel，所有行为描述都来自读源码。
