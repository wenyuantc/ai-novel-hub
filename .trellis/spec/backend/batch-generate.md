# 批量顺序生成（`chapter.batch`）

> 来源任务：`10-26-batch-generate`（迁移 `0012`）。任务 / 订阅契约见 [error-handling.md](./error-handling.md)「后台任务与 SSE 契约」，
> 任务表与状态集合见 [database-guidelines.md](./database-guidelines.md)「后台任务表」，上下文与分析回写见 [story-entities.md](./story-entities.md)。

---

## 场景：一个任务内逐章生成 + 分析，可断点续跑、可在审核点暂停

### 1. 范围 / 触发条件

`batch_service.py`、`task_engine/handlers/batch.py`、`api/v1/batch.py`、`schemas/batch.py`，以及 `generation_service.validate_and_claim`
中的范围锁。以后任何「多步、跨步有状态」的任务类型（拆书等）都应复用这里的断点（`step_cursor` + `checkpoint`）与暂停（`paused`）模式，
而不是拆成 N 个串联的子任务。

### 2. 签名

```python
# backend/app/schemas/batch.py
class BatchGenerateRequest:  # start_chapter_id, end_chapter_id, include_written=False, target_words=3000 (300–20000),
                             # use_plan_words=True, auto_continue=True, review_every=0 (0–50), skip_on_fail=False,
                             # instruction ≤2000, ai_config_id?, style: inherit|none|风格 id（逐章解析，续跑沿用，见 prompts-styles.md）
class BatchPreview: total, items[{chapter_id, chapter_no, title, target_words, words_source: plan|uniform, written}],
                    skipped[{chapter_id, chapter_no, title, word_count}], analyze, estimated_minutes, warnings

# backend/app/services/batch_service.py
KIND = "chapter.batch"; MAX_CHAPTERS = 200; MAX_RETRIES = 2; RETRY_DELAYS = (5.0, 15.0)  # 测试 monkeypatch 为 0
async def preview(db, settings, user_id, project_id, req) -> BatchPreview          # 不建任务
async def validate_start(db, ctx, *, user_id, project_id, req) -> dict             # TaskSpec 字段，不修改数据
async def revalidate(db, ctx, task) -> int                                          # 重试 / 继续前，返回 ai_config_id
async def build_batch_job(ctx, task) -> BatchJob
async def run_batch_job(job, ctx) -> AsyncIterator[Event]
async def restore_current(ctx, task_id) -> None                                     # on_abort
def summarize(checkpoint, cursor, total) -> dict; def finish_result(task) -> dict

# generation_service（单章入口共用）
async def claim_chapter(db, chapter) -> str            # 原子置 generating，返回原状态；冲突 → rollback + 409；不 commit
async def ensure_outside_batch(db, chapter) -> None    # 范围锁，409

# 路由
POST /projects/{pid}/batch-generate/preview  -> BatchPreview
POST /projects/{pid}/batch-generate          -> 202 TaskOut
POST /tasks/{id}/resume  -> 202 TaskOut（paused → queued，同一任务）
POST /tasks/{id}/finish  -> 200 TaskOut（paused → succeeded，result.ended_early = true）
```

### 3. 契约

- **范围**：两端章节须属于该项目（否则 400 `起止章节不存在或不属于该项目`），起始章号 ≤ 结束章号（400 `起始章节不能晚于结束章节`）；
  按 `chapter_no` 两端包含、`(chapter_no, id)` 排序定下 `input.chapter_ids`（> 200 章 → 400）。之后插入的章节不纳入；
  还没建章的细纲不会自动建章（预览给 warning）。
- **任务**：`resource_key = batch:{project_id}`、无 `serial_key`、`chapter_id = None`、`input = {request, chapter_ids, ai_config_id}`、
  标题 `批量生成 第5–24章`。同项目最多一个**未结束**（queued / running / paused）的批量：paused 不受部分唯一索引约束，
  由服务层 `open_batch` 检查 → 409 `已有进行中或待审核的批量生成`。
- **创建校验**：AI 配置（`resolve_config` + `build_provider`）；默认跳过已写后一章都不用生成 → 400 `范围内没有需要生成的章节`；
  要生成的章节有 `generating` → 409（列出章号）。
- **断点**：`step_cursor` = 下一章在 `chapter_ids` 中的下标；`checkpoint = {phase: generate|analyze, current: {chapter_id, prev_status}|null,
  since_review, items: [{chapter_id, chapter_no, title, state: pending|done|skipped_written|skipped_missing|failed, target_words,
  words_source, words, analysis: ok|skipped|failed|null, warning, error}]}`（items 与 chapter_ids 一一对应）。每推进一个阶段单独提交，
  中断后最多重做当前章的当前阶段；`phase=analyze` 的章节续跑时只补做分析，不重写。`TaskOut.checkpoint` 返回它（其他类型为 null），`TaskOut.step_cursor` 返回断点下标（单步类型恒为 0），前端「从第 N 章续跑」直接取 `checkpoint.items[step_cursor]`。
  抢占当前章（`claim_chapter`）与写入 `checkpoint.current` 在**同一事务**提交，崩溃恢复时 `on_abort` 一定能按断点还原该章。
- **逐章流程**：跳过判断（章节已删 → `skipped_missing`；有正文且未勾选包含已写 → `skipped_written`）→ `claim_chapter` 并提交 `current` →
  `build_chapter_job` + `run_chapter_job`（**不复制生成逻辑**；目标字数：`use_plan_words` 且章节有细纲 → `est_words`（夹到 300–20000），
  否则 `target_words`）→ `phase=analyze` 提交 → 分析 → 推进（cursor+1、`since_review+1`）。
- **失败**：生成失败最多重试 2 次（退避 `RETRY_DELAYS`；重试前重新领取，因为 `run_chapter_job` 失败时已回退状态）；`build_chapter_job`
  抛 AppError / 章节已删 → 不重试。仍失败：`skip_on_fail` → item `failed` 并继续；否则 cursor 停在该章、产出 `error`
  `第 N 章生成失败：<detail>`，任务 failed，重试从该章续跑。
- **分析**：在本任务内串行执行 `build_analysis_job` + `run_analysis_job`（回写随分析落库），**不排队、不占额外名额、不调用 `enqueue_auto`**；
  部署开关 `AUTO_ANALYZE_ENABLED` 或项目 `auto_analyze` 关闭 → `skipped`；失败（含 AppError）→ `failed` + warning，不阻塞批量、不重试。
  分析原文不进入任务输出。与手动分析其他章节并发时依赖回写的「更晚章节优先」规则。
- **上下文**：每章的 ChapterJob 在循环里临时组装，第 N 章看到第 N-1 章正文与其分析回写后的角色状态 / 伏笔；审核期间的修改在继续后生效。
- **0039场景闭环**：仍逐章checkpoint，不改成逐Scene断点。每章内部沿稳定Scene顺序暂存、全主成功后一次内容CAS提交，身份／摘要／规划不变；当前章失败／提交前取消不半写，已完成前章保留。每景有效规划／scene start与暂存前景由原ChapterJob统一构建，不复制生成逻辑。
- **快照失效与跳过区别**：每次真实出站和保存前核验同一内容／项目规划／相关设定stamp。首轮飞行中删未来章、改未来标题若推进planning_revision，会使当前旧快照失效，不能重签后强行成功；进入下章快照前发现章已删除仍正常`skipped_missing`。普通额外续写网络／渲染失败仍warning，不能与安全快照撤回混为一谈。
- **审核点**：`review_every > 0` 且 `since_review >= review_every` 且后面还有需要生成的章节 → `since_review` 归零并产出 `paused`
  （载荷 = 汇总）→ 任务 `paused`（`stage = 待审核`、`finished_at` 为空）。待审核**不锁范围**：范围内章节可单独生成 / 编辑。
  继续 = `revalidate` 后条件 UPDATE `paused → queued`（清 `result/error/finished_at`），同一任务 id、断点保留；结束 = `succeeded` + `ended_early`。
- **范围锁**：`chapter.generate / polish / revise` 的 `validate_and_claim` 在「章节已在生成中」之后检查：同项目 queued / running 的批量
  且本章 ∈ `chapter_ids[step_cursor:]` → 409 `该章节在批量生成范围内，请等待批量完成或暂停后再操作`。读后判断，竞争由
  `generating` 原子抢占兜底（批量走到被占用的章节时按失败处理）。手动编辑与局部改写不受限制。
- **取消 / 中断**：`on_abort` 读库里最新的 `checkpoint.current`，章节仍为 `generating` 时还原 `prev_status`（幂等）；已完成章节保留；
  `output_text` 保留当前章的实时文本。`recover()` 只处理 running，paused 不受影响。
- **重试**：handler 的 `validate_retry` 代替 `validate_and_claim`：沿用原 `chapter_ids`，不再要求「还有未写章节」，`step_cursor` / `checkpoint`
  由 `create_task` 从原任务复制；`build_batch_job` 清掉遗留的 `current`。
- **事件**：`reset`（每章开始 / 重试前清空实时输出，`output_text` 只保留当前章）、`chapter_start {index, total, chapter_id, chapter_no, title,
  target_words}`、`chapter_end {index, item}`、`stage`（`第 3/20 章 · 生成中 | 分析中 | 生成失败，第 n 次重试`）、`delta` / `continue`
  （转发单章）、`paused` / `done`（汇总）、`error`。进度 = `(当前下标 + min(1, 字数/目标) × 0.8) / 总数`，单调、≤ 95。
- **汇总**（`done.result` / `paused` / 结束）：`{total, done, skipped_written, skipped_missing, failed: [{chapter_id, chapter_no, title, error}],
  analysis_failed: [{…, warning}], remaining, warnings}`，结束时另有 `ended_early: true`。
- **预览**：`items` / `skipped` / `analyze` / `estimated_minutes = ceil(Σ target/3000 × 2 + (分析 ? 1 : 0))`；warnings：没有需要生成的章节、
  范围内有章节正在生成、有细纲还没建章、项目关闭了自动分析、已有进行中或待审核的批量。预览不校验 AI 配置。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 项目不存在 / 他人 | 404 `项目不存在` |
| 起止章节不属于该项目 / 起始晚于结束 / > 200 章 | 400 |
| 没有 AI 配置 / SSRF / 解密失败 | 400（不建任务） |
| 一章都不用生成 | 400 `范围内没有需要生成的章节` |
| 要生成的章节在生成中 | 409 `范围内有章节正在生成中：第 N 章，请等待其完成后再试` |
| 同项目已有未结束批量（含待审核） | 409 `已有进行中或待审核的批量生成` |
| 运行中批量范围内单章生成 / 润色 / 重写 | 409 范围锁 |
| 继续 / 结束非待审核任务 | 409 `只有待审核的任务可以继续` / `只有待审核的任务可以结束` |
| 删除待审核任务 | 409 `任务待审核，无法删除，请先结束或取消` |
| `review_every` > 50 等 | 422 |

### 5. Good / Base / Bad

- Good：4 章、审核间隔 2 → 写完 2 章 `paused`；审核期间改第 2 章；`resume` → 第 3 章提示词含改动；最后一章写完直接 succeeded。
- Base：第 2 章三次 `AIProviderError` → failed、cursor 停在 1；`retry` 从第 2 章续跑，第 1 章不重写。
- Bad：批量里每章 `create_task("chapter.generate")` + 自动分析排队——每用户 2 个名额会被分析占满而死锁，且第 N 章看不到第 N-1 章的回写。

### 6. 必需的测试

`tests/test_batch_generate.py`（跳过 / 包含已写 + 版本、顺序上下文 + 回写、项目 / 部署关闭分析、分析失败只警告、失败停下 + 重试续跑、
skip_on_fail、第二次尝试成功、审核点继续 / 结束 / 取消、待审核不锁范围 / 不可删 / recover 不动 / 不能再发起、范围锁与唯一性、
取消、中断后续跑（含只补分析）、崩溃恢复、目标字数、预览、校验、进度单调、组装阶段业务错误不重试且还原当前章、批量中途删章 → skipped_missing、
待审核不占每用户名额（`task_max_per_user=1`）、`step_cursor` 停在失败章 / 待分析章）；`tests/test_migrations.py::test_0012_batch_generate`；
`test_cross_user_isolation`（两个批量路由）。

### 7. 错误写法 vs 正确写法

```python
# 错误：在批量里复制一份生成逻辑（漏掉 baseline 版本、生成记录、续写、失败回退）
text = "".join([p async for p in provider.generate_stream(messages, model)])
chapter.content = text
# 正确：claim_chapter → build_chapter_job → run_chapter_job，由单章实现负责版本、记录与回退
```
