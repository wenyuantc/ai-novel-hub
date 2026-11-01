# 本任务相关规范摘录：backend/quality-guidelines.md

来源：`.trellis/spec/backend/quality-guidelines.md`

规划时 SHA-256：`c08a31f2d8473dfe415cd36253f365d86bcb02a61b2595877c08372cd88ef04b`

仅摘录相关原文，完整规范是权威来源；源文件变化时重读相应段落。本摘录不是新增规范。

## 原文 L1–L149

# 质量规范

> 后端开发的代码质量标准。

---

## 概述

两道门禁都在 `backend/` 下运行，变更完成前必须全部通过
（`README.md` 中也有说明）：

```bash
cd backend && uv run ruff check . && uv run pytest
```

Ruff 配置（`backend/pyproject.toml`）：

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
extend-exclude = ["alembic/versions"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "ASYNC"]
ignore = ["B008"]          # 默认参数中使用 FastAPI 的 Depends()/Query()

[tool.ruff.lint.isort]
known-third-party = ["alembic"]   # 否则本地的 alembic/ 目录会被识别为 first-party
```

Pytest 配置：`asyncio_mode = "auto"`、`asyncio_default_fixture_loop_scope = "function"`、
`pythonpath = ["."]`、`testpaths = ["tests"]`。异步测试直接写成 `async def test_...`，
无需标记（marker）。

macOS 终端若使用 `LC_ALL=C`，Alembic/ConfigParser 的显式 `encoding="locale"` 会以 ASCII 读取带中文注释的 alembic.ini，导致迁移测试在执行迁移前 UnicodeDecodeError。先核对 locale；可在本次命令使用 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest`。不要因此修改迁移、删除中文配置、过滤失败或跳过测试；Python UTF-8 模式不等同于显式 locale 编码已正确。

---

## 禁止模式

- **在路由中执行 ORM 查询**（在 `app/api/v1/` 中使用 `select`、`db.get`、`db.execute`）。应经由 service。
- service 中的**未限定范围查询**：任何面向用户的读写，只要没有经过
  `get_owned_project` / `get_owned_chapter` / `AIConfig.user_id == user_id`。
- **在 service/路由中直接调用 `create_provider` 或 provider 类**。
  唯一出口是 `ai_config_service.build_provider(..., allow_private=...)`（SSRF 防护 + 重定向策略）。
- **带有名为 `timeout` 参数的异步函数**（ruff `ASYNC109`，通过 `ASYNC` 启用）。
  通过构造函数传入超时（`LLMProvider(api_key, base_url, timeout)`），换一个名字
  （`build_provider(..., request_timeout, ...)`、`build_chapter_job(..., ai_timeout=...)`），
  或者像 `ai_config_service.test_config` 那样用 `asyncio.timeout(...)` 包裹调用。
- **在事件循环上执行阻塞的 CPU 工作**：bcrypt 通过 `anyio.to_thread.run_sync` 运行
  （`auth_service.register` / `authenticate`）。任何新增的重型同步调用都应照此处理。
- 在 service 中**直接使用 `HTTPException` / 手工构造错误 `JSONResponse`** —— 应抛出 `AppError` 子类。窄例外：`api_key_service.authenticate_key` 的 429 使用携带 `Retry-After` 的 HTTPException，复用现有保留 headers 的处理器；详见 [平台开放接口](./open-api.md)，不扩大为普通业务异常的默认模式。
- **返回模型供应商凭据或默认暴露访问密钥**：绝不能把 `api_key` / `api_key_enc` / `password_hash` 放进普通 Out schema。平台签发 Key 的本人创建 / 主动 reveal 是用户明确要求的专用响应例外，必须 no-store、只读本人的密钥；列表 / 编辑 / 开放读取都不返回完整 Key，日志始终不记录密钥，见 [平台密钥契约](./open-api.md)。
- **在 service 中内联 prompt 文本** —— 提示词文本放在 `app/ai/prompts/templates/*.j2` 默认模板中，
  `build_*_messages` 只拼变量（见 [prompts-styles.md](./prompts-styles.md)；改默认模板要同步 `tests/prompt_legacy.py`）。
- **访问真实 DNS、真实 LLM API 或共享固定路径的测试**（`/tmp/test.db`、共享的
  `.env`）。见“测试要求”。

---

## 必需模式

- 应用工厂 + 依赖注入：所有共享对象都通过 `app/core/deps.py` 从 `app.state` 获取
  （`DbDep`、`SettingsDep`、`CipherDep`、`FactoryDep`、`SessionmakerDep`、`CurrentUser`）。
- 路由中通过 `SettingsDep` 读取配置（而不是 `get_settings()`），这样测试可以注入
  `make_settings(tmp_path, **overrides)`。
- 两阶段流式端点（prepare 阶段抛出异常，run 阶段产出事件）—— 见 `error-handling.md`。
- 迭代 provider 流时用 `contextlib.aclosing(...)` 包裹
  （`run_chapter_job`、`run_wizard_job`、`test_config`；引擎消费 runner 事件时同样如此）。
- 在 `finally` 中关闭 SDK 客户端（openai_compat/anthropic 中的 `await client.close()`）。
- 写入时去除用户字符串首尾空白（`data.title.strip()`），用 `_clean_base_url` 规范化 `base_url`
  （去掉末尾的 `/`，空值 → `None`）。

---

## 测试要求

### 隔离（强制）

每个测试都拥有独立的应用，以及位于 pytest 按测试创建的 `tmp_path` 下的独立 SQLite 文件
（`backend/tests/helpers.py`）：

```python
def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        # 每个测试独立的临时库文件（tmp_path 由 pytest 按测试单独创建）
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        ...
        "frontend_dist_dir": str(tmp_path / "no-dist"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)
```

`app` fixture（`backend/tests/conftest.py`）在该 engine 上执行 `Base.metadata.create_all`，
并在之后将其释放。绝不要使用固定的共享路径或共享数据库；
并发的 agent 会话会在同一份检出代码上并行运行测试套件。
`Settings(_env_file=None, ...)` 可确保开发者本地的 `.env` 不会影响测试。

`conftest.py` 会在导入 `app.main` **之前**在 `os.environ` 中设置 `JWT_SECRET`、`ENCRYPTION_KEY` 和 `FRONTEND_DIST_DIR`，
因为 `app.main` 在导入时就会构建默认应用。

### 按测试覆盖配置

对 `settings_overrides` fixture 进行参数化：

```python
@pytest.mark.parametrize("settings_overrides", [{"registration_enabled": False}])
async def test_registration_disabled(client): ...
```

`make_settings` 默认 `auto_analyze_enabled=False`：否则 AI 生成 / 润色成功后会自动排队章节分析，额外消耗 FakeLLM 的预设输出、
改变任务数与排队顺序。自动分析相关测试用 `@pytest.mark.parametrize("settings_overrides", [{"auto_analyze_enabled": True}])` 显式开启。

### 假 LLM

`backend/tests/helpers.py` 中的 `FakeLLM` 通过 `create_app(settings, provider_factory=fake_llm.factory)` 注入。
`push("text")` 会拆分成 7 个字符一块；`push([..., AIProviderError(...)])` 会在流中途抛出异常；
`fake.calls` 记录 `provider/api_key/base_url/model/messages/max_tokens/thinking`，用于 prompt 和
参数断言。队列为空时产出 `["ok"]`。分片中可放 `Gate()`：流到达时置 `gate.reached`，`gate.release()` 后才继续，
用于取消 / 并发 / 排队测试；`fake.aborted` 记录未产出完就被关闭的流数量。`fake.models`（`{"id", "name"?}` 列表或一个
`AIProviderError`）驱动 `list_models()`；`fake.list_calls` 记录 `provider/api_key/base_url/timeout`。`embed(texts, model)` 返回 `fake_vector(text, fake.embed_dim)`（按字符二元组哈希的确定性向量，默认 64 维），
`fake.embed_calls` 记录 `provider/model/texts`，`fake.embed_error` 抛出一次（`embed_error_sticky=True` 时持续抛出）。知识库样例文件由
`tests/kb_fixtures.py` 现场生成（docx / epub 用 zipfile，pdf 用 pypdf），不提交二进制文件。

### 不访问真实 DNS

`fake_dns` 为 `autouse=True`，并对 `app.ai.endpoint_guard._resolve` 做 monkeypatch：
未知主机 → `93.184.216.34`，`internal.example.test` → `10.0.0.5`，
`rebind.example.test` → 公网地址 + `127.0.0.1`，`metadata.example.test` → `169.254.169.254`，
`nxdomain.example.test` → `OSError`。新的 SSRF 测试可向 `FAKE_DNS` 添加主机，或在本地进行 monkeypatch。

### 辅助函数

`register`、`create_project`、`create_ai_config` 会断言预期状态码并返回 JSON；
`parse_sse(text)` 返回 `[(event, data)]`；`make_client()` 提供第二个拥有独立
cookie jar 的客户端，用于跨用户测试。

### 每个功能都需要

- 通过 HTTP 的正常路径测试（`httpx.AsyncClient` + `ASGITransport`）。
- 每个新增的归属路由都要有跨用户 404 测试（扩展 `test_cross_user_isolation` 的 `checks` 列表）。
- 对 `resp.json()["detail"]`（中文字符串）进行错误响应包断言。
- Schema 变更：`tests/test_migrations.py` 仍能通过。

---

## 场景：用户提供的 AI Base URL 的 SSRF 防护

## 原文 L402–L415

## 代码评审清单

- [ ] 在 `backend/` 中 `uv run ruff check .` 和 `uv run pytest` 均通过。
- [ ] `app/api/v1/` 中没有 ORM 查询；新的 service 入口接收 `user_id` 并校验归属（他人资源返回 404）。
- [ ] 人工项目管理员例外仅通过 `get_manageable_project` 及明确的人工子资源入口；AI/任务/日志/私有配置继续严格归属，跨用户记忆保存不排队所有者向量任务，见 [admin-projects.md](./admin-projects.md)。
- [ ] 新增的归属路由已加入 `test_cross_user_isolation`。
- [ ] AI 调用经由 `build_provider(..., allow_private=settings.allow_private_ai_endpoints)`；新 provider 遵守 `follow_redirects`。
- [ ] 后台任务：校验放在 `validate_and_claim` 中（不 commit），运行逻辑在 `build_job`/`run` 中使用独立 session；runner 事件仅限 `delta`/`continue`（仅整章生成）/`done`/`error`。同步 SSE（改写）：校验放在 `prepare_*`，请求 session 在 `sse_response` 之前关闭。
- [ ] 测试中引擎真实运行：用 `Gate` 控制阻塞，`wait_task` / `stream_task` 等待结束，不用长 sleep。
- [ ] AI 生成的字符串按列长度截断；内容变更时重新计算 `word_count`。
- [ ] 错误为带中文 `detail` 的 `AppError` 子类；未经 `describe_error` 处理的 SDK 原始文本不得外露。
- [ ] 普通响应和日志中没有密钥；已审查 Out schema。平台 Key 的本人创建 / reveal 专用响应遵守 open-api.md，列表、编辑及对外读取仍不得含完整 Key。
- [ ] 模型变更附带 Alembic revision；`test_migrations.py` 通过。
- [ ] 测试使用 `tmp_path`、`FakeLLM` 和 `fake_dns`；不访问网络，不使用固定临时路径。
