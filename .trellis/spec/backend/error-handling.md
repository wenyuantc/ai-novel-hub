# Error Handling

> How errors are handled in this project.

---

## Overview

- Business code raises typed `AppError` subclasses from `backend/app/core/errors.py`.
  It never builds `JSONResponse` or raises `fastapi.HTTPException` itself.
- `register_error_handlers(app)` (called in `create_app`) maps every error to the
  same envelope: `{"detail": "<Chinese message>"}`.
- Model/provider failures are `AIProviderError` (`backend/app/ai/providers/base.py`),
  whose message is already safe, user-facing Chinese. Services convert them to
  `BadRequestError` before streaming starts, or to an SSE `error` event after.
- Unknown exceptions are logged with a stack trace and masked as 500 `服务器内部错误`.

---

## Error Types

`backend/app/core/errors.py` (subclasses condensed to one line each):

```python
class AppError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class BadRequestError(AppError):   status_code = 400
class UnauthorizedError(AppError): status_code = 401
class ForbiddenError(AppError):    status_code = 403
class NotFoundError(AppError):     status_code = 404
class ConflictError(AppError):     status_code = 409
```

When to use which (real call sites):

| Error | Used for | Example |
|-------|----------|---------|
| `BadRequestError` | invalid business input, missing prerequisite, blocked AI endpoint | `"尚未配置 AI 模型，请先在「模型配置」中添加并设为默认"` (`ai_config_service.resolve_config`), `"章节正文为空，无法润色"` |
| `UnauthorizedError` | no/invalid/expired JWT, wrong credentials | `"未登录或登录已过期"` (`deps.get_current_user`), `"用户名或密码错误"` |
| `ForbiddenError` | feature disabled by config | `"注册已关闭，请联系管理员"` |
| `NotFoundError` | missing **or foreign-owned** resource | `"项目不存在"`, `"章节不存在"`, `"模型配置不存在"` |
| `ConflictError` | uniqueness / state conflict | `"用户名已存在"`, `"该章节正在生成中，请稍候再试"` |

`AIProviderError(Exception)` — "模型调用失败（网络、鉴权、配额等），message 为可直接展示给用户的中文说明".
Providers wrap every SDK exception via `describe_error`, which truncates to 500 chars:

```python
def describe_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 500:
        text = text[:500] + "…"
    return f"模型调用失败：{text}"
```

Provider pattern (`backend/app/ai/providers/openai_compat.py`, same in anthropic/gemini):

```python
try:
    ...
except AIProviderError:
    raise
except Exception as exc:  # noqa: BLE001
    raise AIProviderError(describe_error(exc)) from exc
finally:
    await client.close()
```

`JSONExtractError(ValueError)` (`backend/app/ai/json_extract.py`) signals unparsable
wizard output; it is turned into an SSE error `"AI 输出解析失败：{exc}，请重新生成"`.

---

## Error Handling Patterns

### Convert at the boundary that owns the meaning

`ai_config_service.build_provider` is the single place that turns decrypt and
endpoint-guard failures into HTTP errors:

```python
try:
    api_key = cipher.decrypt(cfg.api_key_enc)
except ValueError as exc:
    raise BadRequestError(str(exc)) from exc
try:
    if not allow_private:
        await ensure_endpoint_allowed(cfg.base_url)
    provider = factory(cfg.provider, api_key, cfg.base_url, request_timeout)
except AIProviderError as exc:
    raise BadRequestError(str(exc)) from exc
```

Always chain with `from exc`.

### Unique-constraint races → 409

Pre-check plus constraint fallback (`backend/app/services/auth_service.py`):

```python
db.add(user)
try:
    await db.commit()
except IntegrityError as exc:
    # 并发注册同名用户时由唯一约束兜底
    await db.rollback()
    raise ConflictError("用户名已存在") from exc
```

### Result objects instead of errors

The connectivity test (`POST /ai-configs/{id}/test`) never fails with 4xx/5xx for
provider problems: it returns `200` with `AIConfigTestResult(ok=False, message=..., latency_ms=...)`
for blocked endpoints, timeouts (`asyncio.timeout(TEST_TIMEOUT_SECONDS)`, 30 s), provider errors
and empty replies (`ai_config_service.test_config`). Only ownership (404) still raises.

### Degrade instead of crash when listing

`ai_config_service.to_out` catches decrypt failure and shows `"（解密失败）"` as the
masked key rather than failing the whole list.

---

## API Error Responses

All handlers live in `register_error_handlers`:

```python
@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
```

**422 validation** — keeps a Chinese summary in `detail` plus a sanitized `errors` list:

```python
@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # 只保留可安全序列化的字段：ctx 可能含 ValueError 实例（无法 JSON 序列化），
    # input 会把请求原文（例如密码）回显到响应里
    errors = [{k: v for k, v in err.items() if k in ("type", "loc", "msg")} for err in exc.errors()]
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": _format_validation_errors(exc), "errors": errors}),
    )
```

`_format_validation_errors` yields `"请求参数校验失败：username: 用户名不能包含空白字符"`
(strips `body/query/path` from `loc` and the `"Value error, "` prefix).
History: returning `exc.errors()` raw made custom `field_validator` errors return **500**
(the `ctx` held a `ValueError` instance) and echoed the submitted `input`, including
passwords. Regression test: `backend/tests/test_auth.py::test_custom_validator_error_is_422_without_echo`
(`status == 422`, message present, `"secret123" not in resp.text`).

**Starlette HTTP errors** — default English details are replaced: 404 `资源不存在`,
405 `请求方法不允许`. Unknown `/api/...` paths hit the SPA fallback and return
`{"detail": "接口不存在"}` (`backend/app/main.py`).

**500** — never leaks internals:

```python
@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理的异常", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})
```

---

## Scenario: SSE streaming contract (generate / polish / wizard)

### 1. Scope / Trigger

Applies to `POST /api/v1/chapters/{id}/generate`, `POST /api/v1/chapters/{id}/polish`
and `POST /api/v1/projects/{id}/wizard/{outline|characters|world}`. Cross-layer:
backend `app/api/sse.py` + `generation_service.py` / `wizard_service.py`, frontend
`frontend/src/api/sse.ts` (`postSSE`). Touch this contract when adding any streaming endpoint
or changing event names/payloads.

### 2. Signatures

```python
# backend/app/api/sse.py
def format_sse(event: str, data: dict) -> str: ...
def sse_response(events: AsyncIterator[tuple[str, dict]]) -> StreamingResponse: ...

# backend/app/services/generation_service.py
Event = tuple[Literal["delta", "done", "error"], dict]
async def prepare_chapter_job(db, *, user_id, chapter_id, mode, req, cipher, factory,
                              ai_timeout, allow_private, context_budget) -> ChapterJob
async def run_chapter_job(job: ChapterJob, sessionmaker) -> AsyncIterator[Event]

# backend/app/services/wizard_service.py
async def prepare_wizard_job(db, *, user_id, project_id, kind, req, cipher, factory,
                             ai_timeout, allow_private) -> WizardJob
async def run_wizard_job(job: WizardJob, sessionmaker) -> AsyncIterator[Event]
```

Route shape (`backend/app/api/v1/chapters.py`):

```python
job = await generation_service.prepare_chapter_job(db, user_id=user.id, ...)
await db.close()
return sse_response(generation_service.run_chapter_job(job, sessionmaker))
```

### 3. Contracts

- Transport: **POST only**, JSON body (optional; defaults `GenerateChapterRequest()` etc.).
  The frontend uses `fetch` + `ReadableStream`, not `EventSource`
  (`frontend/src/api/sse.ts`: "后端 SSE 接口均为 POST，无法使用 EventSource").
- Response: `media_type="text/event-stream"`, headers
  `Cache-Control: no-cache, no-transform`, `Connection: keep-alive`, `X-Accel-Buffering: no`.
- First chunk is a comment frame `": stream-start\n\n"`.
- Each event frame: `event: <name>\ndata: <JSON>\n\n`, data via
  `json.dumps(data, ensure_ascii=False)` (JSON keeps newlines in text from breaking frames).
- Events:

| Event | Payload | When |
|-------|---------|------|
| `delta` | `{"text": "<chunk>"}` | each provider chunk |
| `done` (chapter) | `{"chapter": ChapterOut (mode="json")}` | content saved, status `done` |
| `done` (wizard) | `{"kind": "outline"\|"characters"\|"world", "items": [Out...]}` | parsed and saved (old rows of that kind replaced) |
| `error` | `{"detail": "<Chinese>"}` | in-stream failure; always the last event |

- Chapter status lifecycle: `draft|done → generating` (atomic conditional UPDATE in prepare)
  `→ done` on success; on **any** non-success exit (provider error, empty output,
  exception, client disconnect) `run_chapter_job`'s `finally` restores `prev_status`
  and leaves `content` untouched:

```python
finally:
    if not finished:
        # 客户端断开时处于取消作用域内，需屏蔽取消才能完成回退
        with anyio.CancelScope(shield=True):
            await _restore_status(sessionmaker, job.chapter_id, job.prev_status)
```

  `sse_response` wraps the generator in `contextlib.aclosing` so that `finally` runs
  deterministically on disconnect instead of at garbage collection.
- Wizard jobs do not change any status; on parse failure nothing is deleted.

### 4. Validation & Error Matrix

| Condition | Phase | Result |
|-----------|-------|--------|
| not logged in | pre-stream | 401 JSON `未登录或登录已过期` |
| project/chapter missing or not owned | pre-stream | 404 JSON |
| invalid wizard `kind` / body field | pre-stream | 422 JSON |
| no AI config | pre-stream | 400 JSON `尚未配置 AI 模型...` |
| blocked Base URL / key decrypt failure | pre-stream | 400 JSON (from `build_provider`) |
| polish with empty content | pre-stream | 400 JSON `章节正文为空，无法润色` |
| chapter already `generating` | pre-stream | 409 JSON `该章节正在生成中，请稍候再试` |
| provider raises `AIProviderError` mid-stream | in-stream | `error {"detail": str(exc)}`, status restored |
| model returns only whitespace | in-stream | `error {"detail": "模型未返回任何内容"}`, status restored |
| chapter deleted during generation | in-stream | `error {"detail": "章节已被删除，生成结果未保存"}` |
| wizard JSON unparsable | in-stream | `error {"detail": "AI 输出解析失败：...，请重新生成"}`, old data kept |
| unexpected exception | in-stream | logged; `error {"detail": "生成失败：服务器内部错误"}` |
| client disconnects | in-stream | no event; status restored via `aclosing` + shielded `finally` |

Pre-stream errors are ordinary JSON (status != 200), so the frontend checks
`resp.ok` before reading the stream. Once 200 is sent, errors can only be `error` events.

### 5. Good / Base / Bad Cases

- Good: chunks `["夜色深沉，星舰缓缓", ...]` → N `delta` events, then `done` with
  `chapter.status == "done"` and `word_count == count_words(text)`.
- Base: `["部分", AIProviderError("模型调用失败：额度不足")]` → one `delta`, then
  `("error", {"detail": "模型调用失败：额度不足"})`; DB row back to `draft`, content `旧稿`.
- Bad: raising `BadRequestError` inside `run_*_job` — headers are already sent, so it becomes a
  broken stream instead of a 400. All validation belongs in `prepare_*`.

### 6. Tests Required

- `backend/tests/test_chapters.py::test_generate_chapter_sse_persists` — content-type starts with
  `text/event-stream`, `x-accel-buffering == "no"`, `len(deltas) > 1`, `"".join(deltas) == text`,
  last event `done`, persisted content/status.
- `test_generate_error_restores_status` — last event equals the `error` tuple; row `status == "draft"`,
  `content == "旧稿"`.
- `test_generating_conflict` — 409 on generate; 409 on PATCH content while generating; 200 on PATCH `status` only.
- `test_generate_requires_ai_config`, `test_polish` (400 on empty content).
- `backend/tests/test_wizard.py::test_wizard_parse_error` (error + old data kept),
  `test_wizard_invalid_kind` (422), `test_wizard_overwrites_and_keeps_chapters`.
- Parse frames with `tests.helpers.parse_sse`, which skips the `: stream-start` comment.
- Not yet automated: client-disconnect rollback. Any change to `sse_response` or the
  `finally` block must be checked manually or covered by a new test.

### 7. Wrong vs Correct

Wrong — reusing the request session and no deterministic close:

```python
async def gen():
    async for piece in provider.generate_stream(...):
        yield f"data: {piece}\n\n"          # raw text: newlines break frames
    chapter.content = ...; await db.commit()  # request session, already closed/expired
return StreamingResponse(gen(), media_type="text/event-stream")
```

Correct — prepare in the request session, close it, stream with own session and `aclosing`:

```python
job = await generation_service.prepare_chapter_job(db, ...)
await db.close()
return sse_response(generation_service.run_chapter_job(job, sessionmaker))
```

---

## Common Mistakes

- Returning `exc.errors()` directly in a 422 handler (500 on custom validators, echoes passwords).
- Distinguishing "not yours" (403) from "not found" (404) — always 404.
- Putting raw SDK exception text into responses without `describe_error` (unbounded length).
- Raising business errors after a `StreamingResponse` has started.
- Catching `Exception` in a provider without re-raising `AIProviderError` first (double wrapping).
- Gaps in automated coverage today: IntegrityError race in `register`, `describe_error`
  truncation and the generic 500 handler have no dedicated tests; add one when touching them.
