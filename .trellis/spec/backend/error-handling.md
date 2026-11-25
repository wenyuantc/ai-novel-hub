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
  `/outlines/{oid}/expand`（`outline.continue` / `outline.expand`，只产出草稿，见 [outline.md](./outline.md)）同样返回 202；知识库上传 / 重建 `POST /projects/{pid}/kb/documents`、`/kb/documents/{id}/reindex`、`/kb/rebuild`（`kb.index`，
同项目串行、没有向量模型时仍成功并带 warning，见 [retrieval.md](./retrieval.md)）同样返回 202；章节分析 `POST /chapters/{cid}/analyze`（`chapter.analyze`，同章已有排队中的分析时
  返回那条任务、状态码 200）、按建议重写 `POST /chapters/{cid}/revise`（`chapter.revise`）也走任务，见 story-entities.md「章节分析与回写」；拆书导入（`book.import`，见 [book-import.md](./book-import.md)）同样走任务；项目智能体发送消息
  `POST /projects/{pid}/agent/conversations/{cid}/messages`（`agent.turn`，202 `{message, task}`；工具调用循环、待确认时 `paused`、确认后自动续跑，
  自定义事件 `tool_start` / `tool_end` / `proposal` / `executed`，见 [agent.md](./agent.md)）也走任务；实时输出通过
  `GET /api/v1/tasks/{id}/stream`（GET SSE）订阅。关闭页面 / 断开订阅**不会**中断任务。
- **同步 SSE**：`POST /api/v1/chapters/{id}/rewrite`（局部改写预览，D1：不进任务引擎），以及
  `POST /api/v1/styles/extract`、`POST /api/v1/styles/trial`（风格提取 / 试写：`delta` → `done` / `error`，不写生成记录、无项目归属，见 prompts-styles.md）。
  `POST /api/v1/inspiration/sessions/{id}/steps/{step}/generate`（灵感模式生成选项：`delta` →（`retry` → `delta`）… → `done` / `error`，
  请求内重试、断开不落库、同会话同步骤并发 409，见 [inspiration.md](./inspiration.md)）。
- 跨层：后端 `app/api/sse.py`、`task_engine/{engine,hub,registry}.py`、`handlers/{chapter,wizard}.py`、
  `generation_service.py` / `wizard_service.py` / `rewrite_service.py`、`task_service.py`、`api/v1/tasks.py`；
  前端 `frontend/src/api/sse.ts` 与任务订阅。新增任务类型、修改订阅事件名 / payload、新增流式端点时都要遵循并更新此契约。

- **向导从中断处继续**（`10-27-wizard-resume`）：`WizardRequest.continue_from_task_id` 引用同用户、同项目、同 kind、
  状态 failed / cancelled / interrupted 且 `output_text` 含完整对象（`}`）的向导任务，否则 400（`要继续的向导任务不存在` /
  `只有失败、已取消或已中断的向导任务可以从中断处继续` / `该任务没有可续写的部分输出，请重新生成`）。新任务参数取旧任务
  `input.request`（只有 `ai_config_id` 用本次值），`TaskSpec.retry_of_id` → `tasks.retry_of_id` 指向旧任务。运行时：`stage`「从中断处继续」→
  先把截断后的前缀作为 `delta` 输出（新任务 `output_text` 是完整累积文本，再失败可链式继续）→ 续写开头缓冲 600 字符后清洗
  （去 ``` 标记、去与前缀末尾 ≥8 字符的重复、`}` 后接 `{` 补逗号；模型从头重来则只用续写部分）→ 拼接后照常 `extract_json` 落库。
  前端 `WizardGenerator` 挂载时取同 kind 最近一条任务：进行中继续订阅，failed / interrupted 则恢复错误与部分输出并显示「从中断处继续」。

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
# 从中断处继续（WizardRequest.continue_from_task_id）
async def load_resume_source(db, *, user_id, project_id, kind, task_id) -> Task  # 不合法 → 400
def resume_request(source: Task, req: WizardRequest) -> WizardRequest  # 沿用原请求，仅 ai_config_id 可换
def trim_to_resume_point(text: str) -> str  # 截到最后一个 `}`；空串 = 不可续写
def clean_continuation_head(prefix: str, head: str) -> tuple[str, bool]  # (接续文本, 模型是否从头重来)

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
  `result` 取 handler 可选 `finish_result(task)`（默认原 result + `ended_early: true`；`finish_result` 可抛 `AppError` 拒绝结束，如 `agent.turn` → 409）；`cancel` 支持 paused（条件 UPDATE 后 `on_abort`）。
  `GET /tasks?active=true` 返回 `OPEN_TASK_STATUSES`（含 paused）。非 paused 时 resume / finish → 409 `只有待审核的任务可以继续` / `结束`。
  协作者的待审核任务在降级/撤权/轮次失效后由系统受限收尾为 failed（E5-a，见「协作者执行（E4）」末条），不再永久停在 paused。
- **重试钩子**：handler 可选 `validate_retry(db, ctx, *, user_id, task) -> TaskSpec`，有则代替 `validate_and_claim`（多步任务沿用原范围）。

- 创建：`validate_and_claim` 在请求 session 内完成鉴权、`resolve_config` + `build_provider`（SSRF / 解密校验）、参数校验，
  章节类再原子抢占 `generating`；与任务插入**同一事务** commit。撞部分唯一索引 `uq_tasks_active_resource`
  → 回滚（章节抢占随之撤销）→ 409 `已有任务在进行中，请等待其完成后再试`。
- 章节类任务**创建即抢占**：排队期间章节就是 `generating`（编辑器只读）。`input.prev_status` 记录原状态。
- 调度：按 id 升序扫描 queued；全局运行数 ≥ `TASK_MAX_CONCURRENCY`（默认 4）停止，用户运行数 ≥ `TASK_MAX_PER_USER`
  （默认 2）跳过；`serial_key` 同键已有运行中的任务也跳过（同键内先进先出）。先登记 `asyncio.Task` 再做条件 UPDATE
  （`status='queued'` → `'running'`），影响行数为 1 才执行。领取在锁内确认无法前进而返回（旧文档任务不可证明、handler 未就绪、原认证失效、旧章节无 claim、协作失效不可收尾）时记入本进程退避 `_stalled`（按 UUID + `queued_at`）：`_on_done` 只唤醒、不清退避，`_dispatch` 跳过它但仍占其串行键队首；外部 `notify()` / 兜底轮询超时清空后按数据库事实重试。额度/并发等暂时性返回不退避。
- 运行：`build_job` 按**运行时的最新数据**组装（章节上下文、向导的世界观/角色/大纲）；章节已不处于 `generating`
  （排队期间被复位）→ failed `章节生成状态已被复位，任务未执行`，不覆盖正文。之后用 `aclosing` 消费 runner 事件：
  `delta` 追加到 hub 并广播；`continue` 原样广播并把 stage 设为 `字数不足，自动续写第 n 轮`；`done` → succeeded（progress 100）；
  `error` → failed。`output_text` / progress / stage 每 `flush_interval`（2 秒）节流刷库一次，结束时再写一次。
- 模型细碎文本在 `app/ai/streaming.py::coalesce_buffered_text` 合并后才进入业务事件和任务门禁：单个生产协程持有并关闭原 provider 流，队列最多 64 个原始片段；首片立即交付，后批最多收集 50ms，章节目标批大小 4096 字符、大纲保持 256 字符（不拆单个上游片段）。合并只限同一次模型响应，不预读 runner 的业务事件、不跨场景或续写边界；已读取文本先交付，再传播原错误或模型自主取消。消费者取消/提前关闭必须取消并等待生产者，不能残留网络读取。
  每批仍经过原 `_event_task` / `verify_event`，进度刷写与 SSE 实际发送保留各自的原授权和执行身份复核；不得缓存授权结果、跳过撤权检查或用合并替代最终保存屏障。回归 `test_chapter_stream_performance.py` 覆盖立即就绪/持续异步单字符流、正文无损、首片时序、有界预读、异常/取消、撤权后的积压输出与实际校验/落库次数。
- 批量已取得当前章 claim、尚未构建 `ChapterJob` 时，`BatchHandler.verify_event` 允许 `stage`，以及 `data.get("text") == job.verified_text == ""` 的 `flush`；非空输出仍拒绝。空正文刷新同样经原 attempt guard 的 `require_task_write`、执行身份/模型/claim/授权复核及引擎 CAS。回归 `test_batch_stream_progress.py` 在第二章 claim 提交后才触发进度刷写，覆盖正常跨章与正文、权限、claim 变化的拒绝路径。
- 收尾：非成功先 `handler.on_abort`（章节：仍为 `generating` 时还原 `prev_status`），再写终态 + `finished_at`，
  `prune_finished`；成功时再调用可选的 `handler.on_success`（章节 generate / polish / revise → `analysis_service.enqueue_auto`，
  异常只记日志），最后广播终止事件并释放 hub。`done` 已产出后才收到取消 → 仍记 succeeded。
- 本人取消排队任务时，先按原轮次和取消状态取得清理权。单章、文档索引及批量任务在事务内核对资源，其余处理器先登记清理，再提交终态。已结束的任务返回 409 `任务已结束，无法取消`。
  文档侧没有占用的旧 queued `kb.index` 文档任务（0045 前）：锁内可证明无执行体时由受限系统清理同事务记 cancelled 并把文档置 failed
  （见 [retrieval.md](./retrieval.md)「旧无占用文档任务」）；无法证明仍 409 `task_cleanup_unproven`。引擎领取同一函数记 failed，任务流以 `error` 帧结束。
- 本人取消运行任务按任务 UUID、入队轮次和执行 UUID 持久登记。执行者在调度周期内发现并向原协程发出取消。请求完成原有授权检查后在同一 10 秒窗口内等结果，超时返回当前可见状态；进入新轮次时返回 409，整数 ID 已被其他任务复用时返回 404。管理员意图已经存在时加入原操作。分析、向导或其它无法证明结果的原任务保留待核对语义。
  客户端断连后，已提交意图继续生效。原 runner 负责关闭模型连接、回退本轮章节并写取消记录。回归见 `test_c3_owner_cancellation.py`、`test_c3_cross_entry.py`。
  停止后台调度但仍提供 HTTP 时，请求 worker 可按已受理的单个轮次接管旧无主任务。存在原 worker 时须先按 C1 核实其已停止或失联。接管前后重读原意图、业务收据和失败证据，管理员同时受理时按原操作号交接。本人断连或提交回执丢失后，后备处理仍须在库内核对同一轮次。
- 正常停机时，`stop()` 等待本进程执行体和清理完成，再标记登记已停止。启动和周期恢复都只认领已失去执行者的 running 任务。健康执行者继续处理其任务。崩溃后立即重启时，旧心跳到期前仍显示运行中；默认租约 30 秒，通常在到期后的下一个 5 秒调度周期收尾。SQLite 主库被长写事务占用时，独立租约库照常续约、确认失联，主库恢复可写后继续认领和收尾。
- 恢复按持久凭据决定中断、成功、暂停或待核对。单章释放占用与向导撤销可证未提交标记，须与任务终态、统计同事务提交。若先删除唯一凭据再另行提交终态，下一次故障恢复会失去判断依据。存储暂时不可用时保留认领重试。普通迟到的清理回调仍须核验原执行者存活。作品排空重试已提交的终态清理时，须在锁内证明任务、轮次、终态、原执行者、执行键和原章节占用完整匹配。无法证明的运行中任务持久标为“结果待核对”，再次扫描跳过，作品删除继续受保护。部署条件见[部署与迁移](./deployment-migrations.md)。
- 重试：仅 `failed|cancelled|interrupted`，否则 409 `只有失败、已取消或已中断的任务可以重试`；用原 `input.request`
  重新走 `validate_and_claim`（重新校验配置、重新抢占章节）。被系统清理的旧无占用索引任务（input 含 `_kb_legacy_unclaimed_cleanup`）
  重试 → 409 `kb_legacy_task_not_retryable`，须经 `POST /documents/{id}/reindex` 新受理。

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
| 其他（如 `chapter_start` / `chapter_end`、`tool_start` / `tool_end` / `proposal` / `executed`） | handler 自定义 | 原样广播（批量生成见 batch-generate.md，智能体见 agent.md） |

- runner 的 `stage` 事件不直接转发，只更新 hub 的阶段文案（经 `progress` 事件推送）。

- **作者订阅首帧不能遗漏领取状态**：`out_json_live` 的权限投影会异步等待，期间任务可能从 queued 变为 running，唯一的领取 `progress` 可能在订阅注册前已经广播。最终原身份／授权复核后，作者路径须在与 `hub.subscribe()` 相同的无 `await` 区间刷新首帧的公开运行字段；使用本次已校验行及同 UUID、同入队轮次、执行键相容的 hub，不沿用异步投影之前的 queued 状态。已运行时清空排队位次，并带上最新可证明的阶段／进度；开始时间只使用已读取的事实，不猜测时间。正文快照和队列注册仍同步完成，首帧正文加后续 delta 必须无重复、无遗漏。
  此刷新不重新查询或投影、不缓存授权、不提前注册订阅，仍经过原 `checked_frame` 和实际发送前的授权复核；协作者受控帧的来源证明与发送租约不变。回归 `test_task_stream_snapshot.py` 覆盖投影／最后读取期间领取、尚无正文、已有正文和发送前失权，失败与关闭后不得残留订阅。

- 章节 `result`：`{"chapter": ChapterOut, "warning"?: "自动续写失败：…"}`；向导 `result`：`{"kind", "items": [Out...]}`。
- 不重复不遗漏：`TaskHub.subscribe()` 在同一同步临界区内复制缓冲区并注册订阅队列（中间没有 `await`），
  快照之后的第一个 delta 恰好接在快照末尾。
- 已结束 / 待审核任务：数据库状态优先，即使同 UUID 的内存 hub 仍为 queued / running，快照也必须取自库里的 `output_text`，紧接着回放带持久化 `result` / `error` 的 `done` / `error` / `paused`，然后关闭。订阅注册后的复查若已看到终态，也走相同回放路径，不能发出 succeeded 快照后只等心跳。创建 / 继续请求提交后刷新到终态时，不得重新建立空 queued hub；活动状态建 hub 时沿用实际 status / text / progress / stage。
- 任务行已不存在或因作品生命周期不可见（归属校验后被删除；或订阅中的任务随作品进入deletion_started_at/deleted_at，或随用户/项目级联删除——在心跳间隔检查）：发
  `error {"task": null, "detail": "任务不存在"}` 后结束（前者没有 `snapshot`），并释放该任务的 hub。
- 排队任务的软删除回归场景中，取消关闭Hub与生命周期心跳有两个既存合法赢家：取消帧先到则携同UUID的cancelled Task；心跳先观测生命周期不可见则task=null/任务不存在并结束，不能为取得cancelled摘要越过可见性过滤。该排队场景无论订阅反馈哪个先赢，DELETE成功后真实Task必须安全cancelled、资源释放，新订阅仍404；终止帧之后不继续输出。用事件屏障分别验证，不把测试绑死在一次偶然排程顺序。不能把此排队断言扩为所有运行任务：运行中原子保存已提交时，仍保留真实succeeded赢家。
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

**协作者执行（E4）**

- 只有声明 `collaboration_execution_ready = True` 且 `execution_identity_aware` 的 handler 才能受理/领取带 `_collaboration_grant` 的任务；目前为 `ChapterHandler`（`chapter.generate` / `polish` / `revise`，含场景目标的 revise 入口，E4-f）与 `AnalysisHandler`（`chapter.analyze`：`POST /chapters/{id}/analyze`、`POST /projects/{pid}/analysis/analyze-pending`、提案采纳及单章成功后的自动派生，E4-g1）与 `BatchHandler`（`chapter.batch`：`POST /projects/{pid}/batch-generate`，预览同按 `ai.use`，E4-g2）与 `OutlineHandler`（`outline.continue` / `outline.expand`：`POST /projects/{pid}/outlines/continue`、`/outlines/{oid}/expand`，E4-g3a）与 `EntityHandler`（`character.generate` / `organization.generate` / `career.generate` / `relations.parse` / `relations.generate`：`POST /projects/{pid}/characters/generate`、`/organizations/generate`、`/careers/generate`、`/relationships/generate`、`/characters/{cid}/parse-relations`，E4-g3b；无同步预览入口）与 `WizardHandler`（`wizard.outline` / `characters` / `world` / `careers`：`POST /projects/{pid}/wizard/{kind}`，E4-g3c；无同步预览入口，向导步骤进度 `PATCH /projects/{pid}/wizard-progress` 随之对 accepted editor 开放；管理员人工权限不扩展到此处，见 outline.md）。`KbIndexHandler`（`kb.index` 文档/重建，E6-b）已声明就绪：引擎领取并按每批快照 → 受控发送 → 锁内重验 + 快照 CAS 执行（见 [retrieval.md](./retrieval.md)），但协作者 HTTP 入口仍关闭（E6-c）、订阅仍 404。Agent（协作者）、拆书仍失败关闭，新增开放必须逐类声明并补四时点（受理后领取前 / build 前 / 发送前 / 保存前）撤权测试。
- 开放的 handler 必须同时 `admission_preflight_aware`：HTTP 入口先在任何写锁前做短读 `require_project(ai.use)` + `preflight_project_model` + `prepare_task_admission`（DNS 在锁外），再按 认证门禁 → 发送写门禁 → 作品（按预检 grant）加锁去重并交中央 `create_task` 消费证明。改为受控受理后，Agent 通用 `start_task.execute` 对该类型同样失败关闭，只能经提案确认的 `execute_admitted`；`analyze-pending` 逐章准备证明，锁内重算后只为已有证明的章节入队。
- 事件/终态锁序（E4i-b2）：引擎为协作执行的事件提交（`_event_task`）与 succeeded/paused 终态核验（`complete_state`）在取发送写门禁之前，按领取快照冻结的作者 ID（`frozen_project_owner_id`）与 actor 一起升序锁 User；handler 内 `guard.require_task_write` 等再锁作者只是重入。新增会在门禁后锁作者 User 的 engine 路径必须同样预锁，否则 PG 下作者发送 lease 会 2 秒后 `ai_visibility_busy`。
- 受理按 `project_access.require_project(ai.use, grant_identity=…)`：viewer 403、无协作读取来源（非成员、待接受、已撤权、仅管理员身份）404，不建任务、不抢占。模型按本人优先 → 仅本人无该类配置且 `allow_owner_ai` 时用作者配置，本人配置无效不回退；`GenerationLog.model_source/model_config_owner_id` 记录费用来源，`Task.user_id`/`actor_user_id` 为真实 actor。私人风格/模板/Skill 按 actor 解析，作者私人风格既不能被显式选择也不进入提示词。
- 协作 claim 的 generate/revise 检索只能经原执行身份与受理时冻结的共享向量选择（keyword 不发送；vector 为作者向量并经 `for_task` 守卫逐次重验），命中仍逐条 `verify_retrieval_hits`；无执行身份的直接调用拒绝（否则会落到本人默认向量配置）。
- 协作者单章执行成功后按项目开关**向真实触发者派生**自动分析（E4-g1）：`analysis_service.enqueue_auto` 继承原 actor、令牌、模型绑定（含费用来源）与 `_collaboration_grant`，在 认证(含作者 User) → 发送写门禁 → 作品(按原 grant) 锁内核验原单章完成收据与模型/向量授权后入队；撤权/降级/重邀/关模型授权或无法证明时抛错不入队，引擎只记 `正文已保存，自动分析状态未确认…`，章节成功不受影响。协作分析不带提案来源也合法（手动或单章派生），带 `_analysis_proposal_source` 时仍须完整有效；作者任务不得携带。
- 分析写回（分析行、设定/伏笔回写、剧情记忆关键词单元）只在 `run_analysis_job` 的写事务内一次提交：`_analysis_write_barrier`（按原 grant/令牌）+ `send_guard.require_task_write` + 模型/向量绑定重验均在锁内；分析派生的记忆**不**对协作者建向量索引任务（E6 未开放，`enqueue_memory_for_execution` 对 grant 只返回 keyword/unavailable），作者向量模型零调用。
- `build_job`、事件核验（delta 等非终止事件）与输出刷新含持锁事务，引擎经 `_finish_before_cancel` 等在途步骤结束后再传递取消（结果丢弃）：撤权复查/用户取消若打断 aiosqlite 游标会遗留写事务，使按原 claim 的受限回退永久拿不到锁（回归 `test_revoke_cancel_waits_for_in_flight_event_check` / `_build_job`）。测试里不要在执行体任务内直接发 ASGITransport 请求触发撤权（应用在调用方任务内运行，会被随后的取消打断），应放到独立任务。
- 受控策略 code 的固定文案：`permission_denied`（发送/保存前锁内重验失败）→「权限或模型授权已变化，已停止发送」；`task_output_changed`（事件发布/输出刷新核验未通过）→「任务输出授权已变化，已停止」；`analysis_execution_changed`（分析写回/核验锁内重验失败）→「分析任务执行身份或授权已变化，已停止」；不透传底层异常。
- **完成收据受限收尾**：单章保存已提交（`checkpoint._chapter_claim` 完成收据）但之后的取消、`done` 事件核验或 `verify_finalization` 因撤权/降级/关模型授权/作者改密/可见性变化未通过时，`TaskEngine._complete_from_chapter_receipt` 不要求当前授权，只按原 UUID/`queued_at`/执行键 + `running` + 无管理员意图 CAS，并经 `generation_service.completed_chapter_receipt`（锁作品→Task，逐字核对收据、输入摘要、版本摘要及协作模型/日志来源）证明后，把 Task 记为 `succeeded`（`error` 为固定 `RECEIPT_FINALIZED_NOTE`、`execution_key` 清空、活动资源释放）；`result` 只在与收据 `result_hash` 一致时保存，否则为 null。不写正文/版本/日志、不派生自动分析、不向外发送；Hub 以带任务快照的未发布 `done`（E5-d，`detail` 为 `RECEIPT_FINALIZED_DETAIL`）关闭，重订阅回放同一帧。进程内不可用（含收尾前进程退出）时由 `recover()` 按同一证明收尾；收据损坏/伪造或有管理员意图时仍保留原屏障待核对。回归：`test_collaboration_chapter_entry_e4f.py::*receipt*` 与对应 `_postgresql.py`。
- **批量协作（E4-g2）**：批量的每章都是独立的抢占/发送/保存。协作者批量在构建前 `require_write_execution`，每次断点/占用写（`batch_service._require_batch_round` → `_lock_round_parent`：按原 grant 锁后重验 ai.use、原令牌、作者模型授权）、每章 `claim_batch_chapter`、每次发送（`for_task` + `BatchAttemptIdentity`）、每章保存（`require_task_write`）、逐章分析（`_analysis_write_barrier`）与事件发布前重验；屏障失效统一为 `permission_denied` 受控失败，不计作单章失败、不跳到下一章（`skip_on_fail` 不适用于授权失效）。已提交章节保留，未完成章节按原 claim/attempt 回退；逐章分析在本任务内以真实 actor 与冻结模型执行，记忆只维护关键词单元。终态：停止时为 `failed`（撤权复查取消同为固定协作文案）并保留断点，与“单章失败即停止、重试从断点继续”的既有批量语义一致。
- **批量断点受限收尾**（E4-g2）：执行体内存快照无法证明（取消落在某章保存提交之后、断点同步之前，`on_abort` 的 `_sync_released` 不一致），或全部章节已持久化而完成事件/`verify_finalization` 因撤权/改密/关模型授权失败时，`TaskEngine._complete_from_batch_checkpoint` 不要求当前授权，只按原 UUID/轮次/执行键 + running + 无管理员意图 + 原输入逐字一致 + 断点 `current` 为空且没有章节仍持本任务占用 CAS；断点含 `_batch_completion` 时经 `_require_resume_receipt` 锁内核验其属于本轮执行与对应版本/日志。全部步骤已持久（cursor == 总数、phase generate、无待分析收据）记 `succeeded`（`BATCH_RECEIPT_FINALIZED_NOTE`，result 由持久断点 `summarize` 重建），否则以 `_run` 判定的停止终态（failed/cancelled/interrupted，原因取 on_abort 之前的错误）结束并保留断点；待审核点无法发布时记 failed。只改原 Task 并释放 `batch:{project}`，不写正文/设定/记忆、不派生；证明失败（含伪造收据）保留 running 屏障。该收尾对作者批量同样生效：作者用户取消落在保存提交与断点同步之间时以 cancelled 结束（修复前永久 running），全部章节已持久而仅完成发布核验失败（如 AI 可见性版本变化）时记 succeeded + note（此前为 failed）。回归：`test_collaboration_batch_entry_e4g2.py`（含 `test_author_cancel_after_chapter_commit_before_checkpoint_sync`、`test_skip_on_fail_does_not_swallow_revocation`）与 `_postgresql.py`。
- **大纲协作（E4-g3a）**：入口锁外短读 `require_project(ai.use)` + `preflight_project_model` + `prepare_task_admission`，`validate_outline_task` 在中央受理事务内按接受绑定的 grant 以 `for_update + visibility_write` 重验 ai.use 与作品作者，同屏障冻结共享向量选择（grant/作者须与文本绑定一致）。`build_job` 对协作者先 `require_write_execution`（令牌、accepted editor、原轮次、作者模型授权）并**立即结束该写事务**（后续检索/发送各自开短事务或 lease，SQLite 下不可持写锁跨越），再按原 grant 读作品；检索前 `require_task_write`，之后按受理冻结的共享向量选择检索（E6-d：vector → `for_task` 受控作者向量查询、读作者旧键；keyword → 只用关键词、零向量调用）；每次发送 `for_task` lease、每帧 `stream_guard`/`verify_event`、终态 `verify_finalization` 均 `require_task_write`。展开分批时第二批发送前同样被 lease 拦下。作者直调（`execution is None`）仍经 `_job_project` → `project_service.get_execution_project` → `get_owned_project` 严格作者，协作者不可达。
- **实体协作（E4-g3b）**：模板同大纲。入口 `entity_ai_service.create_entity_task` 锁外短读 `require_project(ai.use)` + `preflight_project_model` + `prepare_task_admission`；`validate_entity_task` 在中央受理事务内按接受绑定的 grant 以 `for_update + visibility_write` 重验 ai.use，核对作品作者，旧关系解析的主体角色改按已鉴权作品过滤（`_project_character`，不再经作者专用 `story_service.get_owned`），作者绑定只允许 `source=actor`、`source=owner` 只属于带 grant 的协作者，冻结的共享向量选择 grant/作者须与文本绑定一致。`EntityHandler.build_job` 在同一短会话内 `require_write_execution` 后经共用 `get_execution_project`（作者严格、协作者按原 grant）读作品并逐项核对两份绑定与执行身份，会话结束即释放；`build_entity_job` 的作者直调（`execution is None`，普通 `guard_provider` 分支）仍严格作者，协作者 404。检索前（`_knowledge_for_execution`）：冻结选择为 vector（E6-d）→ 作者与编辑者共用 `for_task` 受控作者向量查询（读作者旧键）；为 keyword 时协作者先以文本 guard（含 extra 共享向量绑定与已采集实体来源）`require_task_write` 并立即回滚，之后只做关键词检索、不构造向量 embedder；每次发送 `for_task` lease（含 `EntitySourceSnapshot`）、每帧 `verify_event`/`stream_guard`、`done` 与终态 `verify_finalization` 均 `require_task_write` 并在锁内按当前同书数据重算草稿。作者配置回退时 `build_entity_job` 只按 `config_owner_id` 核对配置实例与指纹，不把配置交出。
- **向导协作（E4-g3c）**：入口 `wizard_service.create_wizard_task` 同实体模板（锁外 `require_project(ai.use)` 短读 + `preflight_project_model` + `prepare_task_admission`；续写且未显式选模型时预检沿用本人来源任务原请求的配置）。`WizardHandler` 声明 `admission_preflight_aware` 与 `collaboration_execution_ready`；受理锁内 `lock_wizard_project` 按接受绑定的 grant 以 `for_update + visibility_write` 重验 ai.use 并核对作者（作者只允许 `source=actor`），再读续写来源与覆盖预检；重试走 `validate_retry`（原模型绑定与原轮次逐字一致）。续写来源 `load_resume_source(grant_identity=…)` 要求来源 grant 与本次执行逐字一致：作者只接无 grant 的本人任务，协作者只接本人**同一邀请轮次**的任务（重邀后旧来源 400），`verify_resume_source` 对协作者来源按原 grant 重验 ai.use。来源 `input.ai_history_epoch` 的代次核对分两种语义（`load_resume_source` / `verify_resume_source` 的 `expected_epoch`）：受理、构建、发送前与保存事务内传 None，与**当前** epoch 比较（拦截收紧后继续发送旧派生内容）；**保存之后**的核验——`WizardHandler.verify_event(done)`（含 `verify_finalization`）、`confirm_wizard_commit`、done 帧仅发布 guard 的 `_verify_resume`（`for_task(..., publication_only=True, resume_epoch=…)`，续写的发布 guard 必须携带、不晚于新戳，其余 guard 不得携带）——传构建戳 epoch（`WizardJob.resume_epoch` = `scope.stamp.epoch`），因为本任务自己的覆盖保存（替换已有条目或候选竞争时 `bump_visibility_revision(tighten=True)`）可能已推进当前 epoch；保存事务已以 `require_visibility_stamp(构建戳)` + 当前 epoch 核对来源 epoch == 构建戳 epoch，保存后再要求保存事务内读取的新戳仍成立，两段证明串起来说明中间唯一的变化是本次保存。他人在保存后收紧/改版本 → 新戳失效，仍按收据受限收尾（不发布）；构建后、保存前他人改 epoch → 保存被拒、内容不变。不得把保存后路径改回与当前 epoch 比较（续写替换必然不发布），也不得省略 `expected_epoch` 比较（`tests/test_wizard_resume_publication.py` 负对照）。`build_job` 对协作执行先 `require_write_execution`（未决凭据写入之前，撤权不留凭据），作品经 `get_execution_project`；`build_wizard_job` 取模型名/思考/输出上限自原绑定的锁内快照（作者回退时协作者无权按本人读作者配置行），检索前以 send guard `require_task_write` 锁内重验；E6-d 起受理时（仅协作执行）冻结 `_shared_embedding`，send guard 与完成仅发布 guard 都带该额外绑定，冻结选择为 vector → 作者向量经 `for_task` 受控查询（读作者旧键），keyword → 只做关键词检索（协作者本人向量永不参与）。保存事务 `require_write_execution` + 模型绑定重验（两层任一即拒绝撤权/降级/关模型授权/停用）；`verify_finalization` 对协作执行在终态事务内再 `require_write_execution`。
- **向导收据收尾（E4-g3c，写库型，不进 `_DRAFT_ONLY_KINDS`）**：向导直接整体替换设定/大纲，内容与完成收据 `_wizard_completion`（同一 UPDATE 把已逐帧核验发布的输出写入 Task 自有 `output_text` 列，供 stream_hash 持久证明；收据本身只存 64 位摘要，不另存全文）在同一事务提交。完成事件核验、取消或终态核验在收据提交之后失败时，`TaskEngine._complete_from_wizard_receipt` 不要求当前授权，只认原 UUID/轮次/执行键 + running + 无管理员意图，并经 `wizard_service.completed_wizard_receipt`（锁作品 → Task，核对收据字段、原输入摘要、模型绑定与原 grant——协作者按数据库中的作品作者——、结果摘要与输出全文摘要）证明后记 `succeeded`、`result` 取自收据、`error=WIZARD_RECEIPT_FINALIZED_NOTE`、清执行键并释放 `wizard:{pid}:{kind}`；不写内容、不发布、不派生，Hub 以带任务快照的未发布 `done`（E5-d，`detail` 为 `WIZARD_RECEIPT_FINALIZED_DETAIL`）关闭。收据破损/伪造或有管理员意图时保留 running 屏障。收据提交之前的失败：`discard_wizard_pending` 改为**系统清理**（锁作品 → 原轮次 Task，逐字核对凭据后撤销，不要求当前授权；保存与凭据替换同事务，调用方又只在保存从未尝试提交时调用），因此撤权/降级/改密后任务以失败结束而不是永久 running。`recover()`：可证完成收据 → succeeded；只有逐字可证的未决凭据（无收据）→ `release_uncommitted_pending` 撤销后按普通中断收尾；无凭据或凭据破损仍为待核对（`test_wizard_uncertain_cleanup.py`）。成功向导任务的删除/裁剪证明 `verified_wizard_terminal_receipt(task, project_owner_id=…)` 对协作者任务须由调用方传入数据库中的作品作者。作者路径的行为变化（均为失败关闭或由永久 running 改为可证终态）：①发送中/保存前作者改密，凭据可系统清理，任务 failed 而非永久 running；②内容已提交、终态前作者改密/可见性版本变化（已测）及模型绑定变化等同类令终态核验失败的变化，由停在 running 并登记清理失败改为按收据 succeeded（附固定说明）；③重启遗留的只带可证未决凭据的向导任务改为 interrupted；④重试沿用原任务固定的模型（此前按当时默认配置重解析；中央受理对所有 `admission_preflight_aware` 类型的重试只接受原模型预检，与章节/分析/批量/大纲/实体一致，不为作者另开重解析模式）。受理报错顺序保持原语义：锁外短读内先无锁预检续写来源与覆盖冲突，再做模型预检（受理锁内仍完整重验）；`PATCH /wizard-progress` 只新增 accepted editor，管理员语义不变。回归 `tests/test_collaboration_wizard_entry_e4g3c.py`、`_postgresql.py`。
- **草稿任务终态收尾**（E4-g3a，`_DRAFT_ONLY_KINDS = outline.*`；E4-g3b 扩展到五类实体生成，资源键为 `relparse:{cid}`）：大纲/实体唯一持久化是 Task 终态与草稿 `result`，在同一终态事务内锁内重验后写入，没有“已提交、待证明”的业务副作用，因此**不设完成收据**。完成事件已发布、终态事务前授权变化（撤权/降级/关模型授权/改密）导致 `verify_finalization` 或终态认证抛错时，`_finalize` 按原 UUID/轮次/执行键改记 `failed`、`result=None`、固定文案「任务输出授权已变化，已停止」（改密时为既有无正文系统收尾文案），释放 `outline:{pid}` / `expand:{oid}`，不登记项目清理失败。修复前该窗口使任务停在 running 并永久占用资源键；作者任务同样适用，作者路径的行为变化范围是**任何**令作者 `verify_finalization`/终态认证抛错的变化（作者改密、AI 可见性版本/代次变化、作者模型绑定变化、作品进入回收站、SQLite 门禁限时忙），由停在 running 并登记清理失败改为无结果 failed，不止改密一项（回归 `test_author_finalization_window_change_fails_closed`）。实体（E4-g3b）在此之外还包括：发送后原提示词来源设定（角色/世界观/组织/职业/阶段/成员的已采集字段，关系与类型除外）被人工修改、使 `done` 已发布后的终态重验抛 `entity_source_changed` 的情形，同样由停在 running 改为无结果 failed，文案保留与完成事件核验一致的「设定来源已变化，请重新发起」（`_finalize` 仅按受控 code `entity_source_changed` 选固定文案，不透传异常正文；其余变化仍为「任务输出授权已变化，已停止」）（回归 `test_collaboration_entity_entry_e4g3b.py::test_author_finalization_window_change_fails_closed[actor_token|visibility|entity_source]`）；收尾不触碰其他任务类型，也不改变管理员取消（有管理员意图时按原 CAS 写 failed 并清标记，不伪造 cancelled）；`recover()` 对遗留 running 的大纲/实体任务走通用 interrupted 收尾（不经此分支，也无需收据）。草稿不会因提交确认丢失而被误判：先按原 UUID 重读终态事实，已提交的成功不会被改写。
- **分析完成收据受限收尾**（E4-g1）：分析写回已提交（`checkpoint._analysis_completion`）但 `done` 核验/终态核验因授权或令牌变化未通过时，`TaskEngine._complete_from_analysis_receipt` 按同一边界（原 UUID/轮次/执行键 + running + 无管理员意图 CAS，`completed_analysis_receipt` 锁作品→Task→分析行逐字核对输入摘要、模型/向量绑定、grant、结果与分析摘要）把 Task 记为 `succeeded`，`error=ANALYSIS_RECEIPT_FINALIZED_NOTE`，`result` 由收据逐字重建（终态删除/裁剪证明要求一致），释放 `analysis:{project}` 串行键；不派生记忆索引。`recover()` 对带完成收据的 running 分析同样收尾，其余（pending、损坏/伪造收据、管理员意图）仍保留待核对。回归：`test_collaboration_analysis_entry_e4g.py::*receipt*` 与 `_postgresql.py`。
- **待审核协作任务的系统收尾（E5-a）**：编辑者被降为 viewer、被撤权、邀请轮次失效（重邀）、经 PATCH/直接改库关闭作者模型授权（任务使用作者模型时）、账号停用或原受理令牌失效后，其 `paused` 协作任务（批量审核点、富文档待审核等）不再永久停留：`TaskEngine.scan_collaboration_paused` 无锁预筛（`task_visible_to_actor(active_grant_required=True, token_version=原令牌)` + 作者模型开关）后，对失效者逐个 `_fail_collaboration_paused`：与领取同锁序 gate → actor/作者 User → 发送写门禁 → 作品 `lock_active` → Task `FOR UPDATE`（原 UUID + queued_at + paused + 无执行键 + 两项管理员意图为空）→ 锁内 `_collaboration_claim_failure` 复核 → 复用 `_fail_collaboration_queued` 的 paused 分支 CAS 为 failed（固定协作文案，保留暂停时的 result 摘要）、同事务 `rich_document_review.discard_pending`（提案 discarded、断点审核阶段复位）与 `record_state_change`。已提交章节与断点保留，作品级批量互斥随终态释放；不调用 `on_abort`、不写正文/设定、不发送、不派生。触发点：`notify_collaboration_change` 后对该作品+成员立即复查（后台任务，进 `_collaboration_checks`，stop 时取消并等待），以及调度循环按 `collaboration_scan_interval`（15 秒）全量复查，进程启动后第一轮立即执行（覆盖重启/跨实例未收到通知）。候选 SQL 只取 `status='paused'`、带 grant、类型在可收尾白名单内（白名单外的失效任务永远不能收尾，若纳入会每轮反复取写锁）的行，按 `ix_tasks_status_id` 键集分页（`collaboration_scan_batch`=100，每批一个短只读会话）；仍有效者只做无锁读，SQLite 下不取写锁。不误伤：作者任务（候选 SQL 排除 `Project.user_id == Task.user_id`，锁内再拒绝）、有管理员取消意图、轮次/UUID 已变化（已继续或再次暂停）、仍有本任务章节占用（`generation_claim_task_key`）、批量 `current` 非空、含待核对收据键（与排队清理同表）、拆书/知识库类型——保持原屏障并对同一轮次只记一次日志。`agent.turn` 自 E4i-e（裁决 11）起纳入 queued / paused 收尾白名单：同事务把本轮干净的 `proposed` 提案作废为 failed（固定文案），`executing` / 带外部未知标记的行不动；运行中轮次的作废由 `AgentHandler.on_abort` 锁内复核后完成，详见 [agent.md](./agent.md)「协作轮次的撤权受限收尾」。降为 viewer 或重邀后仍是 accepted 成员时可读本人已结束记录；撤权后 404；作者可立即发起新批量。回归：`test_collaboration_paused_cleanup_e5a.py`、`test_collaboration_task_surface_e5a.py`（含批量审核点协作者采纳/finish）、`test_collaboration_paused_task_postgresql.py`（finish/resume × 撤权、系统收尾 × finish/resume 两种先后）。
- **个人任务读写面矩阵（E5-a 补测）**：身份 {作者、原轮次 editor、管理员兼 editor} 对本人任务全部可见可控；{降为 viewer、管理员兼 viewer、重邀后旧轮次} 只可读/删除本人已结束任务，取消/继续/结束一律 404，重试被受控重试的原绑定/轮次校验拒绝（400 或 404），不新建任务——原模型短读预检（`preflight_original_project_model`）在锁外 provider/DNS 预检之前即拒绝非 editor 与旧轮次，不构造 provider；{待接受、已拒绝、已撤权、非成员、仅管理员只读/可写} 一律 404 且清理 0 条。任何成员（含管理员）对他人任务的控制均 404。管理员运维接口 `/admin/tasks` 只返回元数据并可受理取消，不能经个人任务接口读取输出。本人操作提交后、响应投影前撤权提交时，响应为 404，但已提交的操作有效（以数据库为准）。
- **受控任务流的持久回放帧证明（E5-b）**：受控管线（`engine.subscribe(guarded=True)` + `stream_lease.task_sse_response`，自 E5-f 起由生产路由对协作者挂载，作者生产订阅不走受控管线、行为不变）中，来源为 `TaskViewEvidence` 且持锁 Task 为已结束或 `paused` 的帧（`_finished_events` 回放：`snapshot` + `done`/`paused`/`error`），由 `stream_evidence._verify_persisted_replay` 在实际 ASGI send 的同一帧租约内证明：`capture_task_view(持锁行) == 帧来源`；事件名必须与持锁状态对应（succeeded→done、paused→paused、其余→error，键集合固定）；按租约登记的订阅原绑定（`stream_lease.task_stream_binding`，只在真实单帧发送租约内可取，帧结束即随 `_LeaseState` 清除）重算：原始帧取自与引擎 `_finished_events` **同一函数** `stream_evidence.persisted_replay_events`（事件名与键集合由它决定，不得另写映射），作品任务对整帧经 `project_payload`（与引擎 `checked_frame` 相同），再按原绑定（本人、原 queued_at、原 grant、`task_visible_to_actor(原 active_grant_required, 原令牌)`）SQL 复核，最后经共用的 `finalize_projected_frame` 注入原 task_key / 提案凭据，摘要须与帧逐字一致；`paused` 的富文档审核结果另以共用的 `rich_review_shape_ok` + `rich_review_pending_conditions` 在同一事务内联表复核 Task 断点与 pending 提案。个人任务原持久帧逐字发送、不注入。修改回放帧形状、投影或富文档条件时只改这些共用函数。个人任务（project 为 None）不经作品投影，帧不得带可见性戳。不依赖执行体、`SendGuard` 或模型绑定，所以收据收尾（E5-d）可复用；作品读取权与撤权串行化仍由帧租约（认证发送屏障 → 作品 FOR SHARE → Task FOR SHARE + `task_visible_to_actor(原绑定)` → 成员原 invitation/revision）保证。从活动态（含 paused）开始的订阅一直要求原 editor 轮次：降级/系统收尾后旧流的帧（含快照）一律 `permission_denied`；降为 viewer 的本人重新订阅回放终态。作品删除中的作者固定取消帧仍走原分支。任何不一致只发 `permission_denied`、不带 result。回归：`tests/test_collaboration_stream_replay_e5b.py`、`test_collaboration_stream_replay_e5b_postgresql.py`。
- **受控任务流的章节 / 分析运行中帧证明（E5-c1）**：`ChapterHandler`（generate/polish/revise，含场景目标）与 `AnalysisHandler`（`chapter.analyze`）实现 `stream_guard`，引擎 `checked` 用它为每个已过 `verify_event` 的事件签发 `PublicationSource`（snapshot/delta/progress/自定义如 `continue`，以及终态 done/paused/error 的原来源）。章节 `verify_event` 经 `handlers.chapter._job_guard` 为同一执行固定**同一个** `SendGuard`（缓存在 `ChapterJob.publication_guard`，按全部绑定输入——执行身份、模型绑定、场景戳、cipher、私网开关、章节占用、批量 attempt、共享向量选择——的对象身份失效），`stream_guard` 只交出与**当前全部**输入一致的该对象、不重建（只比执行身份不够）——每帧换 guard 会让 hub 文本来源混合、晚到订阅者的快照失败。分析的 guard 在领取时构造一次（`AnalysisJob.send_guard`），发布身份随 pending / 完成收据推进：`collaboration_ai_guard.publication_execution_matches` 只接受与 `guard._task` 逐字相等，或「原断点 + 该 guard 的 pending 文档」，或「原断点 + 由同一 pending 文档派生的 `_analysis_completion`」（收据须逐键包含 pending 文档，缺键不得以 None 冒充，断点不得夹带其他键）；`issue_publication` 与中央校验共用它，不得放宽为 `same_execution`。`SendGuard._stream_readable` 决定可证明的执行：大纲/实体、单章（`_chapter.kind == "single"`）、单独章节分析；非批量 guard 夹带批量 attempt / 批量分析引用 / 批量占用即不可读，`chapter.batch` 只接受 E5-c2 的三种发布形状，`wizard.*` 只接受 E5-c3 的两种；文档占用、向量输入仍失败关闭（E6）。运行帧 `require_task_read`：分析在 `_check_task` 的 read_lock 分支按共享锁核对原执行与 pending；单章在 `_verify_task` 之后调用 `chapter_claim.require_streaming_chapter`——**只读**（PostgreSQL FOR SHARE；SQLite 沿用租约事务已持的库锁）、不要求模型发送 lease、失败不回滚（租约关闭会话），只能用于帧租约，保存/发送路径仍是 `require_running_chapter` / `require_sending_chapter`，不得互换。不取写锁是为了不与同一帧租约的共享锁或运行体发送 lease 自锁 / 互等。终态 `require_terminal_read`：单章不再比场景戳（含目标章 content_revision，本执行保存必然改变），与引擎 done/paused 发布核验一致，只认逐字一致的持久 Task 视图；`Task.input` 须与领取时逐字一致，单章仅允许移除本 guard 原 `_chapter_claim` 指针（`_terminal_input_matches`）。终态帧若为富文档 `paused`，另经 `stream_evidence._bound_task_visible`（与持久回放 E5-b 共用）按原绑定联表核对 pending 提案，提案在产出后被丢弃/采纳即拒绝。领取瞬间与首个事件核验前的帧（排队中开始的订阅在领取时收到的 `reset`/`progress`、领取后等待首个已核验事件期间开始的订阅的快照）**已由 E5-e 解决**（见下「早订阅」条目；原 `test_known_gap_*` 已翻转为 `test_queued_subscription_receives_claim_frames_and_done`、`test_subscription_before_first_verified_event_gets_empty_snapshot`）。回归：`tests/test_collaboration_stream_running_e5c1.py`、`test_collaboration_stream_running_e5c1_postgresql.py`。
- **受控任务流的批量运行中帧证明（E5-c2）**：`BatchHandler.stream_guard` 按当前步骤交出三种发布 guard（形状由 `SendGuard._batch_stream_shape` 判定，`_stream_readable` 对 `chapter.batch` 只接受这三种）：①**attempt**——有当前章时，`verify_event` 刚用过的同一 guard（`handlers.batch._attempt_guard`，缓存在 `BatchJob.publication_guards`，按原执行、模型/向量绑定、可见性戳、场景戳、cipher、私网开关、章节占用、attempt、本章 ChapterJob 的对象身份失效），绑定 `_batch_attempt` 与同一批量章节占用；章切换即换 guard，构建本章 ChapterJob 之前（无场景戳）与之后（带场景戳）是两组输入。②**analysis**——逐章分析中（`BatchJob.active_analysis`，在 `_reserve_review_analysis` 登记收据之后、`分析中` 阶段之前设置，分析结束清除），绑定分析原执行 + 收据引用，只读核对走 `_check_task` 分析分支并以 `analysis_service._require_batch_analysis(streaming=True)` 接受本执行 running 或已完成的收据（全程共享锁；发送/写路径仍只接受 running）。③**between**——无当前章（章与章之间、审核点、完成）：`for_task(..., publication_only=True)` 的**仅发布** guard，原执行即本执行最近一次确认提交的断点（`job.task_execution_identity`，逐字比对持锁行）；`_publication_only` guard 的 `verify`/`prepare_provider`/`require_task_write` 一律 `CollaborationSendBlocked`，非批量不得声明。`publication_execution_matches` 对批量：between 逐字相等；attempt 要求 `same_execution` 且断点/游标恰为本 attempt 写入的状态（与持锁行共用 `_batch_attempt_state`）；analysis 要求阶段 analyze、无 current、游标与 `rich_analysis` 即本 guard 引用——交出旧章 guard 即拒绝，不放宽为 `same_execution`。`chapter_start`/`chapter_end`/`reset`/阶段进度/审核点 `paused`/`done` 都经 `hub.publish` 携带 `hub.source`。`require_streaming_chapter` 接受 `kind == "batch"`（核对 `checkpoint.current` 指针）。**后继来源**：批量断点逐章推进，帧发出时原来源常已被同一执行后来的步骤取代；`verify_stream_frame` 仅对批量 guard、在 hub 未关闭且无终态时，接受同一 hub 已登记（`_publication_hub(latest) is hub`）、`publication_successor` 成立（同 actor/绑定/原执行、游标不回退、两端各自满足发布规则）且与持锁行逐字一致的**最新**来源，并以其 guard 的 `require_task_read` 在锁内复核当前授权与当前章；不以持锁行补签，不跨执行。行已推进而新来源未发布时抛 `PublicationSourcePending`（`TerminalEvidencePending` 子类）：退出租约，等 `hub.source_changed`（`TaskHub.source` setter 每次换值唤醒）或终态 latch 后重验；帧租约在 `frame_timeout` 内循环重验（终态 latch 只触发一次，原单章语义不变）。为不让在途帧等到模型首个 token，批量在本章占用与 attempt 提交后立即重发当前阶段（`batch_service._refresh_stage`，文案不变，不广播），引擎随之核验并换上本 attempt 来源；`verify_event` 在构建前允许该阶段事件及 `data.get("text") == job.verified_text == ""` 的空正文进度刷写，均须通过原 attempt guard；非空正文仍拒绝。终态 `require_terminal_read` 沿用 E5-c1（批量 Task.input 不变；富文档 `paused` 另经 `_bound_task_visible` 联表）。领取瞬间与首个事件前订阅由 E5-e 的领取来源证明（批量首个事件是紧随领取的 `reset`）。回归：`tests/test_collaboration_stream_batch_e5c2.py`、`test_collaboration_stream_batch_e5c2_postgresql.py`。
- **受控任务流的向导运行中帧与 done 帧证明（E5-c3）**：`WizardHandler.stream_guard` 只交出两种发布 guard（形状由 `SendGuard._wizard_stream_shape` 判定，`_stream_readable` 对 `wizard.*` 只接受这两种）：①**pending**——构建时的发送 guard（`WizardJob.guard`），原执行即带本轮 `_wizard_pending` 未决凭据的断点（`job.pending.active`），可见性戳为构建时的戳；stage（续写「从中断处继续」）、delta、error 用它，`stream_guard` 要求 `guard._task is pending.active`、绑定/续写来源/戳与 job 当前值为同一对象，否则拒绝。②**completed**——保存（内容替换与 `_wizard_completion` 收据同事务）之后的 done：`for_task(完成断点, 原模型绑定, 保存事务内读取的新戳, resume=原续写来源, publication_only=True)` 的**仅发布** guard（缓存在 `WizardJob.publication_guard`，按完成断点、模型绑定、戳、cipher、私网开关、续写来源及其构建戳 epoch 的对象身份失效），交出前先以 `verify_wizard_completion` 按原未决凭据逐键核对收据与 done 载荷；`_make` 只允许向导仅发布 guard 绑定「带完成收据、不带未决凭据」的断点。两种形状都要求凭据/收据与原执行身份逐项一致（版本、nonce/task_key/execution_key 为 UUID、queued_at、actor、project、kind、input_hash；收据另含 result（kind 一致）/result_hash/stream_hash），且无章节/批量/分析/文档/向量/实体来源/场景戳与一次性回调；额外绑定只允许协作执行受理时冻结的恰一份共享向量选择（E6-d，主绑定须为文本模型，同执行戳后继另要求前后 guard 额外绑定相同）。`publication_execution_matches` 对向导只认形状合法且与 guard 原执行**逐字相等**；未决 → 完成的推进不在这里放宽（done 由另构造的 completed guard 签发，未决阶段的帧不转交给它，**不使用 E5-c2 的批量后继来源**）。需要 completed guard 的原因：角色/世界观整体替换已有条目时保存本身会收紧可见性（递增 revision/epoch），沿用构建戳的终态核对必然失败。运行帧只读核对沿用 `require_task_read`（`_check_task` 按 guard 原执行逐字比对持锁行，续写来源经 `verify_resume_source(for_update=True)`——PostgreSQL FOR SHARE、协作者走 `require_project` 非写分支），终态沿用 `require_terminal_read`（向导 Task.input 不变），全程共享锁、不升级、不回滚。作者订阅管线、帧序列与内容不变；作者任务的每个事件多一次纯构造检查（done 时多构造一次仅发布 guard），若对作者失败会使 done 走既有收据受限收尾。**同执行戳后继（2026-11-13，原已知窗口已解决）**：未决阶段的帧（最后的 delta 及随之的 progress、或保存后才订阅的快照）若在本任务保存提交之后才实际发送，而保存收紧了可见性（角色/世界观/职业整体替换已有条目或候选竞争，递增 revision/epoch），帧投影戳与构建 guard 戳都已失效。规则**仅适用设定类向导**（`wizard.characters/world/careers`，`collaboration_ai_guard.wizard_successor_eligible`：未决形状、非仅发布）：中央 `verify_stream_frame` 对这类帧，在投影戳与当前作品戳不一致（只允许 revision/epoch 不同，作品 ID 与删除代次仍须一致）**或**构建戳已不再成立时，不走直接/终态分支，改入 `_verify_wizard_successor`：①原未决执行仍与持锁行一致而戳已变 → 拒绝（变化来自他人）；②取同一 hub 当前来源，须由该 hub 登记（`_publication_hub(latest) is hub`）且 `wizard_publication_successor` 成立——后者为同一执行的 completed 仅发布 guard（即 E5-c3 done 帧的 `publication_guard` 缓存），两端各自与发布身份逐字相等、`same_execution` 且游标不变，同 actor/模型绑定/cipher/私网开关/续写来源对象，续写时 `resume_epoch` 恰为构建戳 epoch，完成断点恰为「原断点去掉未决凭据 + 由同一凭据逐键（含类型）派生的 `_wizard_completion`」，新戳与构建戳同作品/同删除代次且不回退，帧投影戳必须**等于构建戳或该新戳之一**（第三方变化后的戳不可）；③以该 completed guard 在帧租约锁内复核当前授权与新戳仍成立（行仍 running 带完成收据 → `require_task_read`；已终态 → 原 hub 登记的终态证据 + `require_terminal_read`，未登记则 `TerminalEvidencePending`）。证明链：保存事务在可见性独占门禁内 `require_visibility_stamp(构建戳)` 后才写入并读取新戳，故构建戳 → 新戳之间的唯一变化是本任务保存；锁内再要求新戳仍成立，排除保存后他人变化。完成来源尚未发布（保存已提交、done 尚未核验）时抛 `PublicationSourcePending`：退出整个租约，等 `hub.source_changed` 或终态 latch 再重验，受 `frame_timeout` 约束；hub 关闭后不再等待（done 核验失败、收据受限收尾时不会有完成来源 → 拒绝）。他人造成的变化一律拒绝：保存后他人再收紧（新戳失效）、构建后保存前他人改 epoch（保存按构建戳失败、无完成来源）、撤权/降级/关作者模型授权（有无通知，租约成员复查或 completed guard 的锁内复核）。**禁止的改法**：用持锁行或当前戳补签/重建 guard；把后继放宽到大纲、单章、分析、实体或跨执行/跨 hub；放宽中央投影戳检查为通用规则（E5-b 取舍不变）；仅比较 nonce 以外的部分收据、省略帧戳 ∈ {构建戳, 新戳}；在持帧锁时等待完成来源。大纲保存及无已有条目且无候选竞争的首次设定保存不改可见性，构建戳仍成立，不进入后继分支，行为不变。作者生产订阅管线与帧序列不变；受控流新增开销仅在上述失效时多一次纯判定与一次锁内只读复核，新失败面为等待完成来源超过 `frame_timeout` 时静默断开（同 E5-c2）。用例 `test_running_frame_in_flight_across_own_tightening_save_is_sent`（e5c3）、`tests/test_collaboration_stream_wizard_successor.py`、`test_collaboration_stream_wizard_successor_postgresql.py`。首个事件核验前订阅由 E5-e 解决（`test_subscription_before_first_verified_wizard_event_gets_empty_snapshot`）。续写向导的 completed guard 另携构建戳 epoch（`_resume_epoch`），续写来源 epoch 与之比较而非当前 epoch（见上「向导协作（E4-g3c）」的代次两种语义）；2026-11-13 前此处与当前 epoch 比较，续写角色/世界观/职业并替换已有条目（或候选竞争）时必然不发布（作者与协作者均受影响，大纲不受影响），已按方案 A 修复，用例 `test_wizard_resume_frames_pass_proof_over_tcp[*-characters]`。回归：`tests/test_collaboration_stream_wizard_e5c3.py`、`test_collaboration_stream_wizard_e5c3_postgresql.py`。
- **收据收尾的终态帧（E5-d）**：单章 / 分析 / 批量 / 向导按完成收据或持久断点受限收尾（`_complete_from_chapter_receipt` / `_complete_from_analysis_receipt` / `_complete_from_batch_checkpoint` / `_complete_from_wizard_receipt`）后，`_finalize` 不再发不含快照的 error，而由 `_receipt_terminal_frame` 按原 UUID/statistics_key/user/project/kind/queued_at/终态/`execution_key IS NULL` 重读**已提交**行，经 `persisted_replay_events` 生成终态帧，并以该行的 `capture_task_view` 作为帧来源（`hub.source`）再 `hub.close`。成功行（`status=succeeded` 且 `error` 恰为本类型固定 NOTE，`engine.receipt_unpublished_detail` 判定）→ `done {"task": TaskOut, "result": null, "unpublished": true, "detail": <对应 *_FINALIZED_DETAIL>}`；批量按断点收尾的 failed/cancelled/interrupted 行 → 普通 `error {"task": TaskOut, "detail": row.error}`。回放（重新订阅已结束任务）与实时用同一映射，帧逐字一致。**result 固定为 null**：收据重建的 result（章节正文、分析摘要、向导条目、批量汇总）保存在持久行供删除/裁剪证明，但不进入任何帧；任务快照 `error` 携带 NOTE 说明，`detail` 保留原「请刷新后查看」提示。证明只依赖已提交行：受控订阅走 E5-b 持久回放证明（帧租约锁内重算投影 + 按订阅原绑定 `_bound_task_visible`，另有 `project_payload` 的本人项目校验），**不**签发 `PublicationSource`/`TerminalEvidence`、不要求当前可见性戳或模型绑定；已失权订阅者在引擎 `checked_frame` 或发送时帧租约被拒，只得 `permission_denied`。作者生产（非受控）订阅经 `checked_frame` 的可见性复验与投影。重读失败（异常 / 行已变化）时退回旧的 `error {"task": null, "detail": DETAIL}`；收尾期间再被取消时先关 hub 再原样传递取消。`recover()` 路径（无订阅者）不变。**已知窗口**：收据收尾没有执行体终态证明，保存提交后才实际发送的在途运行帧（单章/分析/向导的最后 delta、批量末章保存后的 chapter_end/progress）会以 `permission_denied` 结束，重订阅可得上述回放。前端 `subscribeTask` 把 `unpublished: true` 的 done 映射为 `{status: 'succeeded', result: null, unpublished: true, detail}`；章节编辑器重新读取已保存正文，分析/批量/向导以提示消息展示 `detail`，不使用 result。回归：`test_collaboration_receipt_done_e5d.py`（四类 × {作者, 编辑者} × {实时, 回放}、作者生产入口、撤权/降级、发送前撤权/降为 viewer——降级时成员仍 accepted、`project_payload` 放行，单独证明帧租约按原绑定复查承重）与 `_postgresql.py`（帧租约先持锁 / 撤权先持锁两种顺序）；前端 `api/tasks.test.ts`。
- **受控任务流的早订阅：排队中 / 领取瞬间 / 首个已核验事件之前（E5-e）**：原先这三个时点 hub 没有可证明来源（排队 hub 来源为空、领取时为裸 `TaskExecutionIdentity`），受控订阅直接 `permission_denied`。现由引擎在发布时登记两种**无输出**来源（`stream_evidence`，均为进程内弱引用登记、不序列化、不授予发送/预检/写入能力），中央 `verify_stream_frame` 在实际 ASGI send 的帧租约锁内证明：①**`QueuedSource`**——`_broadcast_queue_positions` 为排队中、无执行键的 hub 在发布排队位次前登记（`issue_queued`，绑定 task_id/UUID/queued_at + hub）；`_verify_queued_frame` 只接受 `progress` 且键集合恰为 status/progress/stage/queue_position、status=queued、位次为 null 或非负整数，持锁行须是同一 UUID 与入队轮次（行已被领取或结束时仍是同一轮次的过期位次，可发），另按订阅原绑定 `_bound_task_visible` + 作者模型授权直接核对。②**`ClaimSource`**——`_claim_and_run` 在领取 CAS **已确认提交**后（含提交确认丢失后按原执行键证实为 running 的分支）、发布领取帧之前，`TaskEngine._claim_source` 以领取者在原 queued 行锁内按本次 CAS 执行键构造的同一 `TaskExecutionIdentity` 和以该执行登记的 hub 签发（`issue_claim`；只对实现 `stream_guard` 的类型——单章、分析、批量、向导、大纲、实体、一致性、智能体——签发，知识库/拆书仍为裸执行身份、继续失败关闭）；首个已核验事件会照旧换成 `PublicationSource`。`_verify_claim_frame` 的非终态帧只许不含模型输出的形状：领取 `progress`（status=running 三键，或排队位次清空的过渡帧 status=queued + queue_position=null）、`reset {}`、正文为空的 `snapshot`；delta/continue/自定义事件/带正文快照/多键 progress 一律拒绝，**不以持锁行补签任何正文**。持锁行须 `matches_running_base(领取身份, 行)`（同一执行在运行；断点/游标可由同一执行合法推进）——行是另一次领取（换执行键/输入/入队轮次）或仍排队时直接拒绝、不等待；同一执行仍在运行、只因已知合法推进而不再逐字匹配（`_same_execution_advancing`：单章保存事务已移除输入中的 `_chapter_claim` 指针而终态尚未提交，或已登记管理员取消意图）时不放宽匹配，抛 `TerminalEvidencePending` 退出租约等原 hub 的终态证明后按终态分支重验（此前直接拒绝会在保存→终态窗口误发 `permission_denied`，用例 `test_claim_frame_in_save_to_finalize_window_waits_for_terminal_proof`）；快照的任务摘要按持锁行（或同一执行在领取提交时的断点/游标，`_claim_time_task`，二者都是已提交事实）重算订阅投影（`to_out` + `project_task_out`）后除 hub 实时 progress/stage 外逐字一致。行已由同一执行提交终态时改用原 hub 登记的 `TerminalEvidence`（`same_execution` + 持久视图逐字一致；未登记则 `TerminalEvidencePending` 退出租约等待），领取来源下的终态事件只可能是首个事件前失败/取消/中断的 `error`（done/paused 必经已核验事件）。所有分支都在同一帧租约内按订阅原绑定 `_bound_task_visible` 复核并直接核对 `allow_owner_ai`（`_owner_ai_current`：协作任务依赖作者模型时，不只依赖成员轮次递增；排队快照原分支也补此核对），最后重验租约。③**领取瞬间**：受控订阅读到已提交 running 行、而本进程领取者尚未登记领取来源（领取提交与登记之间）时，`subscribe` 不持读取会话、在 `claim_source_wait_seconds`（5 秒）内等 `hub.source_changed`/终态（`_await_claim_source`，只读 hub 上引擎冻结的来源），之后按最新持久行重新投影正文为空的快照；超时或 hub 无来源关闭时失败关闭。④**排队快照恰逢领取**：排队快照（`TaskViewEvidence`，status=queued）投影后、发送前同一轮次被领取或结束时，`_verify_late_queued_snapshot` 只接受正文为空、动态字段为全新排队形状（无断点/错误/审核、游标 0、未开始未结束）的快照，不变字段（身份、类型、标题、关联 ID、Skill、创建时间）按持锁行重算投影后逐字一致，并按原绑定与作者模型授权复核；不证明任何正文。授权变化（撤权、降级、关作者模型授权、改密、重邀；有无通知；跨实例零通知）在任一时点只得 `permission_denied`：帧租约（认证发送屏障、成员原轮次与 editor 角色、Task 原绑定可见性）+ 分支内原绑定复核 + 作者模型授权直接核对三层，快照另有 `project_task_out` 的可见性复核；领取事务与撤权按共同锁序串行（领取先提交 → 领取帧发送时被拒；撤权先提交 → 领取在锁内复核后受限收尾为 failed，从不进入 running、不签发领取来源、模型零调用）。**作者路径**：作者生产订阅管线不变；`hub.source` 的赋值不发布事件，领取与排队的帧序列、内容与时序均不变（只在发布前多一次同步登记）。**禁止的改法**：领取来源在提交确认前签发、或不核对持锁行仍是本次领取的执行；用它证明 delta/带正文快照/自定义事件；在持帧锁时等待领取来源；把等待改为直接拒绝（会重新引入窗口）。回归：`tests/test_collaboration_stream_early_subscribe_e5e.py`（排队 / 领取瞬间 / 首个事件前 × 单章、分析、批量、向导、大纲、实体 × {编辑者, 作者}；排队位次帧；排队快照恰逢领取；三时点 × 五类变化 × 有无通知全量撤权矩阵与正对照；直接关作者模型授权；领取行身份与无输出形状的伪造拒绝）、`test_collaboration_stream_early_subscribe_e5e_postgresql.py`（领取事务先持锁 → 撤权在数据库层等待 → 领取帧发送时被拒；撤权先持锁 → 领取在数据库层等待 → 受限收尾；跨实例零通知；两订阅者与引擎领取并发不死锁）。
- **协作者任务流的生产入口与撤权通知（E5-f）**：`GET /tasks/{id}/stream` 先经 `get_owned_task` + `_capture_stream_binding` 捕获原绑定（非成员 / 待接受 / 已撤权 / 仅管理员人工权限 404；活动任务要求原轮次 editor，降为 viewer 只可订阅本人已结束任务）。`binding.member_grant` 非空（协作者，含管理员兼协作者）时一律走受控管线 `task_sse_response(engine.subscribe(guarded=True, 原绑定), binding)`，每帧在实际 ASGI send 的帧租约内按原绑定复核授权并由 `verify_stream_frame` 证明来源；只开放实现 `stream_guard` 的类型（`stream_evidence.stream_kind_open`：单章、分析、批量、向导、大纲、实体、一致性、智能体），知识库 / 拆书等协作者任务即使已结束也在入口 404（详情 / 输出接口不变）。作者与个人任务仍走原非受控管线，帧序列、内容与时序不变。没有可证明来源的帧（如引擎未运行时残留的运行中 hub）只得到 `permission_denied`，不泄露输出。帧租约对**活动绑定**的协作订阅另在锁内直接核对 `allow_owner_ai`（任务依赖作者模型时，不只依赖成员轮次递增）；已结束回放绑定不受此限。撤权通知：协作变更提交后 `notify_collaboration_change` 向本进程该作品 + 成员的受控订阅入队`__collaboration_check__`，订阅按数据库复核失效（含 `read_visible` 直接核对 `owner_ai_allowed`）即以 `permission_denied` 关闭（不依赖执行体被取消、不等 15 秒心跳）；跨实例的成员变更在提交后提示对端重查，通道不可用时按周期复查；每个受控内容帧仍在最终写出时核验。跨进程路由与断线语义见[任务订阅规范](task-stream-peers.md)。回归：`test_task_sse_scope.py::test_collaborator_stream_uses_guarded_pipeline_and_closed_kinds_stay_404`、`::test_active_member_frame_lease_rechecks_owner_ai_directly`、`test_collaboration_notifications.py::test_http_stream_wakes_after_project_grant_change`、`test_collaboration_task_privacy.py::test_finished_stream_is_authorized_in_http_and_denied_after_revocation`，PG `test_collaboration_notifications_postgresql.py`、`test_collaboration_task_postgresql.py::test_postgres_http_stream_rechecks_after_cross_instance_revoke`。
- **受控任务流的智能体轮次帧证明（2026-11-20，TODOS 第 22 项）**：`AgentHandler.stream_guard` 按当前已证明断点构造并缓存在 `_AgentExecutionState.publication_guard` 的 `publication_only=True` `SendGuard`（永不可用于发送/预检/写屏障）。`SendGuard._agent_stream_shape` 严格要求文本主绑定、至多一份同 grant 共享向量附加绑定、合法 `conversation_id`/`resource_key` 及同代次 `history_epoch`；`require_task_read` 与 `require_terminal_read` 在帧租约内调用 `_verify_agent_conversation` 核对 `AgentConversation` 归属、原邀请轮次与 `ai_history_epoch`（PostgreSQL `FOR SHARE`）；`stream_evidence._verify_agent_change_frame` 对 `proposal` / `executed` 帧锁内复核 `AgentChange` 存在且属于本任务/作品/会话。因 `agent.turn` 单次执行内会多次推进断点，`publication_successor` 与 `stream_evidence._has_successor_guard` 扩展覆盖 `agent.turn`（要求同执行/同绑定/同戳、`step_cursor == 0`、`round` 与 `tool_calls_used` 单调不回退），`TaskHub.append` 经 `_can_advance_text_source` 允许合法后继推进 `text_source` 以支持多轮中途重连快照，`runner.py` 在断点推进后经 `_refresh_stage` 重发当前阶段文案（文案不变、不广播、不触发 `_flush`，作者路径帧序列零变化）即时推进 `hub.source`；`engine.subscribe` 受控快照投影期间若任务正常推进（领取/刷新进度/终态）且原绑定仍有效，自动有界重试（至多 5 次）或转入持久回放，避免误报 `permission_denied`（期间失权则不重试、立即拒绝）。前端 `COLLABORATOR_STREAM_KINDS` 纳入 `'agent.turn'`，`useTaskStream` 为 `lost` 返回 `accessLost?: boolean`，`useAgentConversation` 放开已就绪且具 `canUseAgent` 能力的协作者实时订阅并在 `accessLost` 时立即触发 `accessLost()` 切只读或不可访问。禁止的改法：放宽 `publication_only` 守卫用于出站发送；跳过 `AgentChange` 或 `AgentConversation` 锁内复核；顺带开放 `kb.index` / `book.import`。回归：`test_collaboration_stream_agent.py`、`test_collaboration_stream_agent_postgresql.py`；前端 `useAgentConversation.test.ts`、`useTaskStream.test.ts`、`projectAccess.test.ts`。
- **SQLite 共享读帧租约（2026-11-20，TODOS 第 22 项）**：根因是出站 `_send_lease` 等待慢模型响应头期间持 SQLite `RESERVED` 写锁，旧 `_task_frame_lease` 对共享读也执行 no-op `UPDATE` 争抢同一写锁，响应头超过 `_LOCK_WAIT_SECONDS`（2 秒）或 `frame_timeout`（5 秒）即误断流。根治：`stream_lease._task_frame_lease` 在 SQLite 且 `PRAGMA journal_mode != "wal"` 时，以 `exec_driver_sql("BEGIN")` + `SELECT 1 FROM sqlite_master LIMIT 1` 建立真实 C 层 `SHARED` 读事务并登记到进程内 `WeakKeyDictionary` `_SQLITE_SHARED_FRAME_LEASES`（`finally` 无条件注销并关闭）；`is_sqlite_shared_frame_lease(db)` 严格校验 8 项物理身份（当前 `asyncio.Task`、活跃根事务、无嵌套事务、同一物理连接/事务/DBAPI 连接、`raw.in_transaction is True`、非 WAL）。SQLite `RESERVED` 与 `SHARED` 完全兼容（帧租约与慢模型发送 lease、以及多帧租约间零阻塞），且 `SHARED` 仍阻塞撤权等写方 `COMMIT`（需 `EXCLUSIVE`）。5 处受控帧租约路径接入 `is_sqlite_shared_frame_lease(db)`：`authorization_service.require_authenticated_for_send`、`ai_dispatch._lock(write=False)`（仍 `SELECT Project.id` 验证存在）、`ai_config_service.lock_config_owner(write=False)`（仍 `SELECT User.id` 验证存在）、`project_ai_config._validate_binding(lock_config=True)` 跳过无操作 `UPDATE`；`personal_projection._final_visibility` 在持共享帧租约时复用当前 `db` 连接执行只读核验（此时写方无法 COMMIT、快照冻结），防止写方 `COMMIT` 持 `PENDING` 锁时再开第二条 SQLite 连接形成 `SHARED → 新连接 → PENDING → SHARED` 三方死锁。若外部手工启用 WAL，`SHARED` 不阻塞写方提交，故自动回退既有写锁路径。已移除 `consistency/check.py` 与 `impact_task.py` 中的 `SQLITE_SEND_GAP` 临时休眠。禁止的改法：在 `_send_lease` 或任何 `write=True` 路径跳过 SQLite 写锁；在 WAL 模式下登记共享帧租约；在持 SQLite `SHARED` 帧租约期间新开第二条只读连接。回归：`test_sqlite_shared_frame_lease.py`、`test_consistency_stream_slow_model.py`。

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
| 服务停止 / 崩溃后重启 | 运行中 | 正常停止处理本地执行体。崩溃后等旧租约到期，依持久证据收尾；普通中断保留 `output_text` |
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
- `tests/test_tasks.py`：hub 快照无缝、两个订阅者 + 已结束快照、五种非活动状态绕过残留 hub 并回放真实输出 / 终态结果、订阅注册期间完成仍回放 done、创建请求刷新到终态不重建 hub、心跳、断开不影响任务（章节 + 向导）、取消运行中（<1 秒、流中断、
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
