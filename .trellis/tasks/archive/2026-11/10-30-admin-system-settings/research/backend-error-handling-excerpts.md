# 规范摘录

权威来源：`.trellis/spec/backend/error-handling.md`。仅截取本任务相关段落；完整规范以原文件为准。

## 原文第 1–125 行

# 错误处理

管理员跨用户任务投影是明确的安全边界例外：普通所有者可见的provider错误不等于已脱敏的管理摘要。`/admin/tasks`根本不读取Task.error或原Task.title，仅按真实状态/pending输出有限类别与固定说明；原始阶段也须固定模板化。异步取消的202只代表“已受理”，不能把等待超时/收尾失败伪造为已取消；详见[管理员任务监控](./admin-task-monitoring.md)。

> 本项目中错误的处理方式。

---

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
