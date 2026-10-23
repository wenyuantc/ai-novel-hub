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

---

## 禁止模式

- **在路由中执行 ORM 查询**（在 `app/api/v1/` 中使用 `select`、`db.get`、`db.execute`）。应经由 service。
- service 中的**未限定范围查询**：任何面向用户的读写，只要没有经过
  `get_owned_project` / `get_owned_chapter` / `AIConfig.user_id == user_id`。
- **在 service/路由中直接调用 `create_provider` 或 provider 类**。
  唯一出口是 `ai_config_service.build_provider(..., allow_private=...)`（SSRF 防护 + 重定向策略）。
- **带有名为 `timeout` 参数的异步函数**（ruff `ASYNC109`，通过 `ASYNC` 启用）。
  通过构造函数传入超时（`LLMProvider(api_key, base_url, timeout)`），换一个名字
  （`build_provider(..., request_timeout, ...)`、`prepare_chapter_job(..., ai_timeout=...)`），
  或者像 `ai_config_service.test_config` 那样用 `asyncio.timeout(...)` 包裹调用。
- **在事件循环上执行阻塞的 CPU 工作**：bcrypt 通过 `anyio.to_thread.run_sync` 运行
  （`auth_service.register` / `authenticate`）。任何新增的重型同步调用都应照此处理。
- 在 service 中**直接使用 `HTTPException` / 手工构造错误 `JSONResponse`** —— 应抛出 `AppError` 子类。
- **返回密钥**：绝不能把 `api_key` / `api_key_enc` / `password_hash` 放进 Out schema。
- **在 service 中内联 prompt 文本** —— 它应放在 `app/ai/prompts/` 中。
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
  （`run_chapter_job`、`run_wizard_job`、`test_config`）。
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

### 假 LLM

`backend/tests/helpers.py` 中的 `FakeLLM` 通过 `create_app(settings, provider_factory=fake_llm.factory)` 注入。
`push("text")` 会拆分成 7 个字符一块；`push([..., AIProviderError(...)])` 会在流中途抛出异常；
`fake.calls` 记录 `provider/api_key/base_url/model/messages/max_tokens/thinking`，用于 prompt 和
参数断言。队列为空时产出 `["ok"]`。`fake.models`（`{"id", "name"?}` 列表或一个
`AIProviderError`）驱动 `list_models()`；`fake.list_calls` 记录 `provider/api_key/base_url/timeout`。

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

### 1. 范围 / 触发条件

任何使用用户可控 `base_url` 发起出站模型请求的代码路径
（ai-config 测试、章节生成/润色、向导）。触发条件：新增 provider、新增 AI
端点，或修改 provider 的构造方式。

### 2. 签名

```python
# backend/app/ai/endpoint_guard.py
def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool
async def _resolve(host: str, port: int) -> list[str]          # 测试中会被 patch
async def ensure_endpoint_allowed(base_url: str | None) -> None  # 抛出 AIProviderError

# backend/app/services/ai_config_service.py —— 两个出口
async def build_provider(cfg: AIConfig, cipher: SecretCipher, factory: ProviderFactory,
                         request_timeout: float, *, allow_private: bool) -> LLMProvider
# POST /ai-configs/models：基于*未保存*的表单值构建 provider，因此它自行重复执行
# ensure_endpoint_allowed + follow_redirects —— 两个出口需保持同步。
async def list_models(db, cipher, factory, user_id, req: ModelListRequest, *, allow_private: bool)
```

环境变量：`ALLOW_PRIVATE_AI_ENDPOINTS` → `Settings.allow_private_ai_endpoints: bool = False`；
路由传入 `allow_private=settings.allow_private_ai_endpoints`。

### 3. 契约

- 空值/`None` 的 `base_url` → 使用厂商默认地址，不做检查。
- scheme 必须为 `http`/`https` 且带有主机名，否则返回 `"Base URL 格式不正确，应以 http:// 或 https:// 开头"`。
- IP 字面量直接检查；主机名通过 `getaddrinfo` 解析；**每一个**地址
  都必须是公网地址。拦截条件：在解包 IPv4 映射的 IPv6 之后，`not ip.is_global or ip.is_multicast`；
  IPv6 zone id（`%eth0`）会被去除。
- `build_provider` 设置 `provider.follow_redirects = allow_private`；三个 provider 会把它传给
  各自的 HTTP 客户端：
  - openai：`http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects)`
  - anthropic：`http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects)`
  - gemini：`types.HttpOptions(..., async_client_args={"follow_redirects": self.follow_redirects})`
- 残余风险（记录在模块 docstring 中）：检查与连接之间存在 DNS 重绑定窗口。
  更强的隔离必须通过网络层的出站规则实现。

### 4. 校验与错误矩阵

| 输入 | `allow_private=False` | `allow_private=True` |
|-------|----------------------|----------------------|
| `None` / `""` | 允许 | 允许 |
| `https://api.example.com/v1`（公网） | 允许，关闭重定向 | 允许，开启重定向 |
| `http://127.0.0.1:11434/v1`、`localhost`、`[::1]`、`10.x`、`169.254.169.254`、`100.64.x`、`::ffff:10.0.0.1` | 包含 `ALLOW_PRIVATE_AI_ENDPOINTS` 的 `AIProviderError` | 允许 |
| 同时解析到公网**和**私网的主机 | 拒绝 | 允许 |
| `ftp://...`、`api.example.com/v1`、`http://:80` | `格式不正确` | 不检查 |
| 无法解析的主机 | `无法解析 Base URL 的域名：<host>` | 不检查 |

各端点的表现：

| 端点 | 被拦截时的结果 |
|----------|---------------------|
| `POST /ai-configs/{id}/test` | **200** `{"ok": false, "message": "...ALLOW_PRIVATE_AI_ENDPOINTS..."}` |
| `POST /chapters/{id}/generate|polish`、`POST /projects/{id}/wizard/{kind}` | **400** JSON `detail`（流开始前） |

两种情况下 provider factory 都不会被调用（`fake_llm.calls == []`）。

### 5. 正常 / 基础 / 错误用例

- 正常：OpenAI 兼容中转 `https://api.example.com/v1/` → 保存为 `https://api.example.com/v1`，测试通过。
- 基础：自托管的 Ollama `http://127.0.0.1:11434/v1` 仅在 `ALLOW_PRIVATE_AI_ENDPOINTS=true` 时可用
  （提示文本为 `PRIVATE_BLOCKED_HINT`）。
- 错误：公网主机 302 重定向到 `169.254.169.254` —— 之所以被拦截，仅仅是因为禁用了重定向；
  新增的 provider 如果忘记处理 `follow_redirects`，就会重新打开 SSRF 漏洞。

### 6. 必需测试（`backend/tests/test_endpoint_guard.py`）

- `test_blocked_addresses` / `test_public_addresses_allowed` —— `is_blocked_ip` 真值表，含 IPv4 映射的 IPv6。
- `test_private_urls_rejected` —— `match="ALLOW_PRIVATE_AI_ENDPOINTS"`，包括混合解析的主机。
- `test_malformed_urls_rejected`（`格式不正确`）、`test_unresolvable_host_rejected`（`无法解析`）。
- `test_build_provider_sets_redirect_policy` —— 拦截模式下 `follow_redirects is False`，允许模式下为 `True`。
- `test_build_provider_rejects_private_by_default` —— `BadRequestError`。
- `test_connectivity_test_reports_blocked_endpoint` —— 200，`ok is False`，`fake_llm.calls == []`。
- `test_wizard_rejects_private_endpoint` —— 400，detail 中提及该环境变量，不调用 LLM。
- `test_private_endpoint_allowed_when_enabled` —— 启用覆盖配置时 `ok is True`，base_url 原样传递。

### 7. 错误写法 vs 正确写法

错误写法：

```python
provider = create_provider(cfg.provider, cipher.decrypt(cfg.api_key_enc), cfg.base_url)
async for piece in provider.generate_stream(...): ...
```

正确写法：

```python
cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
```

---

## 场景：认证与密钥环境契约

### 1. 范围 / 触发条件

登录 cookie、JWT、注册开关以及 API key 加密。触发条件：修改
`app/core/config.py`、`app/core/security.py`、`app/api/v1/auth.py`、`app/core/deps.py`、
`.env.example`，或修改 AI 配置的序列化方式。

### 2. 签名

```python
# backend/app/core/config.py
class Settings(BaseSettings):
    jwt_secret: str = Field(min_length=16)
    encryption_key: str                          # 校验为合法的 Fernet key
    registration_enabled: bool = True
    cookie_secure: bool = False
    access_token_expire_minutes: int = Field(default=60 * 24 * 7, gt=0)

# backend/app/core/security.py
ACCESS_TOKEN_COOKIE = "access_token"; JWT_ALGORITHM = "HS256"
def create_access_token(user_id: int, secret: str, expire_minutes: int) -> str
def decode_access_token(token: str, secret: str) -> int | None
class SecretCipher: encrypt(str) -> str; decrypt(str) -> str  # InvalidToken 时抛出 ValueError
def mask_secret(secret: str) -> str
```

### 3. 契约

- 环境变量：`JWT_SECRET`（必填，>= 16 个字符）、`ENCRYPTION_KEY`（必填，合法的 Fernet key）、
  `REGISTRATION_ENABLED`（默认 `true`）、`COOKIE_SECURE`（默认 `false`；在 HTTPS 后设置为 `true`）、
  `ACCESS_TOKEN_EXPIRE_MINUTES`（默认 10080）。密钥缺失/非法时会在构造 `Settings()` 时失败，
  即在启动时失败（fail-fast），并提示如何生成 Fernet key。
- 注册/登录时设置的 cookie（`auth._set_auth_cookie`）：

```python
response.set_cookie(
    key=ACCESS_TOKEN_COOKIE, value=token, max_age=settings.access_token_expire_minutes * 60,
    httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
)
```

  登出时使用相同属性删除该 cookie，并返回 204。
- 认证解析（`deps._extract_token`）：优先读取 cookie，其次读取 `Authorization: Bearer <token>`。
  任何解码失败 → `None` → 401 `未登录或登录已过期`。
- JWT payload 为 `{"sub": str(user_id), "iat", "exp"}`，HS256。
- `GET /auth/config` → `{"registration_enabled": bool}`；注册关闭时注册 → 403 `注册已关闭，请联系管理员`。
- API key：仅以 `api_key_enc = cipher.encrypt(api_key.strip())` 的形式存储；响应中携带
  由 `mask_secret` 生成的 `api_key_masked`：

```python
if len(secret) >= 12: return f"{secret[:3]}***{secret[-4:]}"   # sk-abcdefgh1234 -> sk-***1234
if len(secret) >= 8:  return f"***{secret[-4:]}"               # abcdefgh -> ***efgh
return "***"                                                    # "" -> ""
```

- PATCH 时省略 `api_key` 或传 `""` 会保留已存储的 key。
- 轮换 `ENCRYPTION_KEY` 会导致已存储的 key 无法解密：列表显示 `（解密失败）`，
  生成时返回 400 `API Key 解密失败，请检查 ENCRYPTION_KEY 是否被更换`。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|-----------|--------|
| `ENCRYPTION_KEY` 缺失 / 不是 Fernet key | 启动时抛出 `ValidationError`（消息中提及 `ENCRYPTION_KEY`） |
| `JWT_SECRET` 缺失或少于 16 个字符 | 启动时抛出 `ValidationError` |
| 用户名少于 3 个、多于 32 个字符或包含空白；密码少于 6 个字符或超过 72 字节 | 422 |
| 用户名重复（含并发竞争） | 409 `用户名已存在` |
| 密码错误 / 用户不存在 | 401 `用户名或密码错误`（相同消息） |
| cookie 无效/过期 | 401 `未登录或登录已过期` |
| 注册已关闭 | 403 |

### 5. 正常 / 基础 / 错误用例

- 正常：注册 → `Set-Cookie: access_token=...; HttpOnly; SameSite=lax; Path=/`，默认不带 `Secure`。
- 基础：`COOKIE_SECURE=true` → 带有 `Secure` 属性。
- 错误：在 `AIConfigOut` 中返回 `api_key`，或在调试 provider 时把解密后的 key 写进日志。

### 6. 必需测试

- `backend/tests/test_config_security.py` —— `ENCRYPTION_KEY` 缺失/非法以及 `JWT_SECRET` 缺失时
  抛出 `ValidationError`；cipher 加解密往返；`test_mask_secret` 表
  （`sk-abcdefgh1234 → sk-***1234`、`abcdefgh → ***efgh`、`abc → ***`、`"" → ""`）。
- `backend/tests/test_auth.py` —— `test_cookie_attributes`（`httponly`、`samesite=lax`、不带 `secure`）、
  `test_cookie_secure_flag`、`test_registration_disabled`、`test_duplicate_username`、
  `test_invalid_token_rejected`、`test_register_login_logout_me`（登出后 cookie 被清除，`/me` 返回 401）。
- `backend/tests/test_ai_configs.py::test_api_key_masked_and_encrypted` —— `api_key_masked == "sk-***1234"`，
  `"api_key" not in cfg`，数据库中的值 != 明文且可解密还原；`test_update_keeps_key_when_omitted`。

### 7. 错误写法 vs 正确写法

错误写法：

```python
class AIConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    api_key_enc: str            # 泄露密文；若有人“修复”它，就会泄露明文
```

正确写法（`ai_config_service.to_out`）：

```python
masked = mask_secret(cipher.decrypt(cfg.api_key_enc))
return AIConfigOut(id=cfg.id, ..., api_key_masked=masked, ...)
```

---

## 场景：AI 配置模型参数（思考 / 上下文 / 最大输出）

### 1. 范围 / 触发条件
涉及 `ai_configs.thinking_level|context_window_k|max_output_k`、某个 provider 的 `generate_stream`
/ `list_models`，或章节/向导任务调用 provider 的方式。

### 2. 签名
```python
ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh", "max"]  # base.py + schemas（保持同步）
LLMProvider.generate_stream(messages, model, *, temperature=None, max_tokens=None, thinking="off")
LLMProvider.list_models() -> list[ModelInfo]                      # ModelInfo = {id, name}
ai_config_service.max_output_tokens(cfg) -> int | None           # max_output_k * 1000
ai_config_service.context_char_budget(cfg, default) -> int        # 取代 CONTEXT_CHAR_BUDGET
```

### 3. 契约
- K = 1000 个 token（不是 1024）：厂商所说的“64K 输出”上限即 64000。
- `thinking="off"` 时**不**发送任何 thinking 参数。绝不要发送 `{"type": "disabled"}` —— 较新的 Claude
  模型（Opus 5.5、Sonnet 5.5、Fable）会以 400 拒绝它。
- low…max：OpenAI 使用 `reasoning_effort=lvl`（原样透传；不支持的等级由上游报错）；
  Claude 4.6+ 使用 `thinking={"type":"adaptive"}` + `output_config={"effort":lvl}`（4.6 没有 `xhigh` →
  按 `high` 发送）；Claude ≤4.5 / 3.x 使用 `budget_tokens` 2048/8192/16384/24576/27648，并要求
  `max_tokens ≥ budget+4096`（max 设有上限，以满足 Opus 4.1 的 32000 输出限制）；
  Gemini 1/2 使用 `thinking_budget` 2048/8192/16384/24576/-1（动态）；Gemini 3+ 使用 `thinking_level`，
  xhigh/max → HIGH。
- provider 只产出回答文本（Anthropic 使用 `text_stream`，Gemini 使用 `chunk.text`，跳过思考内容）。
- 连通性测试始终使用 `thinking="off", max_tokens=16`。
- 上下文预算：`(context_window_k − (max_output_k or 8)) × 1000 − 4000`，1 个字符 ≈ 1 个 token，下限 1000；
  未设置 → 使用全局默认值。

### 4. 校验与错误矩阵
| 输入 | 结果 |
|---|---|
| `context_window_k` ∉ 1..2000、`max_output_k` ∉ 1..256、未知等级 | 422 |
| 创建时 `max_output_k ≥ context_window_k` | 422 |
| PATCH 后合并的值违反该规则 | 400 `最大输出必须小于上下文大小` |
| `/models` provider 报错 / 超时 / 私有 URL | 400，附中文 detail |
| `/models` 使用其他用户的 `config_id` | 404 |

### 5–7. 测试
`backend/tests/test_ai_config_params.py` 覆盖上述所有行以及映射表。错误写法：新增生成调用点时
遗漏 `max_tokens=job.max_tokens, thinking=job.thinking`。

## 代码评审清单

- [ ] 在 `backend/` 中 `uv run ruff check .` 和 `uv run pytest` 均通过。
- [ ] `app/api/v1/` 中没有 ORM 查询；新的 service 入口接收 `user_id` 并校验归属（他人资源返回 404）。
- [ ] 新增的归属路由已加入 `test_cross_user_isolation`。
- [ ] AI 调用经由 `build_provider(..., allow_private=settings.allow_private_ai_endpoints)`；新 provider 遵守 `follow_redirects`。
- [ ] 流式：校验放在 `prepare_*` 中，请求 session 在 `sse_response` 之前关闭，事件仅限 `delta`/`done`/`error`。
- [ ] AI 生成的字符串按列长度截断；内容变更时重新计算 `word_count`。
- [ ] 错误为带中文 `detail` 的 `AppError` 子类；未经 `describe_error` 处理的 SDK 原始文本不得外露。
- [ ] 响应和日志中没有密钥；已审查 Out schema。
- [ ] 模型变更附带 Alembic revision；`test_migrations.py` 通过。
- [ ] 测试使用 `tmp_path`、`FakeLLM` 和 `fake_dns`；不访问网络，不使用固定临时路径。
