# 备份任务错误边界规范摘录

本任务规划时的规范摘录；原规范为权威来源。原文件变化后按来源重新读取相关条款。

来源：`.trellis/spec/backend/error-handling.md:9`，行 9–84；原文件 SHA-256：`b1159b2c23befdb0724f7aa929d3e1557fc7b9009b30de11c8be9de8186d4b9e`。

## 概览

- 业务代码抛出 `backend/app/core/errors.py` 中带类型的 `AppError` 子类，
  从不自行构造 `JSONResponse` 或抛出 `fastapi.HTTPException`。
- `register_error_handlers(app)`（在 `create_app` 中调用）将所有错误映射为
  统一的响应结构：`{"detail": "<中文消息>"}`。
- 模型/provider 失败为 `AIProviderError`（`backend/app/ai/providers/base.py`），
  其 message 已是可安全展示给用户的中文。service 在创建任务 / 流式开始前将其转换为
  `BadRequestError`，运行后则转换为 runner 的 `error` 事件（任务 failed，订阅方收到 `error`）。
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

**结构化附加字段**：`AppError(detail, extra={...})` 的 `extra` 会合并进响应体（`detail` 始终存在）。目前用于
模板校验 400 的 `errors: [{part, line, message}]` 与删除被引用风格 409 的 `references: {projects, chapters}`（见 [prompts-styles.md](./prompts-styles.md)）。
`TemplateRenderError(BadRequestError)`：运行时渲染用户覆盖失败；请求内 → 400，任务内 → failed。

`JSONExtractError(ValueError)`（`backend/app/ai/json_extract.py`）表示向导输出无法解析；
它会被转换为 SSE 错误 `"AI 输出解析失败：{exc}，请重新生成"`。

---


来源：`.trellis/spec/backend/error-handling.md:85`，行 85–138；原文件 SHA-256：`b1159b2c23befdb0724f7aa929d3e1557fc7b9009b30de11c8be9de8186d4b9e`。

## 错误处理模式

管理员审计写边界新增 `app/services/audit_writer.py::AuditOperationError(AppError)`，status_code=500、固定中文detail；其未知系统异常先安全转换再交AppError handler，不让可能含正文/密码SQL参数的原异常进入全局 `logger.exception`。提交后的投影异常同样安全500但不追加业务失败；业务版本flush须保留精确409。仅此敏感边界采用安全运维日志例外，其他全局错误契约不变；详见[管理员操作审计](./admin-audit.md)。

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

关系写入还有前置 flush，不能只包 commit，也不能把所有 IntegrityError 翻译成同一种冲突。`relationship_service` 分别精确识别角色对唯一键和类型唯一键；低层只翻译，HTTP请求或既有外层SAVEPOINT负责回滚。类型首次创建竞争给可重试的独立409，不加入可能破坏SQLite整批回滚的裸SAVEPOINT。签名、矩阵与回归见[AI关系生成契约](./relationship-generation.md)。

### 用结果对象代替错误

连通性测试（`POST /ai-configs/{id}/test`）不会因 provider 问题返回 4xx/5xx：
对于端点被拦截、超时（`asyncio.timeout(TEST_TIMEOUT_SECONDS)`，30 秒）、provider 错误
和空回复，它返回 `200` 及 `AIConfigTestResult(ok=False, message=..., latency_ms=...)`
（`ai_config_service.test_config`）。只有归属校验（404）仍会抛出。

### 列表时降级而非崩溃

`ai_config_service.to_out` 捕获解密失败，将 `"（解密失败）"` 作为脱敏后的 key 展示，
而不是让整个列表失败。

---


来源：`.trellis/spec/backend/error-handling.md:139`，行 139–184；原文件 SHA-256：`b1159b2c23befdb0724f7aa929d3e1557fc7b9009b30de11c8be9de8186d4b9e`。

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
