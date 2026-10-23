# 错误处理

> 本项目中错误的处理方式。

---

## 概览

- 业务代码抛出 `backend/app/core/errors.py` 中带类型的 `AppError` 子类，
  从不自行构造 `JSONResponse` 或抛出 `fastapi.HTTPException`。
- `register_error_handlers(app)`（在 `create_app` 中调用）将所有错误映射为
  统一的响应结构：`{"detail": "<中文消息>"}`。
- 模型/provider 失败为 `AIProviderError`（`backend/app/ai/providers/base.py`），
  其 message 已是可安全展示给用户的中文。service 在流式开始前将其转换为
  `BadRequestError`，开始后则转换为 SSE `error` 事件。
- 未知异常会连同堆栈记录日志，并对外屏蔽为 500 `服务器内部错误`。

---

## 错误类型

`backend/app/core/errors.py`（子类各压缩为一行）：

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

各类型的适用场景（真实调用点）：

| 错误 | 用途 | 示例 |
|-------|----------|---------|
| `BadRequestError` | 业务输入非法、缺少前置条件、AI 端点被拦截 | `"尚未配置 AI 模型，请先在「模型配置」中添加并设为默认"`（`ai_config_service.resolve_config`）、`"章节正文为空，无法润色"` |
| `UnauthorizedError` | 无 JWT / JWT 无效或过期、凭据错误 | `"未登录或登录已过期"`（`deps.get_current_user`）、`"用户名或密码错误"` |
| `ForbiddenError` | 功能被配置关闭 | `"注册已关闭，请联系管理员"` |
| `NotFoundError` | 资源不存在**或属于他人** | `"项目不存在"`、`"章节不存在"`、`"模型配置不存在"` |
| `ConflictError` | 唯一性 / 状态冲突 | `"用户名已存在"`、`"该章节正在生成中，请稍候再试"` |

`AIProviderError(Exception)` ——「模型调用失败（网络、鉴权、配额等），message 为可直接展示给用户的中文说明」。
provider 通过 `describe_error` 包装所有 SDK 异常，并截断至 500 字符：

```python
def describe_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if len(text) > 500:
        text = text[:500] + "…"
    return f"模型调用失败：{text}"
```

provider 模式（`backend/app/ai/providers/openai_compat.py`，anthropic/gemini 相同）：

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

`JSONExtractError(ValueError)`（`backend/app/ai/json_extract.py`）表示向导输出无法解析；
它会被转换为 SSE 错误 `"AI 输出解析失败：{exc}，请重新生成"`。

---

## 错误处理模式

### 在掌握语义的边界处转换

`ai_config_service.build_provider` 是唯一一处将解密失败和端点防护失败
转换为 HTTP 错误的地方：

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

始终使用 `from exc` 链接异常。

### 唯一约束竞争 → 409

预检查加约束兜底（`backend/app/services/auth_service.py`）：

```python
db.add(user)
try:
    await db.commit()
except IntegrityError as exc:
    # 并发注册同名用户时由唯一约束兜底
    await db.rollback()
    raise ConflictError("用户名已存在") from exc
```

### 用结果对象代替错误

连通性测试（`POST /ai-configs/{id}/test`）不会因 provider 问题返回 4xx/5xx：
对于端点被拦截、超时（`asyncio.timeout(TEST_TIMEOUT_SECONDS)`，30 秒）、provider 错误
和空回复，它返回 `200` 及 `AIConfigTestResult(ok=False, message=..., latency_ms=...)`
（`ai_config_service.test_config`）。只有归属校验（404）仍会抛出。

### 列表时降级而非崩溃

`ai_config_service.to_out` 捕获解密失败，将 `"（解密失败）"` 作为脱敏后的 key 展示，
而不是让整个列表失败。

---

## API 错误响应

所有 handler 都位于 `register_error_handlers`：

```python
@app.exception_handler(AppError)
async def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
```

**422 校验** —— 在 `detail` 中保留中文摘要，并附带经过清洗的 `errors` 列表：

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

`_format_validation_errors` 生成 `"请求参数校验失败：username: 用户名不能包含空白字符"`
（去掉 `loc` 中的 `body/query/path` 以及 `"Value error, "` 前缀）。
历史：直接返回原始 `exc.errors()` 会导致自定义 `field_validator` 错误返回 **500**
（`ctx` 中含有 `ValueError` 实例），并回显提交的 `input`，包括密码。
回归测试：`backend/tests/test_auth.py::test_custom_validator_error_is_422_without_echo`
（`status == 422`、包含消息、`"secret123" not in resp.text`）。

**Starlette HTTP 错误** —— 替换默认的英文 detail：404 `资源不存在`，
405 `请求方法不允许`。未知的 `/api/...` 路径会命中 SPA 兜底路由并返回
`{"detail": "接口不存在"}`（`backend/app/main.py`）。

**500** —— 绝不泄露内部细节：

```python
@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理的异常", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})
```

---

## 场景：SSE 流式契约 (generate / polish / wizard)

### 1. 范围 / 触发条件

适用于 `POST /api/v1/chapters/{id}/generate`、`POST /api/v1/chapters/{id}/polish`
以及 `POST /api/v1/projects/{id}/wizard/{outline|characters|world}`。跨层：
后端 `app/api/sse.py` + `generation_service.py` / `wizard_service.py`，前端
`frontend/src/api/sse.ts`（`postSSE`）。新增任何流式端点或修改事件名/payload 时，
都需要遵循并更新此契约。

### 2. 签名

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

路由形态（`backend/app/api/v1/chapters.py`）：

```python
job = await generation_service.prepare_chapter_job(db, user_id=user.id, ...)
await db.close()
return sse_response(generation_service.run_chapter_job(job, sessionmaker))
```

### 3. 契约

- 传输：**仅 POST**，JSON body（可选；默认 `GenerateChapterRequest()` 等）。
  前端使用 `fetch` + `ReadableStream`，而非 `EventSource`
  （`frontend/src/api/sse.ts`："后端 SSE 接口均为 POST，无法使用 EventSource"）。
- 响应：`media_type="text/event-stream"`，响应头
  `Cache-Control: no-cache, no-transform`、`Connection: keep-alive`、`X-Accel-Buffering: no`。
- 第一个分块是注释帧 `": stream-start\n\n"`。
- 每个事件帧：`event: <name>\ndata: <JSON>\n\n`，data 通过
  `json.dumps(data, ensure_ascii=False)` 序列化（JSON 可避免文本中的换行破坏帧结构）。
- 事件：

| 事件 | Payload | 时机 |
|-------|---------|------|
| `delta` | `{"text": "<chunk>"}` | provider 每产出一个分块 |
| `done`（章节） | `{"chapter": ChapterOut (mode="json")}` | 正文已保存，状态为 `done` |
| `done`（向导） | `{"kind": "outline"\|"characters"\|"world", "items": [Out...]}` | 已解析并保存（该类型的旧记录被替换） |
| `error` | `{"detail": "<Chinese>"}` | 流内失败；始终是最后一个事件 |

- 章节状态生命周期：`draft|done → generating`（在 prepare 中执行原子条件 UPDATE）
  成功时 `→ done`；在**任何**非成功退出（provider 错误、空输出、
  异常、客户端断开）时，`run_chapter_job` 的 `finally` 恢复 `prev_status`，
  且不改动 `content`：

```python
finally:
    if not finished:
        # 客户端断开时处于取消作用域内，需屏蔽取消才能完成回退
        with anyio.CancelScope(shield=True):
            await _restore_status(sessionmaker, job.chapter_id, job.prev_status)
```

  `sse_response` 用 `contextlib.aclosing` 包裹生成器，使 `finally` 在断开连接时
  确定性地执行，而不是等到垃圾回收时。
- 向导任务不修改任何状态；解析失败时不删除任何数据。

### 4. 校验与错误矩阵

| 条件 | 阶段 | 结果 |
|-----------|-------|--------|
| 未登录 | 流开始前 | 401 JSON `未登录或登录已过期` |
| 项目/章节不存在或不属于当前用户 | 流开始前 | 404 JSON |
| 向导 `kind` / body 字段非法 | 流开始前 | 422 JSON |
| 没有 AI 配置 | 流开始前 | 400 JSON `尚未配置 AI 模型...` |
| Base URL 被拦截 / key 解密失败 | 流开始前 | 400 JSON（来自 `build_provider`） |
| 润色时正文为空 | 流开始前 | 400 JSON `章节正文为空，无法润色` |
| 章节已处于 `generating` | 流开始前 | 409 JSON `该章节正在生成中，请稍候再试` |
| provider 在流中途抛出 `AIProviderError` | 流内 | `error {"detail": str(exc)}`，状态已恢复 |
| 模型只返回空白 | 流内 | `error {"detail": "模型未返回任何内容"}`，状态已恢复 |
| 生成期间章节被删除 | 流内 | `error {"detail": "章节已被删除，生成结果未保存"}` |
| 向导 JSON 无法解析 | 流内 | `error {"detail": "AI 输出解析失败：...，请重新生成"}`，保留旧数据 |
| 意外异常 | 流内 | 记录日志；`error {"detail": "生成失败：服务器内部错误"}` |
| 客户端断开 | 流内 | 无事件；通过 `aclosing` + 屏蔽取消的 `finally` 恢复状态 |

流开始前的错误是普通 JSON（status != 200），因此前端在读取流之前先检查
`resp.ok`。一旦发送了 200，错误就只能以 `error` 事件的形式出现。

### 5. Good / Base / Bad 用例

- Good：分块 `["夜色深沉，星舰缓缓", ...]` → N 个 `delta` 事件，然后是 `done`，且
  `chapter.status == "done"`、`word_count == count_words(text)`。
- Base：`["部分", AIProviderError("模型调用失败：额度不足")]` → 一个 `delta`，然后
  `("error", {"detail": "模型调用失败：额度不足"})`；数据库记录恢复为 `draft`，正文为 `旧稿`。
- Bad：在 `run_*_job` 内抛出 `BadRequestError` —— 响应头已发送，因此结果是
  一个中断的流而不是 400。所有校验都应放在 `prepare_*` 中。

### 6. 必需的测试

- `backend/tests/test_chapters.py::test_generate_chapter_sse_persists` —— content-type 以
  `text/event-stream` 开头、`x-accel-buffering == "no"`、`len(deltas) > 1`、`"".join(deltas) == text`、
  最后一个事件为 `done`、正文/状态已持久化。
- `test_generate_error_restores_status` —— 最后一个事件等于 `error` 元组；记录 `status == "draft"`、
  `content == "旧稿"`。
- `test_generating_conflict` —— generate 返回 409；生成期间 PATCH content 返回 409；仅 PATCH `status` 返回 200。
- `test_generate_requires_ai_config`、`test_polish`（正文为空时返回 400）。
- `backend/tests/test_wizard.py::test_wizard_parse_error`（error + 保留旧数据）、
  `test_wizard_invalid_kind`（422）、`test_wizard_overwrites_and_keeps_chapters`。
- 使用 `tests.helpers.parse_sse` 解析帧，它会跳过 `: stream-start` 注释。
- 尚未自动化：客户端断开时的回退。对 `sse_response` 或
  `finally` 块的任何改动都必须手动验证，或补充新测试覆盖。

### 7. 错误写法 vs 正确写法

错误写法 —— 复用请求 session，且没有确定性的关闭：

```python
async def gen():
    async for piece in provider.generate_stream(...):
        yield f"data: {piece}\n\n"          # 原始文本：换行会破坏帧结构
    chapter.content = ...; await db.commit()  # 请求 session，已关闭/过期
return StreamingResponse(gen(), media_type="text/event-stream")
```

正确写法 —— 在请求 session 中 prepare，关闭它，再用独立 session 和 `aclosing` 进行流式输出：

```python
job = await generation_service.prepare_chapter_job(db, ...)
await db.close()
return sse_response(generation_service.run_chapter_job(job, sessionmaker))
```

---

## 常见错误

- 在 422 handler 中直接返回 `exc.errors()`（自定义校验器会导致 500，并回显密码）。
- 区分「不属于你」（403）与「不存在」（404）—— 一律返回 404。
- 未经 `describe_error` 就把原始 SDK 异常文本放入响应（长度无上限）。
- 在 `StreamingResponse` 开始后抛出业务错误。
- 在 provider 中捕获 `Exception` 前没有先重新抛出 `AIProviderError`（导致重复包装）。
- 当前自动化覆盖的缺口：`register` 中的 IntegrityError 竞争、`describe_error`
  截断以及通用 500 handler 都没有专门的测试；改动它们时请补充测试。
