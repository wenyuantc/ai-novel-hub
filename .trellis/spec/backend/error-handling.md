# 错误处理

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

## 场景：后台任务与 SSE 契约 (generate / polish / wizard 走任务；rewrite 仍为同步 SSE)

### 1. 范围 / 触发条件

- **后台任务**（`10-23-task-engine`）：`POST /api/v1/chapters/{id}/generate`、`POST /api/v1/chapters/{id}/polish`、
  `POST /api/v1/projects/{id}/wizard/{outline|characters|world|careers}` 都**返回 202 `TaskOut`**（普通 JSON），
  实际执行在进程内任务引擎（`app/services/task_engine/`）中进行；设定类任务 `POST /projects/{id}/characters/generate`、
  `/organizations/generate`、`/characters/{cid}/parse-relations`、`/careers/generate`（`character.generate` / `organization.generate` / `relations.parse` / `career.generate`，
  只产出草稿，见 [story-entities.md](./story-entities.md)）同样返回 202；批量顺序生成 `POST /projects/{pid}/batch-generate`（`chapter.batch`，多步 + 待审核，见 [batch-generate.md](./batch-generate.md)）同样返回 202；大纲续写 / 展开 `POST /projects/{pid}/outlines/continue`、
  `/outlines/{oid}/expand`（`outline.continue` / `outline.expand`，只产出草稿，见 [outline.md](./outline.md)）同样返回 202；章节分析 `POST /chapters/{cid}/analyze`（`chapter.analyze`，同章已有排队中的分析时
  返回那条任务、状态码 200）、按建议重写 `POST /chapters/{cid}/revise`（`chapter.revise`）也走任务，见 story-entities.md「章节分析与回写」；实时输出通过
  `GET /api/v1/tasks/{id}/stream`（GET SSE）订阅。关闭页面 / 断开订阅**不会**中断任务。
- **同步 SSE**：`POST /api/v1/chapters/{id}/rewrite`（局部改写预览，D1：不进任务引擎），以及
  `POST /api/v1/styles/extract`、`POST /api/v1/styles/trial`（风格提取 / 试写：`delta` → `done` / `error`，不写生成记录、无项目归属，见 prompts-styles.md）。
  `POST /api/v1/inspiration/sessions/{id}/steps/{step}/generate`（灵感模式生成选项：`delta` →（`retry` → `delta`）… → `done` / `error`，
  请求内重试、断开不落库、同会话同步骤并发 409，见 [inspiration.md](./inspiration.md)）。
- 跨层：后端 `app/api/sse.py`、`task_engine/{engine,hub,registry}.py`、`handlers/{chapter,wizard}.py`、
  `generation_service.py` / `wizard_service.py` / `rewrite_service.py`、`task_service.py`、`api/v1/tasks.py`；
  前端 `frontend/src/api/sse.ts` 与任务订阅。新增任务类型、修改订阅事件名 / payload、新增流式端点时都要遵循并更新此契约。

### 2. 签名

```python
# backend/app/api/sse.py
def format_sse(event: str, data: dict) -> str: ...
def sse_response(events: AsyncIterator[tuple[str, dict]]) -> StreamingResponse: ...   # event == "" → ": ping\n\n"

# backend/app/services/task_engine/registry.py —— 新任务类型实现该协议并 register()
class TaskHandler(Protocol):
    kinds: tuple[str, ...]
    async def validate_and_claim(self, db, ctx, *, user_id, kind, params: dict) -> TaskSpec  # 创建请求内，不 commit
    async def build_job(self, ctx, task: Task) -> Any            # 运行开始时；抛 AppError → 任务 failed
    def run(self, job, ctx) -> AsyncIterator[Event]              # delta / continue / done / error
    def progress(self, task: Task, text: str) -> int             # 0–95
    async def on_abort(self, ctx, task: Task) -> None            # 幂等，自身吞异常
    # 可选：async def on_success(self, ctx, task: Task) -> None  # succeeded 写库后、广播 done 前；异常只记日志
# TaskSpec(project_id, chapter_id, resource_key, title, input, serial_key=None)；input 必须含原始请求 "request"（重试时原样复用）
# resource_key 冲突 → 409 拒绝；serial_key 冲突 → 排队等待（同键串行、按 id 先进先出，见 database-guidelines.md「后台任务表」）

# backend/app/services/task_engine/engine.py
class TaskEngine:  # app.state.task_engine；lifespan 中 start()/stop()
    async def start(self) -> None          # 先 recover()（遗留 running → interrupted），再启动调度协程
    async def stop(self) -> None           # 停止调度；运行中任务标记关停后 cancel → interrupted
    def notify(self) -> None
    async def cancel_running(self, task_id) -> None
    async def subscribe(self, task_id) -> AsyncIterator[Event]   # 快照 + 增量

# backend/app/services/task_service.py —— HTTP 入口（全部按 user_id 校验归属）
async def create_task(db, engine, user_id, kind, params, *, retry_of=None) -> TaskOut
async def list_tasks / get_task / get_output / cancel_task / retry_task / delete_task / clear_finished
async def prune_finished(db, user_id) -> None     # MAX_FINISHED_PER_USER = 200

# backend/app/services/generation_service.py
Event = tuple[Literal["delta", "continue", "done", "error"], dict]
CONTINUE_RATIO = 0.85; MAX_CONTINUE_ROUNDS = 2
async def validate_and_claim(db, *, user_id, chapter_id, mode, req, cipher, factory, ai_timeout, allow_private) -> ChapterClaim
async def build_chapter_job(db, *, user_id, chapter_id, mode, req, prev_status, ai_config_id, cipher, factory,
                            ai_timeout, allow_private, context_budget, task_id=None) -> ChapterJob
async def run_chapter_job(job: ChapterJob, sessionmaker) -> AsyncIterator[Event]
async def stream_pieces(provider, messages, model, *, max_tokens, thinking, pieces, prefix="") -> AsyncIterator[Event]

# backend/app/services/wizard_service.py
async def validate_wizard(db, *, user_id, project_id, req, cipher, factory, ai_timeout, allow_private) -> int  # ai_config_id
async def build_wizard_job(db, *, user_id, project_id, kind, req, ai_config_id, cipher, factory, ai_timeout,
                           allow_private) -> WizardJob
async def run_wizard_job(job: WizardJob, sessionmaker) -> AsyncIterator[Event]

# backend/app/services/rewrite_service.py（同步 SSE，不变）
async def prepare_rewrite_job(db, *, user_id, chapter_id, req: RewriteRequest, cipher, factory,
                              ai_timeout, allow_private) -> RewriteJob
async def run_rewrite_job(job: RewriteJob, sessionmaker) -> AsyncIterator[Event]
async def apply_rewrite(db, user_id, project_id, chapter_id, data: RewriteApply) -> Chapter  # 普通 JSON
```

路由形态（`backend/app/api/v1/chapters.py` / `tasks.py`）：

```python
# 创建任务：同步校验 + 抢占，202
params = {"chapter_id": chapter_id, **(body or GenerateChapterRequest()).model_dump(mode="json")}
return await task_service.create_task(db, engine, user.id, "chapter.generate", params)

# 订阅：先校验归属，再释放请求 session
await svc.get_owned_task(db, user.id, task_id)
await db.close()
return sse_response(engine.subscribe(task_id))
```

### 3. 契约

**帧格式**（所有 SSE 端点相同）

- 响应：`media_type="text/event-stream"`，响应头 `Cache-Control: no-cache, no-transform`、`Connection: keep-alive`、
  `X-Accel-Buffering: no`；第一个分块是注释帧 `": stream-start\n\n"`。
- 每个事件帧：`event: <name>\ndata: <JSON>\n\n`，data 通过 `json.dumps(data, ensure_ascii=False)` 序列化。
- 任务订阅空闲时每 `engine.heartbeat_seconds`（15 秒）发注释帧 `": ping\n\n"`（生成器产出 `("", {})`）。

**任务生命周期**

```
create ──> queued ──claim──> running ──> succeeded
             │                  ├──> failed        (runner 产出 error、build_job 抛 AppError 或意外异常)
             │                  ├──> cancelled     (POST /tasks/{id}/cancel)
             │                  └──> interrupted   (engine.stop()，或进程崩溃后下次 start() 恢复)
             │                  └──> paused        (多步任务产出 paused：审核点；见 batch-generate.md)
             └──cancel──> cancelled
paused ──resume──> queued（同一任务，断点保留）；paused ──finish──> succeeded（ended_early）；paused ──cancel──> cancelled
failed / cancelled / interrupted ──retry──> 新任务(queued, retry_of_id=原任务, step_cursor / checkpoint 继承)
```

- **待审核（`paused`）**：不属于 ACTIVE（不被调度、不占并发名额、不占 `resource_key` 部分唯一索引），也不属于 FINISHED（不被裁剪、
  不能删除 → 409 `任务待审核，无法删除，请先结束或取消`、不能重试）。收尾时**不调用** `on_abort` / `on_success`，写 `status=paused`、
  `stage=待审核`、`result=paused 载荷`、`finished_at` 留空，hub 以 `paused` 事件关闭；取消落在 `paused` 产出之后仍按 paused 收尾。
  `recover()` 只处理 running。`POST /tasks/{id}/resume`（202）：handler 可选 `revalidate_resume` → 条件 UPDATE `paused → queued`
  （清 `result / error / finished_at`，`IntegrityError` → 409）→ 建 hub + notify；`POST /tasks/{id}/finish`：条件 UPDATE → succeeded，
  `result` 取 handler 可选 `finish_result(task)`（默认原 result + `ended_early: true`）；`cancel` 支持 paused（条件 UPDATE 后 `on_abort`）。
  `GET /tasks?active=true` 返回 `OPEN_TASK_STATUSES`（含 paused）。非 paused 时 resume / finish → 409 `只有待审核的任务可以继续` / `结束`。
- **重试钩子**：handler 可选 `validate_retry(db, ctx, *, user_id, task) -> TaskSpec`，有则代替 `validate_and_claim`（多步任务沿用原范围）。

- 创建：`validate_and_claim` 在请求 session 内完成鉴权、`resolve_config` + `build_provider`（SSRF / 解密校验）、参数校验，
  章节类再原子抢占 `generating`；与任务插入**同一事务** commit。撞部分唯一索引 `uq_tasks_active_resource`
  → 回滚（章节抢占随之撤销）→ 409 `已有任务在进行中，请等待其完成后再试`。
- 章节类任务**创建即抢占**：排队期间章节就是 `generating`（编辑器只读）。`input.prev_status` 记录原状态。
- 调度：按 id 升序扫描 queued；全局运行数 ≥ `TASK_MAX_CONCURRENCY`（默认 4）停止，用户运行数 ≥ `TASK_MAX_PER_USER`
  （默认 2）跳过；`serial_key` 同键已有运行中的任务也跳过（同键内先进先出）。先登记 `asyncio.Task` 再做条件 UPDATE
  （`status='queued'` → `'running'`），影响行数为 1 才执行。
- 运行：`build_job` 按**运行时的最新数据**组装（章节上下文、向导的世界观/角色/大纲）；章节已不处于 `generating`
  （排队期间被复位）→ failed `章节生成状态已被复位，任务未执行`，不覆盖正文。之后用 `aclosing` 消费 runner 事件：
  `delta` 追加到 hub 并广播；`continue` 原样广播并把 stage 设为 `字数不足，自动续写第 n 轮`；`done` → succeeded（progress 100）；
  `error` → failed。`output_text` / progress / stage 每 `flush_interval`（2 秒）节流刷库一次，结束时再写一次。
- 收尾：非成功先 `handler.on_abort`（章节：仍为 `generating` 时还原 `prev_status`），再写终态 + `finished_at`，
  `prune_finished`；成功时再调用可选的 `handler.on_success`（章节 generate / polish / revise → `analysis_service.enqueue_auto`，
  异常只记日志），最后广播终止事件并释放 hub。`done` 已产出后才收到取消 → 仍记 succeeded。
- 取消：queued → 条件 UPDATE 为 cancelled，**之后**再 `on_abort`（避免与领取竞争）；running → 对 `asyncio.Task`
  调用一次 `cancel()`，`CancelledError` 传入 runner 的 `finally`（屏蔽取消回退章节、写 `cancelled` 生成记录），
  引擎 `uncancel()` 后照常收尾为 cancelled；已结束 → 409 `任务已结束，无法取消`。
- 重启（D2）：`stop()` 把运行中任务标记为关停中断再 cancel → `interrupted`，`error = "服务重启，任务已中断"`，
  `output_text` 保留；`start()` 先把库里遗留的 `running` 全部置为 interrupted 并 `on_abort`；queued 不受影响，照常调度。
  **前提：单进程部署**（见 `database-guidelines.md`「后台任务表」）。
- 重试：仅 `failed|cancelled|interrupted`，否则 409 `只有失败、已取消或已中断的任务可以重试`；用原 `input.request`
  重新走 `validate_and_claim`（重新校验配置、重新抢占章节）。

**订阅事件**（`GET /tasks/{id}/stream`）

| 事件 | Payload | 时机 |
|-------|---------|------|
| `snapshot` | `{"task": TaskOut, "text": <已输出全文>}` | 订阅后第一帧 |
| `delta` | `{"text": "<chunk>"}` | 新增输出；续写轮首个分块前有一个 `{"text": "\n\n"}` |
| `continue` | `{"round": n, "words": <当前字数>, "target": <target_words>}` | 仅整章生成，自动续写每轮开始前，n 从 1 起最多 2 |
| `progress` | `{"status", "progress", "stage", "queue_position"?}` | 状态 / 进度 / 阶段变化；排队中时含 `queue_position`，位置变化也会推送 |
| `done` | `{"task": TaskOut, "result": <runner done 载荷>}` | 成功；之后流结束 |
| `error` | `{"task": TaskOut, "detail": "<中文>"}` | 失败 / 取消 / 中断；之后流结束 |
| `paused` | `{"task": TaskOut, "result": <paused 载荷>}` | 多步任务到达审核点（待审核）；之后流结束 |
| `reset` | `{}` | 清空实时输出（多步任务开始新的一步；客户端清空本地文本） |
| 其他（如 `chapter_start` / `chapter_end`） | handler 自定义 | 原样广播（批量生成见 batch-generate.md） |

- runner 的 `stage` 事件不直接转发，只更新 hub 的阶段文案（经 `progress` 事件推送）。

- 章节 `result`：`{"chapter": ChapterOut, "warning"?: "自动续写失败：…"}`；向导 `result`：`{"kind", "items": [Out...]}`。
- 不重复不遗漏：`TaskHub.subscribe()` 在同一同步临界区内复制缓冲区并注册订阅队列（中间没有 `await`），
  快照之后的第一个 delta 恰好接在快照末尾。
- 已结束 / 待审核任务：快照取自库里的 `output_text`，紧接着 `done` / `error` / `paused`，然后关闭。
- 任务行已不存在（归属校验后被删除；或订阅中的排队任务随用户 / 项目被级联删除——在心跳间隔检查）：发
  `error {"task": null, "detail": "任务不存在"}` 后结束（前者没有 `snapshot`），并释放该任务的 hub。
- 订阅队列上限 1000 帧；溢出时断开该订阅者（前端重连，用 `snapshot` 覆盖本地文本）。
- 客户端断开只移除该订阅者（`aclosing` 关闭生成器 → `finally` 中 unsubscribe），任务继续。

**runner 语义（不变）**

- 章节：成功时**同一事务** `ensure_baseline → 覆盖正文 → write_log(success, task_id) → record(generate|polish, log_id)`；
  非成功退出时正文与版本都不变，在屏蔽取消的 `finally` 中回退状态并用独立 session 写 `status=error|cancelled` 的记录
  （没有被 except 分支改写的失败即视为 `cancelled`）。生成记录带 `task_id`（`GenerationLogBrief.task_id`）。
- 自动续写（D3）：`GenerateChapterRequest.auto_continue`（**接口默认 `false`**；编辑器显式传 `true`）。首轮
  `count_words < 0.85 × target_words` 时最多续写 2 轮：消息为原消息 + `assistant(已生成正文)` + `user(续写指令)`，
  结果以 `"\n\n"` 拼接。续写返回空白 → 停止；续写轮报错 → 丢弃本轮半截内容，已生成正文照常落库，`warning = "自动续写失败：…"`，
  生成记录 `status=success`、`error` 存同一警告。润色不续写。
- 向导：不修改任何状态；解析成功覆盖该类型旧数据；解析失败不删除任何数据。向导不写生成记录（与原行为一致）。

**局部改写预览（同步 SSE，不变）**

- `POST /chapters/{id}/rewrite`（仅 POST、JSON body）：**不抢占 `generating`、不改正文**，事件 `delta` / `done` / `error`；
  `done` = `{"text", "start", "end", "original", "before"（≤200 码点）, "log_id"}`，并写一条生成记录（`kind = op`，`task_id` 为空）。
  客户端断开 → `sse_response` 的 `aclosing` 让 `finally` 确定性执行。
- 采纳走普通 JSON `POST /projects/{pid}/chapters/{cid}/rewrite/apply`：`content[start:end] != original`（或越界）→ 409
  `正文已变化，请重新选择后再改写`；传入 `before` 时还要求 `start >= len(before)` 且 `content[start-len(before):start] == before`，
  否则同样 409；章节生成中 → 409；`log_id` 不属于本章时忽略。偏移一律为**码点**偏移。

### 4. 校验与错误矩阵

| 条件 | 阶段 | 结果 |
|-----------|-------|--------|
| 未登录 | 创建 / 任务接口 | 401 JSON `未登录或登录已过期` |
| 项目/章节不存在或不属于当前用户 | 创建前 | 404 JSON |
| 任务不存在或不属于当前用户（详情 / output / stream / cancel / retry / delete） | 请求时 | 404 JSON `任务不存在` |
| 向导 `kind` / body 字段非法、列表 `kind` 非法 | 创建前 | 422 JSON |
| 没有 AI 配置 / Base URL 被拦截 / key 解密失败 | 创建前 | 400 JSON（不建任务、不抢占） |
| 润色时正文为空 | 创建前 | 400 JSON `章节正文为空，无法润色` |
| 章节已处于 `generating`（含已有排队 / 运行中的章节任务） | 创建前 | 409 JSON `该章节正在生成中，请稍候再试` |
| 章节在排队中 / 运行中批量的剩余范围内（generate / polish / revise） | 创建前 | 409 `该章节在批量生成范围内，请等待批量完成或暂停后再操作` |
| 同项目同类向导已有活动任务（部分唯一索引） | 创建时 | 409 JSON `已有任务在进行中，请等待其完成后再试` |
| 取消已结束任务 | 请求时 | 409 `任务已结束，无法取消` |
| 重试非失败/取消/中断任务 | 请求时 | 409 `只有失败、已取消或已中断的任务可以重试` |
| 删除进行中任务 | 请求时 | 409 `任务进行中，无法删除，请先取消` |
| 排队期间章节被复位 | 运行开始 | 任务 failed `章节生成状态已被复位，任务未执行` |
| 运行开始时配置 / 章节已删除 | 运行开始 | 任务 failed（`模型配置不存在` / `章节不存在` 等 AppError detail） |
| provider 中途抛 `AIProviderError` | 运行中 | 任务 failed，`error {"detail": str(exc)}`，章节状态已恢复 |
| 模型只返回空白 | 运行中 | failed `模型未返回任何内容` |
| 生成期间章节被删除 | 运行中 | failed `章节已被删除，生成结果未保存` |
| 自动续写轮报错 | 运行中 | succeeded，`result.warning` |
| 向导 JSON 无法解析 | 运行中 | failed `AI 输出解析失败：...，请重新生成`，保留旧数据 |
| 意外异常 | 运行中 | failed `生成失败：服务器内部错误`（runner 内）/ `任务执行失败：服务器内部错误`（引擎兜底） |
| 分析：章节正文为空 | 创建前 / 运行开始 | 400 / failed `章节正文为空，无法分析` |
| 分析：模型输出无法解析 | 运行中 | failed `AI 输出解析失败：...，请重新生成`，不保存分析、不回写 |
| 分析：自动入队的任务运行时没有可用模型配置 | 运行开始 | failed（`resolve_config` 的 detail） |
| 重写：未选建议也未填要求 / 建议 id 不存在 / 分析不属于本章 / 正文为空 | 创建前 | 400 `请至少选择一条建议或填写修改要求` / `建议不存在：N` / `分析结果不存在或不属于该章节` / `章节正文为空，无法重写` |
| 用户取消 | 运行中 | cancelled `任务已取消`，生成记录 `cancelled` |
| 服务停止 / 崩溃后重启 | 运行中 | interrupted `服务重启，任务已中断`，保留 `output_text` |
| 改写：选区 / op 非法 | 流开始前 | 400 / 422 JSON（见 `rewrite_service`）；改写意外异常 → `error {"detail": "改写失败：服务器内部错误"}` |
| 请求 `style` 指定了不可见的风格（generate / polish / revise / rewrite / batch） | 创建前 / 流开始前 | 400 `指定的写作风格不存在或不可用`（不建任务、不抢占） |
| 用户覆盖的模板运行时渲染失败 | 运行开始 | failed `提示词模板「X」模板渲染失败：…，请检查或恢复默认` |

### 5. Good / Base / Bad 用例

- Good：`POST /chapters/1/generate` → 202 `{"status": "queued", ...}`；订阅得到 `snapshot{text:""}` → 多个 `delta` →
  `done{result.chapter.status == "done"}`；`snapshot.text + "".join(deltas) == 正文`。
- Base：`["部分", AIProviderError("模型调用失败：额度不足")]` → `error {"detail": "模型调用失败：额度不足"}`；
  任务 failed，`/output` 为 `部分`；章节恢复为 `draft`，正文为 `旧稿`。
- Bad：在 `run` / `build_job` 之外的异步路径抛业务错误给已经返回 202 的请求——此时只能让任务 failed。所有可以同步判定的
  校验都应放在 `validate_and_claim` 中。另一个 Bad：在 runner 里自己 catch `CancelledError` 并吞掉（取消将无法停止流）。

### 6. 必需的测试

- `tests/helpers.py`：`Gate`（FakeLLM 分片中的阻塞点，`wait_reached()` / `release()`）、`FakeLLM.aborted`（流被中途关闭的次数）、
  `wait_task`、`stream_task`、`run_task`（202 → 订阅至结束）、`stream_with_gate`（先挂订阅再放行，保证全部 delta 经由流到达）、
  `streamed_text`、`final_event`。`app` fixture 显式 `task_engine.start()`，结束前 `stop()`。
- `tests/test_chapters.py`：`test_generate_chapter_task_persists`（202、创建即 `generating`、标题、事件流头、`len(deltas) > 1`、
  done 载荷、终态 progress 100、`/output`）、`test_generate_error_restores_status`、`test_generating_conflict`（generate/polish 409 +
  生成中 PATCH content 409 / 只 PATCH status 200）、`test_generate_requires_ai_config`（不建任务）、`test_polish`、`test_explicit_ai_config_id`。
- `tests/test_wizard.py`：outline（delta 拼接 + 结果）、覆盖并保留章节、characters + world、解析失败（failed + 保留旧数据）、非法 kind 422。
- `tests/test_chapter_versions.py`：baseline + generate 版本与日志（`task_id`）关联、失败不写版本只写 error 记录、
  `test_generate_cancelled_logs_cancelled`（直接驱动 `validate_and_claim` + `build_chapter_job` + `run_chapter_job` 后 `aclose()`）、
  自动续写（`continue` payload、stage 文案、拼接、最多 2 轮、默认关闭、续写失败 warning）。
- `tests/test_tasks.py`：hub 快照无缝、两个订阅者 + 已结束快照、心跳、断开不影响任务（章节 + 向导）、取消运行中（<1 秒、流中断、
  状态复位、记录 cancelled）/ 排队中（不再执行）、stop → interrupted 且 queued 继续 + 重试关联原任务、崩溃恢复、排队期间复位、
  每用户上限 + queue_position、全局上限 + 位置前移、资源互斥 409、列表过滤 / 删除 / 清理、越权 404、保留裁剪 200、订阅中任务被删（`error {task: null}`）。
- `tests/test_rewrite.py`（同步 SSE，不变）；`tests.helpers.parse_sse` 会跳过注释帧（含 `: ping`）。

### 7. 错误写法 vs 正确写法

错误写法 —— 在请求里直接跑模型，断开即中断：

```python
async def gen():
    async for piece in provider.generate_stream(...):
        yield f"data: {piece}\n\n"          # 原始文本：换行会破坏帧结构
return StreamingResponse(gen(), media_type="text/event-stream")
```

正确写法 —— 创建任务（同步校验），由引擎执行，页面只订阅：

```python
return await task_service.create_task(db, engine, user.id, "chapter.generate", params)   # 202 TaskOut
# 前端：GET /api/v1/tasks/{id}/stream → snapshot / delta / continue / progress / done|error
```

---

## 常见错误

- 在 422 handler 中直接返回 `exc.errors()`（自定义校验器会导致 500，并回显密码）。
- 区分「不属于你」（403）与「不存在」（404）—— 一律返回 404。
- 未经 `describe_error` 就把原始 SDK 异常文本放入响应（长度无上限）。
- 在 `StreamingResponse` 开始后（或任务已返回 202 后）抛出业务错误。
- 在 provider 中捕获 `Exception` 前没有先重新抛出 `AIProviderError`（导致重复包装）。
- 当前自动化覆盖的缺口：`register` 中的 IntegrityError 竞争、`describe_error`
  截断以及通用 500 handler 都没有专门的测试；改动它们时请补充测试。
