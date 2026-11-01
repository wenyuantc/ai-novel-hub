# 本任务相关规范摘录：backend/error-handling.md

来源：`.trellis/spec/backend/error-handling.md`

规划时 SHA-256：`b1159b2c23befdb0724f7aa929d3e1557fc7b9009b30de11c8be9de8186d4b9e`

仅按原文摘录相关段落，完整规范是权威来源。源文件有变化时请重读相应段落；不得把摘录当作新增规范。

## 原文 L9–L45

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

## 原文 L75–L88


**结构化附加字段**：`AppError(detail, extra={...})` 的 `extra` 会合并进响应体（`detail` 始终存在）。目前用于
模板校验 400 的 `errors: [{part, line, message}]` 与删除被引用风格 409 的 `references: {projects, chapters}`（见 [prompts-styles.md](./prompts-styles.md)）。
`TemplateRenderError(BadRequestError)`：运行时渲染用户覆盖失败；请求内 → 400，任务内 → failed。

`JSONExtractError(ValueError)`（`backend/app/ai/json_extract.py`）表示向导输出无法解析；
它会被转换为 SSE 错误 `"AI 输出解析失败：{exc}，请重新生成"`。

---

## 错误处理模式

管理员审计写边界新增 `app/services/audit_writer.py::AuditOperationError(AppError)`，status_code=500、固定中文detail；其未知系统异常先安全转换再交AppError handler，不让可能含正文/密码SQL参数的原异常进入全局 `logger.exception`。提交后的投影异常同样安全500但不追加业务失败；业务版本flush须保留精确409。仅此敏感边界采用安全运维日志例外，其他全局错误契约不变；详见[管理员操作审计](./admin-audit.md)。


## 原文 L109–L124

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


## 原文 L139–L184

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


## 原文 L468–L476

## 常见错误

- 在 422 handler 中直接返回 `exc.errors()`（自定义校验器会导致 500，并回显密码）。
- 区分「不属于你」（403）与「不存在」（404）—— 一律返回 404。
- 未经 `describe_error` 就把原始 SDK 异常文本放入响应（长度无上限）。
- 在 `StreamingResponse` 开始后（或任务已返回 202 后）抛出业务错误。
- 在 provider 中捕获 `Exception` 前没有先重新抛出 `AIProviderError`（导致重复包装）。
- 当前自动化覆盖的缺口：`register` 中的 IntegrityError 竞争、`describe_error`
  截断以及通用 500 handler 都没有专门的测试；改动它们时请补充测试。
