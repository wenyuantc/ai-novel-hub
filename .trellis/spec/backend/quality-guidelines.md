# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

Both gates run from `backend/` and must pass before a change is done
(also documented in `README.md`):

```bash
cd backend && uv run ruff check . && uv run pytest
```

Ruff config (`backend/pyproject.toml`):

```toml
[tool.ruff]
line-length = 120
target-version = "py312"
extend-exclude = ["alembic/versions"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "ASYNC"]
ignore = ["B008"]          # FastAPI Depends()/Query() in defaults

[tool.ruff.lint.isort]
known-third-party = ["alembic"]   # local alembic/ dir would otherwise look first-party
```

Pytest config: `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`,
`pythonpath = ["."]`, `testpaths = ["tests"]`. Async tests are plain `async def test_...`
without markers.

---

## Forbidden Patterns

- **ORM queries in routes** (`select`, `db.get`, `db.execute` in `app/api/v1/`). Go through a service.
- **Unscoped queries** in services: any user-facing read/write that does not go through
  `get_owned_project` / `get_owned_chapter` / `AIConfig.user_id == user_id`.
- **Calling `create_provider` or a provider class directly** from services/routes.
  The only exit is `ai_config_service.build_provider(..., allow_private=...)` (SSRF guard + redirect policy).
- **Async functions with a parameter named `timeout`** (ruff `ASYNC109`, enabled via `ASYNC`).
  Pass timeouts through the constructor (`LLMProvider(api_key, base_url, timeout)`), name it
  differently (`build_provider(..., request_timeout, ...)`, `prepare_chapter_job(..., ai_timeout=...)`),
  or wrap the call in `asyncio.timeout(...)` as `ai_config_service.test_config` does.
- **Blocking CPU work on the event loop**: bcrypt runs via `anyio.to_thread.run_sync`
  (`auth_service.register` / `authenticate`). Do the same for any new heavy sync call.
- **Raw `HTTPException` / hand-built error `JSONResponse`** in services — raise an `AppError` subclass.
- **Returning secrets**: never put `api_key` / `api_key_enc` / `password_hash` in an Out schema.
- **Prompt text inline in services** — it belongs in `app/ai/prompts/`.
- **Tests that hit real DNS, real LLM APIs, or shared fixed paths** (`/tmp/test.db`, a shared
  `.env`). See Testing Requirements.

---

## Required Patterns

- App factory + DI: every shared object comes from `app.state` through `app/core/deps.py`
  (`DbDep`, `SettingsDep`, `CipherDep`, `FactoryDep`, `SessionmakerDep`, `CurrentUser`).
- Settings are read via `SettingsDep` in routes (not `get_settings()`), so tests can inject
  `make_settings(tmp_path, **overrides)`.
- Two-phase streaming endpoints (prepare raises, run yields events) — see `error-handling.md`.
- Wrap provider streams in `contextlib.aclosing(...)` when iterating them
  (`run_chapter_job`, `run_wizard_job`, `test_config`).
- Close SDK clients in `finally` (`await client.close()` in openai_compat/anthropic).
- Strip user strings on write (`data.title.strip()`), normalize `base_url` with
  `_clean_base_url` (trailing `/` removed, empty → `None`).

---

## Testing Requirements

### Isolation (mandatory)

Every test gets its own app and its own SQLite file under pytest's per-test `tmp_path`
(`backend/tests/helpers.py`):

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

The `app` fixture (`backend/tests/conftest.py`) runs `Base.metadata.create_all` on that
engine and disposes it afterwards. Never use a fixed shared path or a shared database;
concurrent agent sessions run the suite in parallel on the same checkout.
`Settings(_env_file=None, ...)` keeps a developer's `.env` out of tests.

`conftest.py` sets `JWT_SECRET`, `ENCRYPTION_KEY` and `FRONTEND_DIST_DIR` in `os.environ`
**before** importing `app.main`, because `app.main` builds the default app at import time.

### Overriding settings per test

Parametrize the `settings_overrides` fixture:

```python
@pytest.mark.parametrize("settings_overrides", [{"registration_enabled": False}])
async def test_registration_disabled(client): ...
```

### Fake LLM

`FakeLLM` in `backend/tests/helpers.py` is injected as `create_app(settings, provider_factory=fake_llm.factory)`.
`push("text")` splits into 7-char chunks; `push([..., AIProviderError(...)])` raises mid-stream;
`fake.calls` records `provider/api_key/base_url/model/messages/max_tokens/thinking` for prompt and
parameter assertions. An empty queue yields `["ok"]`. `fake.models` (list of `{"id", "name"?}` or an
`AIProviderError`) drives `list_models()`; `fake.list_calls` records `provider/api_key/base_url/timeout`.

### No real DNS

`fake_dns` is `autouse=True` and monkeypatches `app.ai.endpoint_guard._resolve`:
unknown hosts → `93.184.216.34`, `internal.example.test` → `10.0.0.5`,
`rebind.example.test` → public + `127.0.0.1`, `metadata.example.test` → `169.254.169.254`,
`nxdomain.example.test` → `OSError`. New SSRF tests add hosts to `FAKE_DNS` or monkeypatch locally.

### Helpers

`register`, `create_project`, `create_ai_config` assert the expected status and return JSON;
`parse_sse(text)` returns `[(event, data)]`; `make_client()` gives a second client with its own
cookie jar for cross-user tests.

### What every feature needs

- Happy path through HTTP (`httpx.AsyncClient` + `ASGITransport`).
- Cross-user 404 for each new owned route (extend `test_cross_user_isolation`'s `checks` list).
- Error envelope assertions on `resp.json()["detail"]` (Chinese string).
- Schema changes: `tests/test_migrations.py` still passes.

---

## Scenario: SSRF guard for user-supplied AI Base URL

### 1. Scope / Trigger

Any code path that makes an outbound model request with a user-controlled `base_url`
(ai-config test, chapter generate/polish, wizard). Trigger: adding a provider, a new AI
endpoint, or changing how providers are constructed.

### 2. Signatures

```python
# backend/app/ai/endpoint_guard.py
def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool
async def _resolve(host: str, port: int) -> list[str]          # patched in tests
async def ensure_endpoint_allowed(base_url: str | None) -> None  # raises AIProviderError

# backend/app/services/ai_config_service.py — the two exits
async def build_provider(cfg: AIConfig, cipher: SecretCipher, factory: ProviderFactory,
                         request_timeout: float, *, allow_private: bool) -> LLMProvider
# POST /ai-configs/models: builds a provider from *unsaved* form values, so it repeats
# ensure_endpoint_allowed + follow_redirects itself — keep both exits in sync.
async def list_models(db, cipher, factory, user_id, req: ModelListRequest, *, allow_private: bool)
```

Env: `ALLOW_PRIVATE_AI_ENDPOINTS` → `Settings.allow_private_ai_endpoints: bool = False`;
routes pass `allow_private=settings.allow_private_ai_endpoints`.

### 3. Contracts

- Empty/`None` `base_url` → vendor default, not checked.
- Scheme must be `http`/`https` with a hostname, else `"Base URL 格式不正确，应以 http:// 或 https:// 开头"`.
- IP literals are checked directly; hostnames are resolved with `getaddrinfo`; **every** address
  must be public. Blocked: `not ip.is_global or ip.is_multicast`, after unwrapping IPv4-mapped IPv6;
  IPv6 zone ids (`%eth0`) are stripped.
- `build_provider` sets `provider.follow_redirects = allow_private`; the three providers pass it to
  their HTTP clients:
  - openai: `http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects)`
  - anthropic: `http_client=DefaultAsyncHttpxClient(follow_redirects=self.follow_redirects)`
  - gemini: `types.HttpOptions(..., async_client_args={"follow_redirects": self.follow_redirects})`
- Residual risk (documented in the module docstring): a DNS-rebinding window between check and
  connect. Stronger isolation must be done with network-level egress rules.

### 4. Validation & Error Matrix

| Input | `allow_private=False` | `allow_private=True` |
|-------|----------------------|----------------------|
| `None` / `""` | allowed | allowed |
| `https://api.example.com/v1` (public) | allowed, redirects off | allowed, redirects on |
| `http://127.0.0.1:11434/v1`, `localhost`, `[::1]`, `10.x`, `169.254.169.254`, `100.64.x`, `::ffff:10.0.0.1` | `AIProviderError` containing `ALLOW_PRIVATE_AI_ENDPOINTS` | allowed |
| host resolving to public **and** private | rejected | allowed |
| `ftp://...`, `api.example.com/v1`, `http://:80` | `格式不正确` | not checked |
| unresolvable host | `无法解析 Base URL 的域名：<host>` | not checked |

Surface per endpoint:

| Endpoint | Result when blocked |
|----------|---------------------|
| `POST /ai-configs/{id}/test` | **200** `{"ok": false, "message": "...ALLOW_PRIVATE_AI_ENDPOINTS..."}` |
| `POST /chapters/{id}/generate|polish`, `POST /projects/{id}/wizard/{kind}` | **400** JSON `detail` (pre-stream) |

In both cases the provider factory is never invoked (`fake_llm.calls == []`).

### 5. Good / Base / Bad Cases

- Good: OpenAI-compatible relay `https://api.example.com/v1/` → saved as `https://api.example.com/v1`, test ok.
- Base: self-hosted Ollama `http://127.0.0.1:11434/v1` works only with `ALLOW_PRIVATE_AI_ENDPOINTS=true`
  (hint text `PRIVATE_BLOCKED_HINT`).
- Bad: public host that 302-redirects to `169.254.169.254` — blocked only because redirects are disabled;
  a new provider that forgets `follow_redirects` reopens SSRF.

### 6. Tests Required (`backend/tests/test_endpoint_guard.py`)

- `test_blocked_addresses` / `test_public_addresses_allowed` — `is_blocked_ip` truth table incl. IPv4-mapped IPv6.
- `test_private_urls_rejected` — `match="ALLOW_PRIVATE_AI_ENDPOINTS"`, including the mixed-resolution host.
- `test_malformed_urls_rejected` (`格式不正确`), `test_unresolvable_host_rejected` (`无法解析`).
- `test_build_provider_sets_redirect_policy` — `follow_redirects is False` when blocked mode, `True` when allowed.
- `test_build_provider_rejects_private_by_default` — `BadRequestError`.
- `test_connectivity_test_reports_blocked_endpoint` — 200, `ok is False`, `fake_llm.calls == []`.
- `test_wizard_rejects_private_endpoint` — 400, detail mentions the env var, no LLM call.
- `test_private_endpoint_allowed_when_enabled` — with override, `ok is True`, base_url passed through.

### 7. Wrong vs Correct

Wrong:

```python
provider = create_provider(cfg.provider, cipher.decrypt(cfg.api_key_enc), cfg.base_url)
async for piece in provider.generate_stream(...): ...
```

Correct:

```python
cfg = await ai_config_service.resolve_config(db, user_id, req.ai_config_id)
provider = await ai_config_service.build_provider(cfg, cipher, factory, ai_timeout, allow_private=allow_private)
```

---

## Scenario: Auth and secrets environment contract

### 1. Scope / Trigger

Login cookie, JWT, registration switch and API-key encryption. Trigger: changing
`app/core/config.py`, `app/core/security.py`, `app/api/v1/auth.py`, `app/core/deps.py`,
`.env.example`, or how AI configs are serialized.

### 2. Signatures

```python
# backend/app/core/config.py
class Settings(BaseSettings):
    jwt_secret: str = Field(min_length=16)
    encryption_key: str                          # validated as a Fernet key
    registration_enabled: bool = True
    cookie_secure: bool = False
    access_token_expire_minutes: int = Field(default=60 * 24 * 7, gt=0)

# backend/app/core/security.py
ACCESS_TOKEN_COOKIE = "access_token"; JWT_ALGORITHM = "HS256"
def create_access_token(user_id: int, secret: str, expire_minutes: int) -> str
def decode_access_token(token: str, secret: str) -> int | None
class SecretCipher: encrypt(str) -> str; decrypt(str) -> str  # ValueError on InvalidToken
def mask_secret(secret: str) -> str
```

### 3. Contracts

- Env vars: `JWT_SECRET` (required, >= 16 chars), `ENCRYPTION_KEY` (required, valid Fernet key),
  `REGISTRATION_ENABLED` (default `true`), `COOKIE_SECURE` (default `false`; set `true` behind HTTPS),
  `ACCESS_TOKEN_EXPIRE_MINUTES` (default 10080). Missing/invalid secrets fail at `Settings()`
  construction, i.e. at startup (fail-fast), with a hint on how to generate a Fernet key.
- Cookie set on register/login (`auth._set_auth_cookie`):

```python
response.set_cookie(
    key=ACCESS_TOKEN_COOKIE, value=token, max_age=settings.access_token_expire_minutes * 60,
    httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
)
```

  Logout deletes it with the same attributes and returns 204.
- Auth resolution (`deps._extract_token`): cookie first, then `Authorization: Bearer <token>`.
  Any decode failure → `None` → 401 `未登录或登录已过期`.
- JWT payload `{"sub": str(user_id), "iat", "exp"}`, HS256.
- `GET /auth/config` → `{"registration_enabled": bool}`; register when disabled → 403 `注册已关闭，请联系管理员`.
- API keys: stored only as `api_key_enc = cipher.encrypt(api_key.strip())`; responses carry
  `api_key_masked` from `mask_secret`:

```python
if len(secret) >= 12: return f"{secret[:3]}***{secret[-4:]}"   # sk-abcdefgh1234 -> sk-***1234
if len(secret) >= 8:  return f"***{secret[-4:]}"               # abcdefgh -> ***efgh
return "***"                                                    # "" -> ""
```

- PATCH with `api_key` omitted or `""` keeps the stored key.
- Rotating `ENCRYPTION_KEY` makes stored keys undecryptable: list shows `（解密失败）`,
  generation returns 400 `API Key 解密失败，请检查 ENCRYPTION_KEY 是否被更换`.

### 4. Validation & Error Matrix

| Condition | Result |
|-----------|--------|
| `ENCRYPTION_KEY` missing / not Fernet | `ValidationError` at startup (message mentions `ENCRYPTION_KEY`) |
| `JWT_SECRET` missing or < 16 chars | `ValidationError` at startup |
| username < 3, > 32 or containing whitespace; password < 6 or > 72 bytes | 422 |
| duplicate username (incl. concurrent race) | 409 `用户名已存在` |
| wrong password / unknown user | 401 `用户名或密码错误` (same message) |
| invalid/expired cookie | 401 `未登录或登录已过期` |
| registration disabled | 403 |

### 5. Good / Base / Bad Cases

- Good: register → `Set-Cookie: access_token=...; HttpOnly; SameSite=lax; Path=/`, no `Secure` by default.
- Base: `COOKIE_SECURE=true` → `Secure` attribute present.
- Bad: returning `api_key` in `AIConfigOut`, or logging the decrypted key while debugging a provider.

### 6. Tests Required

- `backend/tests/test_config_security.py` — missing/invalid `ENCRYPTION_KEY` and missing `JWT_SECRET`
  raise `ValidationError`; cipher round trip; `test_mask_secret` table
  (`sk-abcdefgh1234 → sk-***1234`, `abcdefgh → ***efgh`, `abc → ***`, `"" → ""`).
- `backend/tests/test_auth.py` — `test_cookie_attributes` (`httponly`, `samesite=lax`, no `secure`),
  `test_cookie_secure_flag`, `test_registration_disabled`, `test_duplicate_username`,
  `test_invalid_token_rejected`, `test_register_login_logout_me` (cookie cleared, `/me` 401 after logout).
- `backend/tests/test_ai_configs.py::test_api_key_masked_and_encrypted` — `api_key_masked == "sk-***1234"`,
  `"api_key" not in cfg`, DB value != plaintext and decrypts back; `test_update_keeps_key_when_omitted`.

### 7. Wrong vs Correct

Wrong:

```python
class AIConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    api_key_enc: str            # leaks ciphertext; plaintext if someone "fixes" it
```

Correct (`ai_config_service.to_out`):

```python
masked = mask_secret(cipher.decrypt(cfg.api_key_enc))
return AIConfigOut(id=cfg.id, ..., api_key_masked=masked, ...)
```

---

## Scenario: AI config model parameters (thinking / context / max output)

### 1. Scope / Trigger
Touching `ai_configs.thinking_level|context_window_k|max_output_k`, a provider's `generate_stream`
/ `list_models`, or how chapter/wizard jobs call the provider.

### 2. Signatures
```python
ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh", "max"]  # base.py + schemas (keep in sync)
LLMProvider.generate_stream(messages, model, *, temperature=None, max_tokens=None, thinking="off")
LLMProvider.list_models() -> list[ModelInfo]                      # ModelInfo = {id, name}
ai_config_service.max_output_tokens(cfg) -> int | None           # max_output_k * 1000
ai_config_service.context_char_budget(cfg, default) -> int        # replaces CONTEXT_CHAR_BUDGET
```

### 3. Contracts
- K = 1000 tokens (not 1024): vendor "64K output" caps are 64000.
- `thinking="off"` sends **no** thinking parameter. Never send `{"type": "disabled"}` — newer Claude
  models (Opus 5.5, Sonnet 5.5, Fable) reject it with 400.
- low…max: OpenAI `reasoning_effort=lvl` (passed through; unsupported levels error upstream);
  Claude 4.6+ `thinking={"type":"adaptive"}` + `output_config={"effort":lvl}` (4.6 has no `xhigh` →
  sent as `high`); Claude ≤4.5 / 3.x `budget_tokens` 2048/8192/16384/24576/27648 with
  `max_tokens ≥ budget+4096` (max capped so Opus 4.1's 32000 output limit holds);
  Gemini 1/2 `thinking_budget` 2048/8192/16384/24576/-1(dynamic); Gemini 3+ `thinking_level`,
  xhigh/max → HIGH.
- Providers yield answer text only (Anthropic `text_stream`, Gemini `chunk.text` skip thoughts).
- Connectivity test always `thinking="off", max_tokens=16`.
- Context budget: `(context_window_k − (max_output_k or 8)) × 1000 − 4000`, 1 char ≈ 1 token, floor 1000;
  unset → global default.

### 4. Validation & Error Matrix
| Input | Result |
|---|---|
| `context_window_k` ∉ 1..2000, `max_output_k` ∉ 1..256, unknown level | 422 |
| create with `max_output_k ≥ context_window_k` | 422 |
| PATCH making merged values violate the rule | 400 `最大输出必须小于上下文大小` |
| `/models` provider error / timeout / private URL | 400 with Chinese detail |
| `/models` with another user's `config_id` | 404 |

### 5–7. Tests
`backend/tests/test_ai_config_params.py` covers all rows above plus mapping tables. Wrong: adding a
generation call site that forgets `max_tokens=job.max_tokens, thinking=job.thinking`.

## Code Review Checklist

- [ ] `uv run ruff check .` and `uv run pytest` pass in `backend/`.
- [ ] No ORM queries in `app/api/v1/`; new service entry points take `user_id` and check ownership (404 for foreign).
- [ ] New owned routes added to `test_cross_user_isolation`.
- [ ] AI calls go through `build_provider(..., allow_private=settings.allow_private_ai_endpoints)`; new providers honor `follow_redirects`.
- [ ] Streaming: validation in `prepare_*`, request session closed before `sse_response`, events limited to `delta`/`done`/`error`.
- [ ] AI-derived strings sliced to column length; `word_count` recomputed on content change.
- [ ] Errors are `AppError` subclasses with Chinese `detail`; no raw SDK text without `describe_error`.
- [ ] No secrets in responses or logs; Out schemas reviewed.
- [ ] Model change has an Alembic revision; `test_migrations.py` passes.
- [ ] Tests use `tmp_path`, `FakeLLM` and `fake_dns`; no network, no fixed temp paths.
