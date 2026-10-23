# 目录结构

> 本项目后端代码的组织方式。

---

## 概述

后端是位于 `backend/app/` 下的单个 FastAPI 应用，使用 `uv` 管理
（`backend/pyproject.toml`，`[tool.uv] package = false`，Python `>=3.12,<3.13`）。
它遵循严格的分层：

```
HTTP route (app/api/v1)  ->  service (app/services)  ->  ORM model (app/models)
        |                         |
   Pydantic schema           AI layer (app/ai: providers + prompts)
   (app/schemas)
```

- 路由保持轻薄：通过 Pydantic schema 解析输入，从 `app/core/deps.py` 解析依赖，
  调用**一个** service 函数，然后返回结果。
- 所有业务查询以及所有 `user_id` 隔离都放在 `app/services/` 中。
- `app/ai/` 对 HTTP 和数据库一无所知；它对外提供 `LLMProvider` 抽象、
  prompt 构建器以及 SSRF 防护。

应用由工厂函数创建，即 `backend/app/main.py` 中的 `create_app(settings, provider_factory)`，
它将共享对象（engine、sessionmaker、cipher、provider factory）存放在 `app.state` 上，
以便测试注入假的 LLM。

---

## 目录布局

```
backend/
├── pyproject.toml            # 依赖、pytest + ruff 配置
├── alembic.ini
├── alembic/
│   ├── env.py                # 异步 migration，render_as_batch=True
│   └── versions/0001_initial_schema.py
├── app/
│   ├── main.py               # create_app()、错误处理器、SPA 回退、/healthz
│   ├── api/
│   │   ├── sse.py            # format_sse()、sse_response() —— SSE 帧封装
│   │   └── v1/
│   │       ├── __init__.py   # api_router = APIRouter(prefix="/api/v1")
│   │       ├── auth.py       # /auth/register|login|logout|me|config
│   │       ├── ai_configs.py # /ai-configs CRUD、/{id}/default、/{id}/test
│   │       ├── projects.py   # /projects CRUD、/{id}/export
│   │       ├── story.py      # /projects/{id}/world|characters|outlines（路由工厂）
│   │       ├── chapters.py   # 章节 CRUD、/chapters/{id}/generate|polish（创建后台任务，202）
│   │       ├── tasks.py      # /tasks 列表 / 详情 / output / stream（SSE 订阅）/ cancel / retry / 删除 / 清理
│   │       └── wizard.py     # /projects/{id}/wizard/{outline|characters|world}（创建后台任务，202）
│   ├── core/
│   │   ├── config.py         # Settings（pydantic-settings）、fail-fast 校验器
│   │   ├── database.py       # create_engine()、create_sessionmaker()
│   │   ├── deps.py           # DbDep、CurrentUser、CipherDep、FactoryDep、...
│   │   ├── errors.py         # AppError 层级 + register_error_handlers()
│   │   └── security.py       # bcrypt、JWT、SecretCipher（Fernet）、mask_secret()
│   ├── models/
│   │   ├── base.py           # Base（命名约定）、TimestampMixin
│   │   └── entities.py       # User、AIConfig、Project、WorldSetting、Character、Outline、Chapter
│   ├── schemas/              # 按领域划分的 Pydantic 请求/响应模型
│   │   ├── auth.py  ai_config.py  project.py  story.py  chapter.py
│   ├── services/
│   │   ├── auth_service.py        ai_config_service.py   project_service.py
│   │   ├── story_service.py       chapter_service.py     export_service.py
│   │   ├── context_builder.py     # 带字符预算的章节 prompt 上下文
│   │   ├── generation_service.py  # 章节生成/润色（validate_and_claim + build_chapter_job + run）
│   │   ├── wizard_service.py      # 大纲/角色/世界观（validate + build + run）+ JSON 规范化
│   │   ├── task_service.py        # 后台任务 API 入口：创建 / 列表 / 取消 / 重试 / 删除 / 清理 / 保留裁剪
│   │   └── task_engine/           # 统一持久化后台任务引擎（单进程，数据库即队列）
│   │       ├── engine.py          # TaskEngine：start/stop/recover、调度循环、_run/_finalize、取消、订阅
│   │       ├── hub.py             # TaskHub：已输出文本缓冲 + 订阅者队列（快照与注册同一同步临界区）
│   │       ├── registry.py        # TaskHandler 协议、TaskContext、TaskSpec、register/get_handler
│   │       └── handlers/          # 导入即注册：chapter.py（chapter.*）、wizard.py（wizard.*）
│   └── ai/
│       ├── endpoint_guard.py # 针对用户提供的 Base URL 的 SSRF 防护
│       ├── json_extract.py   # 从模型输出中容错提取 JSON
│       ├── providers/
│       │   ├── __init__.py   # create_provider()、ProviderFactory 类型
│       │   ├── base.py       # LLMProvider 抽象基类、AIProviderError、describe_error()
│       │   ├── openai_compat.py  anthropic.py  gemini.py
│       └── prompts/
│           ├── __init__.py   common.py   chapter.py   wizard.py
└── tests/
    ├── conftest.py           # app/client fixture、autouse 的 fake_dns
    ├── helpers.py            # FakeLLM、make_settings、register、parse_sse
    └── test_*.py
```

---

## 模块组织

**规则：路由绝不执行 ORM 查询；所有业务查询都经由 service。**
路由模块从 `app.core.deps`、`app.schemas.*` 和 `app.services.*` 导入。
`app/api/v1/` 中对 `app.models` 的导入仅用于返回类型注解
（`auth.py` 中的 `User`、`chapters.py` 中的 `Chapter`）—— 路由中没有 `select()` / `db.get()`。

典型路由（`backend/app/api/v1/chapters.py`）：

```python
@router.patch("/projects/{project_id}/chapters/{chapter_id}", response_model=ChapterOut)
async def update_chapter(
    project_id: int, chapter_id: int, body: ChapterUpdate, user: CurrentUser, db: DbDep
) -> Chapter:
    return await svc.update_chapter(db, user.id, project_id, chapter_id, body)
```

service 函数以 `db: AsyncSession` 和 `user_id: int` 作为前两个参数，
并以归属校验开头（见 `database-guidelines.md`）。

新增一个功能：

1. 在 `app/models/entities.py` 中添加模型（不使用 ORM `relationship()`），然后添加一个 Alembic revision。
2. 在 `app/schemas/<domain>.py` 中添加 schema（`XxxCreate`、`XxxUpdate`、带
   `ConfigDict(from_attributes=True)` 的 `XxxOut`）。例外：当 Out schema 不能直接映射
   数据行（涉及密钥）时，在 service 中显式构建 —— `AIConfigOut` 由
   `ai_config_service.to_out()` 构建，确保只有 `api_key_masked` 离开服务器。
3. 在 `app/services/<domain>_service.py` 中添加 service，每个入口都按 `user_id` 限定范围。
4. 在 `app/api/v1/<domain>.py` 中添加路由，并在 `app/api/v1/__init__.py` 中注册。
5. 任何 LLM 调用都经由 `ai_config_service.resolve_config()` +
   `ai_config_service.build_provider()`；prompt 文本放在 `app/ai/prompts/` 中，绝不内联在 service 里。

长耗时 AI 操作走**后台任务**：在 `task_engine/handlers/` 实现 `TaskHandler` 并 `register()`，
`validate_and_claim` 在创建请求内同步校验（可能抛出 `AppError` → 4xx），`build_job` / `run` 在引擎中用
sessionmaker 获取的独立 session 执行；路由只调用 `task_service.create_task` 返回 202。
局部改写预览仍是同步 SSE 的 **prepare** + **run** 两阶段（`rewrite_service.py`）。
见 `error-handling.md`「后台任务与 SSE 契约」与 `database-guidelines.md`「后台任务表」。

重复性的 CRUD 由工厂生成，而非复制粘贴：
`backend/app/api/v1/story.py` 中的 `_make_router(segment, kind, create_schema, update_schema, out_schema, tag)`，
其背后是 `backend/app/services/story_service.py` 中通用的 `StoryKind` dataclass。

---

## 命名约定

- 文件：`snake_case.py`。service 命名为 `<domain>_service.py`；不属于领域 service 的
  辅助模块保留描述性名称（`context_builder.py`、`json_extract.py`、`endpoint_guard.py`）。
- 路由以 `svc` 名称导入 service 模块：`from app.services import chapter_service as svc`。
- 路由对象命名为 `router`（当一个模块构建多个路由时命名为 `world_router` 等）。
- Schema：`<Entity>Create`、`<Entity>Update`、`<Entity>Out`，以及诸如
  `GenerateChapterRequest`、`WizardRequest` 之类的请求 DTO。
- 归属校验辅助函数：`get_owned_<entity>(db, user_id, id)`。
- `deps.py` 中的依赖别名以 `Dep` 结尾（`DbDep`、`SettingsDep`、`CipherDep`、
  `FactoryDep`、`SessionmakerDep`），`CurrentUser` 除外。
- 代码注释和 docstring 使用中文编写；面向用户的错误字符串使用中文。

---

## 示例

- 轻薄路由 + service 拆分：`backend/app/api/v1/ai_configs.py` 搭配 `backend/app/services/ai_config_service.py`。
- 通用 CRUD 工厂：`backend/app/api/v1/story.py` + `backend/app/services/story_service.py`。
- 后台任务类型：`backend/app/services/task_engine/handlers/chapter.py` 包装
  `generation_service.validate_and_claim` / `build_chapter_job` / `run_chapter_job`。
- 同步两阶段流式：`backend/app/services/rewrite_service.py`（`prepare_rewrite_job` / `run_rewrite_job`）。
- Provider 抽象：`backend/app/ai/providers/base.py` 及其三个实现。
