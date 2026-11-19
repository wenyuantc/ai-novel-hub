# Research: 浏览器验收环境与以往做法

- **Query**: TODOS 第 19 项七组界面要做浏览器验收，查清本仓库以往的浏览器验收是怎么搭环境、接假模型、备账号、驱动浏览器的，以及第 4 项「站点策略阻挡」的原始记录与窄屏验收做法
- **Scope**: internal（仓库文件 + 本机 `/private/tmp` 下以往验收留下的临时目录）
- **Date**: 2026-11-18

路径均相对仓库根 `/Users/wenyuantc/IdeaProjects/my/aiNovel`；归档任务前缀 `A = .trellis/tasks/archive/2026-11`。本次调研未读取任何 `.env`、凭据、cookie jar、`postgres-url`、`env.sh` 文件；`.env.example` 只取了变量名。

## Findings

### 0. 结论速览

| 问题 | 结论 |
|---|---|
| 仓库内有无可复用的验收脚本 | **没有**。`git ls-files` 中无 Playwright/e2e/seed/验收启动脚本；`frontend/package.json:6-12` 只有 dev/build/lint/preview/test，依赖里没有 Playwright（`:13-47`）。历次验收的脚本都放在系统临时目录，不入库 |
| 环境形态 | 每次现搭：临时目录 + 临时 SQLite + 现场生成的密钥 + 随机/专用端口 + 合成账号，不碰用户的 8000/5173 实例 |
| 假模型 | 有两种做法并存：（甲）进程内注入测试类 `FakeLLM`；（乙）本地 OpenAI 兼容 HTTP 假服务 + 在「模型配置」里配成模型。11-13 以后的任务基本用（乙） |
| 账号 | 注册接口 `POST /api/v1/auth/register` 由种子脚本批量建；管理员用 `ADMIN_USERNAME`/`ADMIN_PASSWORD` 启动引导；协作角色经协作接口或界面邀请 |
| 浏览器驱动 | 11-05 前后用 Codex 的 IAB（内置浏览器）+ CUA；11-17 起的任务用 Playwright 驱动本机 Chrome（`channel: 'chrome'`） |

### 1. 隔离环境怎么搭

#### 1.1 文档给出的基线（README）

| 依据 | 内容 |
|---|---|
| `README.md:192-198` | SQLite：`DATABASE_URL=sqlite+aiosqlite:///./dev.db`，驱动 `aiosqlite` 已在运行依赖里 |
| `README.md:209-215` | 后端在 `backend/` 下：`uv run alembic upgrade head`，再 `uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000`；本地启动不会自动迁移 |
| `README.md:219-233` | 前端：`cd frontend && npm ci && npm run dev`，默认 5173；`VITE_BACKEND_URL=http://127.0.0.1:9000 npm run dev` 可改代理目标 |
| `frontend/vite.config.ts:5,9-13` | `VITE_BACKEND_URL` 默认 `http://localhost:8000`，dev 端口 5173，`/api` 走代理 |
| `README.md:188` | 本地后端从**当前工作目录**读 `.env`（`backend/app/core/config.py:23` `env_file=".env"`），所以验收时都用进程环境变量覆盖，或干脆不从 `backend/` 读 `.env` |
| `README.md:552-590` | 环境变量表；变量名来源是 `.env.example` 与 `backend/.env.example` |

验收用到的变量名（来自 `backend/.env.example:4-58` 与 `backend/app/core/config.py:26-62`）：`DATABASE_URL`、`JWT_SECRET`（≥16 字符）、`ENCRYPTION_KEY`（合法 Fernet）、`REGISTRATION_ENABLED`、`ADMIN_USERNAME`、`ADMIN_PASSWORD`、`COOKIE_SECURE`、`ALLOW_PRIVATE_AI_ENDPOINTS`、`AUTO_ANALYZE_ENABLED`、`BACKUP_DIR`、`FRONTEND_DIST_DIR`（对应 `config.py:59 frontend_dist_dir`）。

#### 1.2 以往各次验收的实际环境

| 任务 | 依据 | 环境 |
|---|---|---|
| 一致性检查（11-17） | `A/11-17-consistency-check-issues/browser-verification.md:9-14` | 临时目录 `/private/tmp/ainovel-consistency-qa-1S45`；假模型 `127.0.0.1:61175/v1`、后端 `:61176`、前端 Vite dev `:61177`（`VITE_BACKEND_URL` 指后端）；后端 `--app-dir backend` 启动，配置全部来自现场生成的环境变量（临时 SQLite、随机 JWT/Fernet、`ALLOW_PRIVATE_AI_ENDPOINTS=true`）；alembic 从空库升到 `0058`；视口 1280×900 与 375×812 |
| 自动保存（11-18） | `A/11-17-autosave-draft-recovery/research/verification.md:3` | Chrome 157 + Playwright 1.64、**生产构建**、隔离 FastAPI、临时 SQLite、仓库 FakeLLM |
| 全书搜索（11-18） | `A/11-18-book-search-batch-revision/research/verification.md:3,19` | 正式前端构建 + FastAPI + 经 Alembic 迁移的临时 SQLite；桌面 Chrome 157，窄屏用视口模拟 |
| 连载工作台（11-18） | `A/11-18-serialization-workbench/implement.md:43` | 生产前端构建 + 独立 SQLite 服务，Chrome 1440×960 与 390×844 |
| AI 用量（11-18） | `A/11-18-ai-usage-cost-management/implement.md:17,42,51,59,70,82,91` | 隔离 Chrome + 「本地假上游／本地模型服务／本地模型替身」+ 隔离数据库 |
| 协作者（11-13、11-15） | `A/11-09-novel-collaborators/implement.md:1100,2298` | `/tmp/e5g_browser`：隔离 SQLite、本地假 OpenAI 兼容服务 `fake_ai.py`、后端 `:9137`、Vite `:5187`；启动配置当时写在 `.claude/launch.json` |
| 自定义分类（11-06） | `A/11-05-custom-setting-fields-categories/research/runtime-environment.md:7-13`、`research/category-browser-verification.md:7-10` | 系统临时目录 ROOT，临时后端 `8014`、前端 `5179`；`server.py` 只用本 ROOT 的 `test.db`、FakeLLM 与测试密钥，不读产品 `.env` |
| 修改影响追踪 | `A/11-17-edit-impact-tracking/implement.md:33`、`check-report.md:10,13` | 计划「隔离数据 + 本地假模型」，实际**未做**浏览器验收 |

#### 1.3 两种服务形态（本机临时目录中现存的脚本，非仓库文件）

形态一：**生产构建 + 单端口**。先 `npm run build`，后端通过 `FRONTEND_DIST_DIR` 指向 `frontend/dist`，由 FastAPI 同源提供页面和 API。例：`/private/tmp/ainovel-autosave-verify.cgJy2zrF/serve_autosave.py:11-16,50-59`（绑定 `127.0.0.1:0` 取随机端口并打印 `TEST_ORIGIN=`）。当前 `frontend/dist/` 存在（含 `index.html`），但是否对应最新代码未核对。

形态二：**后端 + Vite dev 双端口**。例：一致性检查（`browser-verification.md:10`）、协作者（`implement.md:1100`）。

启动脚本样例（均在 `backend/` 下以 `uv run python <脚本>` 之类方式运行，因为要 `import app`/`tests.helpers`；确切调用命令未在记录里写明）：

- `/private/tmp/ainovel-book-revision.d5q2rg9d/serve.py:10-20`：组装环境变量（随机名 SQLite、`BACKUP_DIR`、随机 `JWT_SECRET`、`Fernet.generate_key()`、空 `ADMIN_*`、`REGISTRATION_ENABLED=true`、`COOKIE_SECURE=false`、`AUTO_ANALYZE_ENABLED=false`）→ 取空闲端口写入 `port` 文件 → `python -m alembic upgrade head` → `python -m uvicorn app.main:app --host 127.0.0.1 --port <port>`。
- `/private/tmp/ainovel-multi-offpeak.RcLuoN/server.py:17-83`（`ainovel-cache-ui.4H6PTE/server.py`、`ainovel-usage-smoke.0eOLnA/server.py` 同构）：同一进程里先 `alembic` 升级，再用两个 `uvicorn.Server` 分别起应用和假模型，端口写入 `ports.json`；设置了 `ALLOW_PRIVATE_AI_ENDPOINTS=true`、`AUTO_ANALYZE_ENABLED=true`、`ADMIN_USERNAME`/`ADMIN_PASSWORD`。

注意：`serve_autosave.py:52-53` 用 `Base.metadata.create_all` 建表；其他几份用 alembic。规范对此的说法见 `A/../2026-10/10-30-admin-recycle-bin/research/runtime-verification.md:9`（`create_all` 不当作迁移验收）。

### 2. 「本地假模型 / FakeLLM」的接入

仓库里没有独立可启动的假模型服务。以往有两种接法：

**（甲）进程内注入 `FakeLLM`（配置项式，不走 HTTP）**

- 定义：`backend/tests/helpers.py:36`（`class FakeLLM`，`push()` 预置文本、`push_tools()` 预置工具事件、`calls` 记录调用）。
- 注入点：`backend/app/main.py:101-103` `create_app(settings, provider_factory=...)`，`main.py:160` `app.state.provider_factory = provider_factory or create_provider`。
- 自动化用法：`backend/tests/conftest.py:52-66,107`。
- 浏览器验收用法：`/private/tmp/ainovel-autosave-verify.cgJy2zrF/serve_autosave.py:23-25,51`（`FakeLLM()` 预置 24 段文本后传给 `create_app`）。此外还需按 conftest 的方式放行章节发送门禁：`serve_autosave.py:42-46` 覆写了 `endpoint_guard._resolve`、`ai_dispatch._chapter_factory_allowed`/`_chapter_provider_allowed`、`collaboration_ai_guard._trusted_factory`/`_trusted_provider`（对应 `conftest.py:57-66`）。
- 这种方式下界面仍需有一条模型配置记录；`helpers.py:285-296` 的 `create_ai_config` 用的是 `openai_compat` + 占位 Base URL，实际请求被 FakeLLM 接管。
- `backend/app/services/ai_dispatch.py:631-640` 说明注入的 FakeLLM 不发 HTTP、但仍执行发送前检查。
- 局限（记录原文）：预设输出是队列，耗尽即无输出；`A/11-09-novel-collaborators/implement.md:1137` 记了「假模型非 JSON 导致解析失败，属测试桩限制」。

**（乙）本地 OpenAI 兼容 HTTP 假服务（可启动的本地服务）**

- 一致性检查：`/private/tmp/ainovel-consistency-qa-1S45/fake_model.py`（标准库 `ThreadingHTTPServer`，`:183-184` 以 `python fake_model.py <port>` 启动；`:21-26` 按提示词关键词分流；所有请求体写入 `requests.jsonl`；存在 `delay` 文件时抽取请求延迟）。规则说明见 `browser-verification.md:13`。
- AI 用量：`/private/tmp/ainovel-multi-offpeak.RcLuoN/server.py:36-64`（FastAPI 实现 `POST /v1/chat/completions`，SSE 流式，带 `usage` 与 `cached_tokens`，第一次正文生成故意返回 503；分析请求返回 `tests.helpers.analysis_json`）。
- 协作者：`/tmp/e5g_browser/fake_ai.py`（逐块慢速流式、流式工具调用脚本、`/calls` 计数，`implement.md:1100,2298`）——该目录现已不存在。
- 在应用里配成模型：登录后在「模型配置」新增 `openai_compat`、Base URL 填 `http://127.0.0.1:<端口>/v1`、设为默认（`browser-verification.md:12`「文本模型在界面『模型配置』中新增并设为默认」；配置入口说明 `README.md:239`）。
- 必须的开关：后端 `ALLOW_PRIVATE_AI_ENDPOINTS=true`，否则回环地址被 SSRF 防护拒绝（`README.md:271-272`；`.trellis/spec/backend/quality-guidelines.md:260,284,293`）。

### 3. 多账号怎么建

仓库里没有种子脚本或管理命令，全部走接口：

| 角色 | 做法 | 依据 |
|---|---|---|
| 普通账号（作者／待邀请人） | `POST /api/v1/auth/register`，体为 `{username, password}`；需 `REGISTRATION_ENABLED=true` | `backend/app/api/v1/auth.py:32-37`；测试辅助 `backend/tests/helpers.py:234-237`；种子脚本样例 `/private/tmp/ainovel-consistency-qa-1S45/seed.py:4-10`（httpx 逐个注册，已存在则登录） |
| 查看者／编辑者 | 作者 `POST /api/v1/projects/{id}/collaborators` 邀请 → 被邀请人 `POST /api/v1/collaboration/invitations/{scope}/{id}/accept`；角色与「允许作者模型」可 `PATCH .../collaborators/{user_id}` 实时改 | `backend/app/api/v1/collaboration.py:30,38,84`；`A/11-09-novel-collaborators/implement.md:2298`（「成员权限由作者经 PATCH collaborators 实时修改」）；界面路径 `README.md:477` |
| 系列设定协作者 | `POST /api/v1/series/{sid}/collaborators` | `backend/app/api/v1/collaboration.py:64` |
| 超级管理员 | 启动前同时设 `ADMIN_USERNAME` + `ADMIN_PASSWORD`，服务启动时引导创建 | `README.md:650-654`；`backend/app/main.py:116`；`backend/app/services/auth_service.py:143`；样例 `/private/tmp/ainovel-multi-offpeak.RcLuoN/server.py:25-26` |
| 管理员建号 | `POST /api/v1/admin/users`（需用户创建权限） | `backend/app/api/v1/admin.py:56-58` |
| 只读管理账号（仅 `projects.read`） | 超管建自定义角色并分配：`POST /api/v1/admin/roles`、`PUT /api/v1/admin/users/{id}/roles` | `backend/app/api/v1/admin_roles.py:35,56`；先例 `fields_reader`：`A/11-05-custom-setting-fields-categories/research/runtime-environment.md:12`、`implementation-log.md:62`（由 `seed_reader.py` 创建，`category-browser-verification.md:9`） |
| 管理员兼协作者 | 同一账号既是管理账号又被邀请为协作者；语义见 `README.md:497,677` | 早期快照抽查记录 `A/11-09-novel-collaborators/research/phase-g-integration.md:56-60` |

以往账号命名：`author_qa/editor_qa/viewer_qa/outsider_qa`（`browser-verification.md:12`）、`e5g_owner/e5g_editor/e5g_viewer`（`implement.md:1100`）、`fields_author/fields_reader`（`runtime-environment.md:12`）。

「只读账号」在历史记录里有两种含义，第 19 项各条应分别对照：第 4 项分类页用的是**仅 `projects.read` 的管理账号** `fields_reader`（`category-browser-verification.md:46`）；第 9 项笔记的「只读账号界面」原计划条目见 `A/11-08-snippet-favorite-pin-sidebyside/implement.md:43`（AC6），该文件未写明用哪一种只读身份——**未查实**。

`implementation-log.md:62` 记录：创建只读账号并授予 `projects.read` 当时先被自动审批拒绝，用户明确批准后才在隔离环境执行。

### 4. 浏览器驱动工具与可复用资产

| 时期 | 工具 | 依据 |
|---|---|---|
| 11-02（RBAC） | 「Chrome 独立会话」，可对 `**/api/v1/workshop/items**` 注入 500 | `A/10-30-admin-rbac-quotas/research/browser-preflight.md:9,19` |
| 11-05/06（字段与分类） | Codex 的 IAB（内置浏览器）+ CUA，截图存 `~/.codex/visualizations/...` | `category-browser-verification.md:10`、`runtime-environment.md:13` |
| 11-13/15（协作者） | 主会话浏览器验证，启动配置写在 `.claude/launch.json`（现已不存在） | `implement.md:1100` |
| 11-17（一致性检查） | 「Playwright 驱动的真实浏览器」；Playwright 工具在仓库根留下未跟踪的 `.playwright-mcp/`（当时未被 `.gitignore` 覆盖；现在仓库根无此目录） | `browser-verification.md:3,62` |
| 11-18（自动保存、搜索、用量、连载） | Node 脚本 + Playwright + 本机 Chrome | `A/11-17-autosave-draft-recovery/research/verification.md:3` 及下列脚本 |

Playwright 脚本的实际写法（临时目录现存）：

- `chromium.launch({ channel: 'chrome', headless: true })`，`browser.newContext({ viewport })`，中途 `page.setViewportSize({ width: 390, height: 780 })`：`/private/tmp/ainovel-multi-offpeak.RcLuoN/browser.cjs:5,45,47,119`。
- 持久化配置目录（验证关浏览器重开）：`/private/tmp/ainovel-autosave-verify.cgJy2zrF/browser_harness.cjs:70-74`（`launchPersistentContext` + `channel: 'chrome'`）。
- 多身份用相互独立的浏览器上下文（`browser-verification.md:76`）。
- 全接口 mock 的做法（不连后端）：`/private/tmp/ainovel-ai-usage-browser/verify.cjs:29-40`（`page.route('**/api/v1/**', ...)` + `route.fulfill`）。
- Playwright 来源：脚本 `require('/Users/wenyuantc/.npm/_npx/e41f203b7505f1fb/node_modules/playwright')`（npx 缓存，现存），或 `/private/tmp/ainovel-ai-usage-browser/node_modules/playwright`（1.65.0）。`~/Library/Caches/ms-playwright/` 下有 chromium-1248、firefox、webkit 及 mcp-chrome-*；`/Applications/Google Chrome.app` 存在。全局 `playwright` 命令不在 PATH。

本机现存、可参考的临时目录（**不在仓库内，系统清理临时目录后会消失**）：

| 目录 | 内容 |
|---|---|
| `/private/tmp/ainovel-consistency-qa-1S45/` | `fake_model.py`、`seed.py`、`edit.sh`、`qa.db`、`requests.jsonl` 等（另有 `env.sh`、`*.jar`，未读取） |
| `/private/tmp/ainovel-autosave-verify.cgJy2zrF/` | `serve_autosave.py`、`browser_harness.cjs`、`verify_autosave*.cjs` |
| `/private/tmp/ainovel-book-revision.d5q2rg9d/` | `serve.py`、`fixture.cjs`、`accept.cjs`、`cross-page.cjs`、`layout.cjs`、`rich.cjs` |
| `/private/tmp/ainovel-multi-offpeak.RcLuoN/`、`ainovel-cache-ui.4H6PTE/`、`ainovel-usage-smoke.0eOLnA/` | `server.py`（应用 + 假模型同进程）、`browser.cjs` |
| `/private/tmp/ainovel-ai-usage-browser/` | `verify.cjs`、`node_modules/playwright` |

已不存在：`/tmp/e5g_browser`（协作者两轮验证环境，含 `fake_ai.py`）、`.claude/launch.json`。11-05 的分类环境 ROOT 在 `/var/folders/.../T/ainovel-categories-browser-1onr9vwt`（`runtime-environment.md:7`），本次未核对是否仍在。

### 5. 第 4 项「站点策略阻挡」的原始记录

原始记录：`A/11-05-custom-setting-fields-categories/research/category-browser-verification.md:44-49`，摘要：

- 工具是「本任务 IAB，操作均经 CUA」（同文件 `:10`），即 Codex 内置浏览器 + 计算机操作代理；被拦的站点是临时前端 `5179`。
- `:46` 只读分类页（用 `fields_reader` 登录后看控件状态）**尚未执行**。
- `:47` 读取失败→同 revision 重试**尚未执行**：「曾停止 8014 准备故障场景，但浏览器动作在请求发出前被站点权限拒绝」。
- `:48` 「首次为『用户拒绝访问 5179』；用户两次明确要求继续且确认已解除拒绝后，工具仍返回『已保存权限或管理策略拒绝』。每次均停止 CUA 并报告，没有切换端口／浏览器、raw CDP、脚本抓页面或其他绕过。」
- `:49` 2026-11-06 用户要求提交收尾，两项原样保留为人工未验。

旁证：`runtime-environment.md:9,13`（「5179 保存权限／管理策略仍拒绝，用户重新授权并确认解除后工具仍拒绝」）；`research/closeout.md:21`；`research/commit-plan.md:5`；`implementation-log.md:156`；`A/11-05-setting-auto-categories/implement.md:3,112`；`A/11-05-custom-setting-fields-categories/implement.md:3,81`；`.trellis/workspace/aiNovel/journal-1.md:1250,1273`。`A/11-05-typed-setting-fields/` 下只有 prd/design/implement，字段阶段的只读 UI 当时已在旧隔离库验过（`runtime-environment.md:12`），不涉及这次阻挡。

也就是说，「站点策略阻挡」指的是**驱动浏览器的工具（IAB/CUA）对 `localhost:5179` 这个站点的访问权限被拒**，拒绝发生在页面请求发出之前；不是应用自身的 CSP/CORS，也不是产品缺陷。

现在是否有别的路径（只陈述已有事实）：

- 11-17 起的验收改用 Playwright 驱动本机 Chrome 访问 `127.0.0.1` 随机端口，相关记录（`browser-verification.md`、两份 `verification.md`、连载与用量的 `implement.md`）里没有再出现站点权限拒绝。
- 「读取失败」不必停后端：以往已用 Playwright 路由拦截制造失败，例如 `browser-preflight.md:19`（对指定接口「可撤销注入 500」）、`verification.md:25,32`（丢弃响应、模拟 404）、`verify.cjs:29-40`（`page.route`）。
- IAB/CUA 那条站点权限现在是否仍被拒：**未查实**（本次未调用该工具）。
- 当时会话明确选择「不切换端口／浏览器绕过」（`:48`）；本任务 PRD 把这两项列为待验（`TODOS.md:256`）。换工具执行是否需要用户另行确认，记录里没有结论。

### 6. 窄屏／移动端验收

断点定义：

| 位置 | 内容 |
|---|---|
| `frontend/src/hooks/useIsMobile.ts:4` | `export const MOBILE_BREAKPOINT = 768` |
| `frontend/src/hooks/useIsMobile.ts:10-13` | `useIsMobile()` = antd `Grid.useBreakpoint()` 的 `screens.md === false`，即视口 < 768px；首帧未算出时按桌面 |
| `frontend/src/hooks/useIsMobile.ts:16-18` | `useModalWidth`：移动端 `calc(100vw - 24px)` |
| `.trellis/spec/frontend/component-guidelines.md:411` 起 | 断点 768、禁止直接读 `window.innerWidth`、项目页内容区宽度不等于视口宽度（用 `AutoGrid`/`splitRow`/`useElementWidth`） |
| `.trellis/spec/frontend/component-guidelines.md:421` | 验收约定：「Playwright 在 375px 下对一级页面断言 `document.documentElement.scrollWidth <= clientWidth`」 |
| CSS 媒体查询 | 多处 `@media (max-width: 767px)`（如 `frontend/src/components/aiUsage/aiUsage.css:24`、`pages/AdminTasksPage.css:14,19`）；`components/AppLayout.css:50,51,63` 另有 1100／1000／360 |
| 章节编辑页 | `frontend/src/pages/project/ChapterEditorPage.tsx:85,167` 使用 `useIsMobile()`（第 9 项笔记栏移动端抽屉、TODOS 第 23 项跨断点重建都与此相关） |

以往用的视口：

| 任务 | 桌面 | 窄屏 | 依据 |
|---|---|---|---|
| 一致性检查 | 1280×900 | 375×812 | `browser-verification.md:14` |
| 自动保存 | 1350×900（harness 默认） | 375px，明暗主题 | `verification.md:34`；`browser_harness.cjs:74` |
| 全书搜索 | 1365px | 375px（375×812） | `verification.md:13`；`accept.cjs:76` |
| 连载工作台 | 1440×960 | 390×844 | `implement.md:43` |
| AI 用量 | 1100×860 | 390×780 | `browser.cjs:47,119` |
| 自定义分类 | — | 375px，核对 `innerWidth`/`scrollWidth`，「未用桌面缩放截图冒充移动视口」 | `category-browser-verification.md:28,58` |

窄屏都是浏览器视口模拟（`verification.md:19`「窄屏使用浏览器视口模拟」），真机未测（`browser-verification.md:77`、`verification.md:38`）。常规检查项：无横向滚动、抽屉可操作、明暗主题。

### Related Specs

- `.trellis/spec/frontend/component-guidelines.md:411-421` — 断点、移动适配与 375px 验收断言
- `.trellis/spec/frontend/component-guidelines.md:159` — 静态 UI 测试不能代替真实浏览器
- `.trellis/spec/backend/quality-guidelines.md:260-312` — `ALLOW_PRIVATE_AI_ENDPOINTS` 与 SSRF 防护
- `.trellis/spec/backend/quality-guidelines.md:502` — 测试用 `tmp_path`、`FakeLLM`、`fake_dns`，不访问网络
- `.trellis/spec/backend/ai-usage.md:57` — 用隔离数据库和本地模型替身做浏览器验证
- `.trellis/spec/guides/` 下没有专门的浏览器验收指南（检索「浏览器／验收／Playwright」无命中）

### 第 19 项各条对应的原始未验记录

| TODOS 行 | 原始记录 |
|---|---|
| `TODOS.md:252` 修改影响追踪 | `A/11-17-edit-impact-tracking/check-report.md:10,13`；计划 `implement.md:33`（AC-P1–AC-P4） |
| `TODOS.md:253` 一致性检查 3 个缺陷复验 | `A/11-17-consistency-check-issues/check-report.md:16-20`（证据高亮、协作者进度流兜底、键盘焦点）；复现方式 `browser-verification.md:44-45` |
| `TODOS.md:254` 系列设定协作者、管理员兼协作者 | `A/11-09-novel-collaborators/implement.md:87,2375`；早期抽查 `research/phase-g-integration.md:56-60` |
| `TODOS.md:255` 智能体前端 E4i-h | `A/11-09-novel-collaborators/implement.md:2316-2335`（问题 1、2 的根因与改动）、`:2334` 待决（留意是否出现「作品权限已变化」或网络错误提示）；当时的假服务工具脚本描述 `:2298` |
| `TODOS.md:256` 自定义分类两项 | 见第 5 节 |
| `TODOS.md:257` 笔记只读与移动端抽屉 | `TODOS.md:116`；计划 `A/11-08-snippet-favorite-pin-sidebyside/implement.md:40-43` |
| `TODOS.md:258` Skill 输入表单 | `TODOS.md:128`；计划 `A/11-08-prompt-inputs/implement.md:23`（六个入口各走一次，FakeLLM） |

## Caveats / Not Found

- **未查实**：各临时启动脚本当时的确切调用命令行（记录只写了「从临时目录启动、`--app-dir backend`」，未给完整命令）。
- **未查实**：IAB/CUA 对本地站点的权限拒绝现在是否仍然存在。
- **未查实**：第 9 项笔记「只读账号」当时计划用查看者协作者还是仅 `projects.read` 的管理账号。
- **未查实**：`frontend/dist/` 是否由当前 HEAD 构建；11-05 分类环境的临时 ROOT 与 `seed*.py` 是否仍在。
- **未查实**：协作者 11-13/15 两轮具体用的浏览器驱动工具名称（记录只写「主会话浏览器验证」和 `.claude/launch.json`）。
- 协作者环境 `/tmp/e5g_browser`（含支持流式工具调用的 `fake_ai.py`）已不存在；智能体相关验收若走 HTTP 假服务，仓库和现存临时目录里都没有带工具调用的现成脚本。进程内 `FakeLLM.push_tools`（`backend/tests/helpers.py:36` 起）支持工具事件。
- `/private/tmp` 下的脚本不属于仓库，未经版本管理，内容可能与当前代码的接口不完全一致（例如 `seed.py` 写于迁移 `0058` 时期，当前迁移头已到 `0064`）。
- 本次未启动任何服务、未运行任何验收脚本，以上均为阅读记录与文件所得。
