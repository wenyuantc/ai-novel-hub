# Research: aiNovel 当前能力盘点（对标 MuMuAINovel 的基线）

- **Query**: 盘点本项目已实现能力：技术栈与架构约定、功能清单（模型字段 / API / 页面 / 完成度 / 限制）、ER 概览、扩展挂点
- **Scope**: internal
- **Date**: 2026-10-23
- **基线提交**: `0701f81 feat: 模型配置区分文本模型与图片模型`

---

## 1. 技术栈与架构约定

### 1.1 总体

| 层 | 技术 | 证据 |
|---|---|---|
| 后端 | Python 3.12、FastAPI、SQLAlchemy 2 async、Alembic、pydantic-settings、uv 管理 | `backend/pyproject.toml` |
| AI SDK | `openai>=1.40`、`anthropic>=0.34`、`google-genai>=1.0` | `backend/pyproject.toml` |
| 数据库 | 生产 PostgreSQL 16（asyncpg）；测试 / 本地可用 SQLite（aiosqlite） | `docker-compose.yml`、`backend/app/core/database.py` |
| 前端 | Vite 8 + React 18 + TypeScript strict + Ant Design 5（zh_CN）+ Zustand 5 + React Router 6 + dayjs + Vitest | `frontend/package.json`、`.trellis/spec/frontend/index.md` |
| 部署 | 单镜像多阶段构建：node 构建前端 → python 运行时，FastAPI 同进程托管 `frontend/dist`（SPA 回退）；compose 只有 `db` + `app` 两个服务，app 无数据卷 | `Dockerfile`、`docker-compose.yml`、`backend/app/main.py:21-37` |
| 质量门禁 | 后端 `uv run ruff check . && uv run pytest`；前端 `npm run lint && npm run build && npm test` | `README.md`、`.trellis/spec/backend/index.md` |

未引入：Redis / 任务队列 / 向量库 / 对象存储 / 文件上传（`python-multipart` 未安装）/ WebSocket / MCP / 富文本编辑器 / 图表库。

### 1.2 后端分层

`api/v1`（轻薄路由，Pydantic 校验 + 依赖注入，调用一个 service）→ `services`（全部业务查询与 `user_id` 隔离）→ `models`；`app/ai/` 不知道 HTTP 与 DB，只提供 `LLMProvider`、提示词纯函数、SSRF 防护、JSON 抽取（`.trellis/spec/backend/directory-structure.md`）。

- 应用工厂 `create_app(settings, provider_factory)`（`backend/app/main.py:40`），共享对象挂在 `app.state`：`settings / engine / sessionmaker / cipher / provider_factory`，测试可注入 Fake LLM 工厂。
- 依赖别名（`backend/app/core/deps.py`）：`SettingsDep`、`DbDep`、`CipherDep`、`FactoryDep`、`SessionmakerDep`、`CurrentUser`、`AdminUser`。
- 统一错误：`AppError` → `BadRequest(400)/Unauthorized(401)/Forbidden(403)/NotFound(404)/Conflict(409)`，响应体统一 `{"detail": "中文"}`；422 只保留 `type/loc/msg`、不回显输入；未处理异常 500「服务器内部错误」（`backend/app/core/errors.py`）。
- 配置全部来自环境变量，`JWT_SECRET`（≥16）、`ENCRYPTION_KEY`（合法 Fernet）缺失即启动失败；其它：`REGISTRATION_ENABLED`、`COOKIE_SECURE`、`ACCESS_TOKEN_EXPIRE_MINUTES`（默认 7 天）、`CORS_ORIGINS`、`CONTEXT_CHAR_BUDGET`（默认 12000）、`AI_REQUEST_TIMEOUT`（默认 300s）、`ALLOW_PRIVATE_AI_ENDPOINTS`、`ADMIN_USERNAME/ADMIN_PASSWORD`（`backend/app/core/config.py`）。

### 1.3 数据库与迁移

- ORM：Declarative + `Mapped[...]`，所有表继承 `TimestampMixin`（`created_at/updated_at`，timezone-aware，`server_default=now()`），`Base.metadata` 带命名约定（`backend/app/models/base.py`）。
- **不声明 ORM relationship**：级联由外键 `ON DELETE CASCADE / SET NULL` 负责，所有访问经 service 显式查询（`entities.py` 文件头注释）。SQLite 下通过 `PRAGMA foreign_keys=ON` 让级联生效（`database.py`）。
- sessionmaker `expire_on_commit=False`，便于流式响应期间读取属性。
- 迁移：Alembic 异步 `env.py`（`render_as_batch=True`，兼容 SQLite），版本链 `0001_initial_schema → 0002_ai_config_model_params → 0003_user_admin_fields → 0004_ai_config_model_type`；**下一版应为 `0005_*`**。容器启动命令 `alembic upgrade head && uvicorn ...`（`Dockerfile`），应用 lifespan 不建表，只做管理员引导。有 `tests/test_migrations.py`。
- 枚举以 `String` 列 + Python 常量 / Pydantic `Literal` 表达（`CHAPTER_STATUSES`、`AI_PROVIDERS`、`PROJECT_STATUSES`），无 DB enum 类型。

### 1.4 鉴权

- 本地账户，bcrypt 哈希（密码 6–72 字节），JWT HS256，payload `{sub, ver, iat, exp}`（`backend/app/core/security.py`）。
- 令牌下发为 **httpOnly cookie `access_token`**（`samesite=lax`，`secure` 由 `COOKIE_SECURE` 控制），同时兼容 `Authorization: Bearer`（`deps.py:_extract_token`）。
- `User.token_version`：重置密码 / 禁用 / 自助改密时 +1，令牌 `ver` 不一致即 401，实现会话立即失效。
- 角色只有 `is_admin` 布尔；`AdminUser` 依赖 403；管理员看不到他人小说内容。
- 数据隔离：所有项目内资源先 `get_owned_project(db, user_id, project_id)`，他人项目与不存在统一 404（`project_service.py`）。
- 无 OAuth / LinuxDO / 邮箱验证 / 多租户共享。

### 1.5 SSE 流式

- 后端 `backend/app/api/sse.py`：`sse_response(events)` 包装 `AsyncIterator[(event, data)]`；帧格式 `event: delta|done|error` + `data: <JSON>`；先发 `: stream-start` 注释帧；头 `Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no`；用 `aclosing` 保证客户端断开时业务生成器 `finally` 立即执行。
- **两阶段模式**（`generation_service.py` / `wizard_service.py`）：
  1. `prepare_*_job(db, ...)`：在请求会话内完成鉴权、配置解析、上下文组装、状态抢占；业务错误以普通 JSON 4xx 抛出。
  2. 路由 `await db.close()` 后返回 `sse_response(run_*_job(job, sessionmaker))`，流式阶段使用独立会话落库。
- 事件载荷：`delta {text}`；`done` 章节为 `{chapter: ChapterOut}`、向导为 `{kind, items}`；`error {detail}`。
- 章节生成状态机：`draft/done → generating`（`UPDATE ... WHERE status != 'generating'` 原子抢占，失败 409）→ 成功 `done`；失败 / 断开时在 `anyio.CancelScope(shield=True)` 中回退为原状态，正文不变。
- 前端 `frontend/src/api/sse.ts`：基于 `fetch + ReadableStream` 的 POST SSE（不能用 EventSource），`SSEParser` 处理跨块、CRLF、注释帧、多行 data；`runGeneration<TDone>(path, body, {onDelta, signal, parseDone})`；`StreamError` / `isAbortError`。Hook `useStreamTask()`（`frontend/src/hooks/useStreamTask.ts`）管理 `text/running/error/start/stop/reset`，卸载自动 abort。Vite 开发代理对 `text/event-stream` 关闭缓冲（`frontend/vite.config.ts`）。

### 1.6 AI 多提供商抽象

- 抽象类 `LLMProvider`（`backend/app/ai/providers/base.py`）：
  - `generate_stream(messages, model, *, temperature, max_tokens, thinking) -> AsyncIterator[str]`（只返回正文，不含思考内容；失败抛 `AIProviderError`，消息为中文）
  - `list_models() -> list[ModelInfo]`
  - `follow_redirects` 属性（禁私网时置 False）
  - 工具：`split_system()`、`describe_error()`；`ChatMessage = {role: system|user|assistant, content}`；`ThinkingLevel = off|low|medium|high|xhigh|max`
- 实现（`backend/app/ai/providers/`）：
  - `openai_compat.py`：AsyncOpenAI，`thinking != off` 时传 `reasoning_effort`，不读 `reasoning_content`；适用 OpenAI / 中转 / DeepSeek / Ollama 等。
  - `anthropic.py`：4.5 及更早模型用 `thinking.budget_tokens`，新模型用 adaptive thinking + `output_config.effort`；`text_stream` 只取正文。
  - `gemini.py`：1.x/2.x 用 `thinking_budget`，3+ 用 `thinking_level`。
- 工厂：`create_provider(provider, api_key, base_url, timeout)`，注册表 `_PROVIDERS`（`providers/__init__.py`）；类型别名 `ProviderFactory`。
- `ai_config_service.build_provider(cfg, cipher, factory, timeout, allow_private=)`：解密 Key → 未允许私网时 `ensure_endpoint_allowed(base_url)`（SSRF 防护，`backend/app/ai/endpoint_guard.py`）→ 工厂创建。
- `resolve_config(db, user_id, config_id, model_type="text")`：指定 id 则校验类型；否则取该类型默认（无默认取最新），都没有则 400「尚未配置文本模型」。
- 参数推导：`max_output_tokens(cfg) = max_output_k*1000`；`context_char_budget(cfg, default) = (context_window_k - (max_output_k or 8))*1000 - 4000`（≥1000，中文 1 字≈1 token）。
- **没有图片生成实现**：`model_type="image"` 只能存配置、连通性测试走 `list_models()`；Provider 无 `generate_image` 方法。
- **没有非流式调用 / 函数调用 / 结构化输出 / embedding 接口**；向导的 JSON 结构化依赖提示词 + `app/ai/json_extract.py:extract_json`（容错抽取）。
- 不记录 token 用量、无计费 / 调用日志表。

### 1.7 前端 UI 与状态管理

- 所有后端调用经 `src/api/`：`client.ts`（`API_BASE='/api/v1'`、`ApiError(status, detail)`、`buildUrl`、`credentials: 'include'`、401 时调用注入的 `unauthorizedHandler` 清会话）、按资源拆分 `auth/admin/aiConfigs/projects/story/chapters/wizard.ts`，类型集中在 `api/types.ts`（镜像后端 schema）。
- Zustand store 仅三个（`src/stores/`）：`auth`（status/user/registrationEnabled/init/login/register/logout/clear）、`aiConfigs`（configs/load/reset + `selectTextConfigs`）、`project`（当前项目 load/refresh/setProject/reset）。其余为组件本地状态。
- Hooks：`useStreamTask`、`useLoader`（数据加载 + reload）、`useProjectId`、`useHasAIConfig`。
- 通用组件：`AppLayout`（顶栏 + 用户菜单）、`RequireAuth`、`RequireAdmin`、`AIConfigSelect`、`StreamOutput`、`WizardGenerator`、`WizardModal`、`StoryPreview`、`ExportModal`、`ChangePasswordModal`。
- 正文编辑器为 antd `Input.TextArea`（纯文本），无 Markdown / 富文本。

---

## 2. 已实现功能清单

### F1 账户与会话 — 完成
- 模型：`users(id, username uniq ≤64, password_hash, is_admin, is_active, token_version, created_at, updated_at)`
- API（`/api/v1/auth`）：`GET /config`（是否开放注册）、`POST /register`、`POST /login`、`POST /logout`、`GET /me`、`POST /change-password`
- 前端：`/login`（`LoginPage.tsx` 登录/注册切换）、`ChangePasswordModal`（顶栏菜单）
- 限制：无 OAuth、无找回密码、无邮箱、无头像/昵称等资料字段。

### F2 管理员用户管理 — 完成
- API（`/api/v1/admin`）：`GET /users?q=&page=&page_size=`（含 `project_count`）、`POST /users`、`PATCH /users/{id}`（is_admin/is_active）、`POST /users/{id}/reset-password`、`DELETE /users/{id}`
- 防护：不能禁用/降级/删除自己，始终保留至少一名启用管理员（`admin_service._ensure_other_active_admin`）；启动时 `bootstrap_admin` 按环境变量引导管理员。
- 前端：`/admin/users`（`AdminUsersPage.tsx`，`RequireAdmin` 包裹）
- 限制：无系统级设置页、无用量统计、无公告。

### F3 AI 模型配置 — 完成（图片模型仅能配置）
- 模型：`ai_configs(id, user_id→users CASCADE, name, provider[openai_compat|anthropic|gemini], base_url, api_key_enc(Fernet), model, model_type[text|image], is_default, thinking_level, context_window_k, max_output_k, ts)`；默认配置按 `model_type` 各自唯一，同类型第一条自动默认，删除/换类型时自动提升。
- API（`/api/v1/ai-configs`）：`GET ""`、`POST ""`、`POST /models`（用未保存表单参数拉模型列表，Key 为空可回退到 `config_id` 已存 Key）、`GET/PATCH/DELETE /{id}`、`POST /{id}/default`、`POST /{id}/test`（文本发 16 token 测试；图片改为 list_models 校验）
- 输出时 Key 掩码 `sk-***1234`（`mask_secret`）。
- 前端：`/settings/ai`（`AIConfigPage.tsx`，517 行），`AIConfigSelect` 供各生成处选择（只列文本模型）。
- 限制：按用户维度配置，无系统级共享配置；无温度/top_p 等采样参数持久化（`temperature` 参数存在但调用方从不传）；图片生成未实现。

### F4 项目（小说）管理 — 完成
- 模型：`projects(id, user_id→users CASCADE, title ≤200, genre ≤100, premise(一句话设定), style(写作风格), status[planning|writing|completed], ts)`
- API（`/api/v1/projects`）：`GET ""`（附 `chapter_count/total_words` 聚合）、`POST ""`、`GET/PATCH/DELETE /{id}`、`GET /{id}/export?format=txt|md&chapter_ids=`
- 前端：`/projects`（`ProjectListPage.tsx` 卡片列表 + 新建弹窗）、`/projects/:pid/overview`（`OverviewPanel.tsx`：统计、编辑基本信息、删除、导出、进入向导）
- 限制：无封面（进行中任务 `10-23-project-cover`）、无目标字数/叙事视角/时代背景等结构化字段、无项目导入导出（JSON 备份）、无回收站。

### F5 世界观 — 完成（扁平条目）
- 模型：`world_settings(id, project_id CASCADE, title, content, sort, ts)`
- API：`/api/v1/projects/{pid}/world` 标准 CRUD（`GET/POST ""`、`GET/PATCH/DELETE /{item_id}`），由 `story.py:_make_router` 工厂生成
- AI：向导 `world` 生成 5–10 条，**覆盖**旧数据
- 前端：`/projects/:pid/world`（`WorldPanel.tsx` + `WizardModal`）
- 限制：只有 title/content 文本条目，无分类、无地点/势力/物品等结构化实体，无单条 AI 扩写。

### F6 角色 — 完成（文本卡片）
- 模型：`characters(id, project_id CASCADE, name ≤100, role_type ≤50, profile, relations_text, sort, ts)`
- API：`/api/v1/projects/{pid}/characters` 标准 CRUD
- AI：向导 `characters` 按 `character_count`（3–30）生成，**覆盖**旧数据
- 前端：`/projects/:pid/characters`（`CharactersPanel.tsx` 卡片 + 编辑弹窗 + `WizardModal`）
- 限制：关系为自由文本 `relations_text`，无关系表/关系图谱；无组织/门派实体；无年龄、性别、外貌等结构化字段；无角色状态随章节变化的追踪；无单个角色 AI 生成。

### F7 大纲 — 完成（分卷分章）
- 模型：`outlines(id, project_id CASCADE, volume_no, volume_title, chapter_no, title, summary, sort, ts)`；卷不是独立表，以 `volume_no/volume_title` 冗余在每行。
- API：`/api/v1/projects/{pid}/outlines` 标准 CRUD（排序 volume_no, chapter_no, sort, id）
- AI：向导 `outline` 参数 `volume_count`(1–20)、`chapters_per_volume`(1–100)；解析后章节号全书连续重编，**覆盖**旧数据
- 前端：`/projects/:pid/outlines`（`OutlinesPanel.tsx`：按卷分组表格 `utils/outline.ts:groupOutlines`、新增/编辑/删除、标记已建章、批量建章、`WizardModal`）
- 限制：无"大纲续写/追加"、无单章大纲展开为多章、无拖拽排序、无一对多（大纲→多章节）展开、无情节节点/伏笔字段。

### F8 创作向导 — 完成
- API：`POST /api/v1/projects/{pid}/wizard/{kind}`，`kind ∈ outline|characters|world`，SSE；请求体 `WizardRequest{ai_config_id, instruction≤2000, volume_count, chapters_per_volume, character_count}`
- 流程：`wizard_service.prepare_wizard_job` 读取项目信息 + 已有世界观/角色/大纲作为参考 → 流式输出 → `extract_json` + `normalize_*` 容错（支持多种键名、嵌套卷结构）→ 删除该类型旧数据后批量插入
- 前端：`/projects/:pid/wizard`（`WizardPage.tsx`）Steps：世界观 → 角色 → 大纲 → 完成（`POST chapters/from-outlines` 批量建章）；可跳过/上一步；`WizardGenerator` 流式展示原文 + `StoryPreview` 结构化预览
- 限制：只能整体覆盖、不能增量合并；同项目并发两次向导以后完成者为准（README 已知限制）；无"从一句话自动生成书名/简介/题材"的项目级向导步骤。

### F9 章节管理与 AI 生成/润色 — 完成
- 模型：`chapters(id, project_id CASCADE, outline_id→outlines SET NULL, chapter_no, title, content, status[draft|generating|done], word_count, ts)`；字数 = 非空白字符数（前后端一致 `count_words/countWords`）
- API：
  - `GET/POST /api/v1/projects/{pid}/chapters`、`POST .../chapters/from-outlines {outline_ids|null}`（跳过已关联大纲）、`GET/PATCH/DELETE .../chapters/{cid}`（PATCH 可单独把 generating 复位为 draft；生成中禁止改正文/标题，409）
  - `POST /api/v1/chapters/{cid}/generate` SSE，`GenerateChapterRequest{ai_config_id, instruction≤2000, target_words 300–20000(默认3000)}`
  - `POST /api/v1/chapters/{cid}/polish` SSE，`PolishChapterRequest{ai_config_id, instruction}`；正文为空 400
- 上下文：见 §4.1 `context_builder`
- 前端：`/projects/:pid/chapters`（`ChaptersPanel.tsx` 表格、新建、删除、跳转从大纲创建）、`/projects/:pid/chapters/:cid`（`ChapterEditorPage.tsx`）：标题 + TextArea 正文、Ctrl/⌘+S 保存、未保存/生成中离开拦截（`useBlocker` + beforeunload）、上一章/下一章、标记完成/草稿、"生成中"卡住复位；右侧 AI 助手（选配置、生成 Tab 目标字数+补充指令、润色 Tab 要求、流式输出自动滚动、可停止）；生成前自动保存脏数据；生成/润色完成即**整章覆盖并落库**
- 限制：生成结果直接覆盖正文，无版本历史/对比/回滚；无局部选段改写/扩写/续写；无章节摘要存储（前情提要是截取正文前 200 字）；无批量连续生成；无章节质量分析/评分；无伏笔/记忆抽取；无"重新生成时参考旧稿"。

### F10 导出 — 完成（TXT / Markdown）
- API：`GET /api/v1/projects/{pid}/export?format=txt|md&chapter_ids=1,2`（也支持重复参数）；排除 generating 与空正文章节；`Content-Disposition` RFC 5987 中文文件名（`export_service.py`）
- 前端：`ExportModal.tsx`（格式单选 + 章节勾选，不勾选导出全部）
- 限制：无 EPUB/DOCX/PDF；无项目数据（设定/角色/大纲）导出与导入。

### F11 运维与安全 — 完成
- `/healthz`；SPA 回退且 `/api/*` 不存在返回 JSON 404（`main.py:_mount_frontend`，`test_spa.py`）
- SSRF 防护：Base URL 解析后拒绝回环/内网/链路本地/保留/组播地址，禁止跟随重定向；`ALLOW_PRIVATE_AI_ENDPOINTS=true` 放开（`endpoint_guard.py`）
- API Key Fernet 加密，`ENCRYPTION_KEY` 更换后解密失败给出中文提示

### 明确未实现（README「已知限制」+ 代码核实）
伏笔管理、关系图谱、提示词工坊（提示词硬编码在 `app/ai/prompts/`，无用户自定义/模板表）、拆书、OAuth 登录、向量检索/RAG 记忆、EPUB 导出、图片生成/封面、文件上传、写作风格库、职业/境界体系、组织管理、章节版本、灵感模式、MCP 插件、用量统计、多语言 UI、移动端适配（未专门处理，仅 antd 栅格/Sider breakpoint）。

### 测试覆盖（`backend/tests/`，按 `def test` 计数）
admin 27、ai_config_params 15、auth 12、endpoint_guard 11、chapters 9、wizard 9、config_security 6、context_builder 6、ai_configs 5、ai_config_model_type 5、projects_and_story 4、export 3、migrations 3、spa 1。前端 vitest：`api/sse.test.ts`、`api/admin.test.ts`、`utils/*.test.ts`。

---

## 3. 数据模型 ER 概览

```
users (id PK, username UQ, password_hash, is_admin, is_active, token_version, ts)
 ├─< ai_configs (id PK, user_id FK CASCADE, name, provider, base_url, api_key_enc,
 │               model, model_type, is_default, thinking_level, context_window_k, max_output_k, ts)
 └─< projects   (id PK, user_id FK CASCADE, title, genre, premise, style, status, ts)
       ├─< world_settings (id PK, project_id FK CASCADE, title, content, sort, ts)
       ├─< characters     (id PK, project_id FK CASCADE, name, role_type, profile, relations_text, sort, ts)
       ├─< outlines       (id PK, project_id FK CASCADE, volume_no, volume_title, chapter_no, title, summary, sort, ts)
       │      └─o< chapters.outline_id (FK SET NULL，大纲删除/重生成后章节保留)
       └─< chapters       (id PK, project_id FK CASCADE, outline_id FK NULL, chapter_no, title,
                           content, status, word_count, ts)
```

要点：
- 共 7 张表；所有 `*_id` 外键建索引；无 ORM relationship。
- 卷没有独立表；角色关系没有独立表；章节与大纲是 0..1 对 0..1 的弱关联（`create_from_outlines` 防重复，但 DB 无唯一约束）。
- `chapter_no` 与 `outlines.chapter_no` 均无唯一约束。
- 向导重新生成 = `DELETE WHERE project_id` + 批量插入，因此大纲重生成后章节 `outline_id` 被置 NULL。

---

## 4. 扩展新功能的关键挂点

### 4.1 章节上下文组装 `backend/app/services/context_builder.py`
- 入口 `build_chapter_context(db, project, chapter, budget) -> str`：读取世界观、角色、大纲（全部）、本章之前（`chapter_no <` 当前）所有章节的 `(chapter_no, title, content)`，调用纯函数 `assemble_context(...)`。
- `ContextParts.render()` 段落顺序：`【作品信息】`（`format_project_info`）→ `【世界观】` → `【主要角色】` → `【当前卷大纲】`（`_current_volume_outlines`：按 outline_id 或最近章节号推断卷）→ `【前情提要（更早章节）】`（每章正文前 200 字 `SUMMARY_CHARS`）→ `【最近章节正文】`（最近 `RECENT_FULL_CHAPTERS=2` 章全文）。
- 超预算裁剪顺序：①从最早丢摘要 ②截最近章节正文开头（保留结尾）③依次截世界观、角色、大纲尾部 ④硬截断。
- 预算来源：`ai_config_service.context_char_budget(cfg, settings.context_char_budget)`，在 `generation_service.prepare_chapter_job` 中计算。
- 新增上下文来源（如伏笔、记忆、角色状态、真实章节摘要、RAG 检索结果）的方式：在 `ContextParts` 加字段 + `render()` 加段 + 在 `assemble_context` 裁剪序列中定位优先级 + `build_chapter_context` 查询数据；有 `tests/test_context_builder.py` 覆盖纯函数。
- 文本格式化函数集中在 `story_service.format_world / format_characters / format_outlines`，向导与章节共用。

### 4.2 提示词 `backend/app/ai/prompts/`
- 纯函数，输入纯文本参数，输出 `list[ChatMessage]`；`__init__.py` 统一导出。
- `common.py`：`NOVELIST_SYSTEM`（系统人设）、`JSON_ONLY_RULE`、`ProjectInfo` dataclass、`format_project_info()`。
- `chapter.py`：`build_chapter_messages(context, chapter_no, title, summary, target_words, style, instruction)`、`build_polish_messages(content, style, instruction)`、`PROSE_RULES`。
- `wizard.py`：`build_outline_messages / build_characters_messages / build_world_messages`，带 `_reference_block` / `_instruction_block`，JSON 输出格式写在提示词里。
- 新功能的提示词：新增 `prompts/<feature>.py` 并在 `__init__.py` 导出；JSON 类输出复用 `JSON_ONLY_RULE` + `app/ai/json_extract.extract_json`。当前无 DB 存储模板、无用户覆盖机制（"提示词工坊"需新增表与解析层）。

### 4.3 AI 调用
- 新的流式 AI 功能照搬两阶段模式：`prepare_xxx_job(db, *, user_id, ..., cipher, factory, ai_timeout, allow_private)` 内调用 `resolve_config(db, user_id, req.ai_config_id[, model_type])` + `build_provider(...)` + 组装 messages，返回 dataclass Job（含 `provider/model/messages/thinking/max_tokens`）；`run_xxx_job(job, sessionmaker)` 产出 `("delta"|"done"|"error", dict)`，失败分支 `AIProviderError → error {detail}`，其他异常 `logger.exception` + 通用中文错误。`Event` 类型从 `generation_service` 导入。
- 路由：依赖 `CurrentUser, DbDep, SettingsDep, CipherDep, FactoryDep, SessionmakerDep`，`job = await prepare(...)`；`await db.close()`；`return sse_response(run(job, sessionmaker))`，`response_class=StreamingResponse`。
- 新提供商：在 `app/ai/providers/` 新建 `LLMProvider` 子类并注册到 `_PROVIDERS`，同时更新 `entities.AI_PROVIDERS`、`schemas/ai_config.ProviderName` 与前端 `types.ts`。
- 新能力（图片生成、非流式、embedding）需在 `LLMProvider` 基类新增方法（当前基类只有 `generate_stream` 与 `list_models`）；`resolve_config` 已支持 `model_type="image"`。
- 测试用 Fake 工厂注入：`create_app(provider_factory=...)`（见 `tests/helpers.py`、`conftest.py`）。

### 4.4 新增数据表与 CRUD
- 模型加在 `backend/app/models/entities.py`（继承 `TimestampMixin, Base`，外键 `ondelete="CASCADE"` 指向 `projects.id` 并 `index=True`），在 `app/models/__init__.py` 导出；新增 Alembic 迁移 `backend/alembic/versions/0005_*.py`（`render_as_batch` 兼容 SQLite），并考虑 `test_migrations.py`。
- 项目子资源的简单 CRUD 可复用 `story_service.StoryKind` + `story.py:_make_router(segment, kind, Create, Update, Out, tag)`；schema 放 `app/schemas/`。注意 `update_owned` 会忽略值为 `None` 的字段（无法通过 PATCH 置空）。
- 所有 service 入口先 `get_owned_project(db, user_id, project_id)`。
- 路由注册：在 `backend/app/api/v1/__init__.py` 中 `api_router.include_router(...)`（统一前缀 `/api/v1`）。

### 4.5 前端新增页面
- 路由表 `frontend/src/router.tsx`：项目级页面加在 `projects/:pid` 的 `children` 中；全局页面加在 `/` 的 children（管理类用 `RequireAdmin` 包裹）。
- 项目左侧导航：`frontend/src/pages/project/ProjectLayout.tsx` 的 `NAV` 数组（key 即路由段）。全局顶栏菜单：`components/AppLayout.tsx`。
- API 封装：在 `src/api/<resource>.ts` 新增 `xxxApi = { list/get/create/update/remove }`（基于 `api.get/post/patch/del`），流式用 `runGeneration<TDone>(path, body, {onDelta, signal})`；类型补到 `src/api/types.ts`。
- 页面组件放 `src/pages/project/<Name>Panel.tsx`，数据用 `useLoader`，项目 id 用 `useProjectId`，流式用 `useStreamTask` + `StreamOutput`，模型选择用 `AIConfigSelect`，无配置提示用 `useHasAIConfig`；消息用 `App.useApp()` 的 `message/modal`。
- 若向导要新增 kind：后端 `wizard.py` 的 `Literal`、`wizard_service._KIND_SPEC` 与 `normalize_*`、前端 `WizardKind/WizardItemMap/WIZARD_KIND_LABELS`、`WizardPage.STEPS`、`StoryPreview` 需同步。

### 4.6 相关规范
- `.trellis/spec/backend/index.md` — 后端规范索引；含 SSE 流式契约（`error-handling.md`）、SSRF（`quality-guidelines.md`）、认证密钥（`quality-guidelines.md`）、管理员与 token_version（`auth-admin.md`）、数据库（`database-guidelines.md`）
- `.trellis/spec/frontend/index.md` — 前端规范索引；API 客户端契约（`type-safety.md`）、SSE 客户端解析器（`hook-guidelines.md`）、store 约定（`state-management.md`）
- 文档语言规则：spec 全部简体中文，标识符保持原样。

### 4.7 并行进行中的任务：`.trellis/tasks/10-23-project-cover/prd.md`
- 目标：项目封面，支持上传（JPG/PNG/WebP，≤5MB 建议）与使用"图片模型"AI 生成；R1 0/1 张封面、R4 可删除、R5 仅本人可访问；概览页展示，列表卡片是否展示待定。
- PRD 已确认：`Project` 无封面字段；无任何图片生成实现与文件上传/存储（无 `python-multipart`、compose app 无数据卷）；鉴权 cookie 同源，`<img src="/api/v1/...">` 可直接带鉴权；Anthropic 无图片生成能力；下一版迁移 `0005_*`。
- 状态：Requirements 草案、Acceptance Criteria 为 TBD、Open Questions（AI 生成交互方式、列表是否展示封面）。与本任务存在重叠：迁移版本号、`LLMProvider` 图片能力、文件存储方案都会被两者共同依赖。

---

## Caveats / Not Found

- 未逐行阅读 `AIConfigPage.tsx`、`AdminUsersPage.tsx`、`CharactersPanel.tsx`、`WorldPanel.tsx`、`OutlinesPanel.tsx` 的全部细节，仅确认其主要交互（列表/编辑弹窗/删除/AI 生成弹窗）。
- 未打开 `anthropic.py`、`gemini.py` 全文，仅确认思考参数映射与 `list_models` 存在。
- 前端 i18n、暗色主题、移动端专门适配：未发现相关代码。
- `.trellis/tasks/10-22-ai-novel-platform/design.md` 被 backend spec 引用为设计意图来源，本次未读取（可能已归档）。
