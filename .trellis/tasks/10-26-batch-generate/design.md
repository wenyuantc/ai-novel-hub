# 技术设计：批量顺序生成

## 1. 总体形态

新增任务类型 `chapter.batch`，整个批量就是一个任务。`run` 内按章节顺序循环：

```
for i in range(cursor, len(chapter_ids)):
    检查跳过条件 → 领取章节（置 generating） → 生成（含重试） → 分析（同一任务内） → 推进断点
    到审核点 → 产出 paused 事件并结束本次运行
```

逐章复用现有实现，不复制生成逻辑：

- `generation_service.validate_and_claim` 中的"原子置 generating"部分，抽成 `claim_chapter(db, chapter)`，返回 prev_status。
- `generation_service.build_chapter_job` 和 `run_chapter_job` 原样调用。`run_chapter_job` 失败时会自己恢复章节状态并写生成记录；成功时会存 baseline 和 generate 版本。
- `analysis_service.build_analysis_job` 和 `run_analysis_job` 原样调用，回写在分析成功落库时完成。

批量不调用 `enqueue_auto`，因为分析已经在任务内做过；也不使用分析的 serial_key。和手动分析其他章节并发时，依靠回写中已有的"较晚章节优先"规则保证正确。

## 2. 数据模型（迁移 `0012_batch_generate`）

- `tasks.checkpoint`：JSON，可为空。多步任务的结构化断点。重试和继续时连同 `step_cursor` 一起复制。
- `TASK_STATUSES` 新增 `paused`。它不属于 `ACTIVE_TASK_STATUSES`，所以不占 resource_key 部分唯一索引，调度器也不会扫到；它也不属于 `FINISHED_TASK_STATUSES`，所以不会被裁剪，也不能删除。新增常量 `OPEN_TASK_STATUSES = ACTIVE + ("paused",)`。
- `TASK_KINDS` 新增 `chapter.batch`。`status` 和 `kind` 列没有 CHECK 约束（迁移前再确认一次），只改常量。

`step_cursor` 指向下一章在 `chapter_ids` 中的下标。`checkpoint` 的结构：

```json
{
  "phase": "generate" | "analyze",      // cursor 所指章节所处的阶段；analyze 表示正文已写好、还没分析
  "current": {"chapter_id": 12, "prev_status": "draft"} | null,  // 正在生成的章节，用于 on_abort 回退
  "since_review": 2,                      // 距上个审核点已生成的章数
  "items": [                              // 每章的结果，与 chapter_ids 一一对应，可追加
    {"chapter_id": 10, "chapter_no": 5, "state": "done"|"skipped_written"|"skipped_missing"|"failed"|"pending",
     "words": 3120, "analysis": "ok"|"skipped"|"failed"|null, "warning": "...", "error": "..."}
  ]
}
```

每推进一个阶段就单独提交一次 `step_cursor` 和 `checkpoint`，任务中断后最多重做当前这一章的当前阶段。

## 3. 请求与校验（`validate_and_claim`）

请求 `BatchGenerateRequest` 的字段：

- `project_id`；
- `start_chapter_id` 和 `end_chapter_id`（按 chapter_no 确定范围，两端都包含）；
- `include_written`，默认 false；
- `target_words`，默认 3000，范围 300–20000；
- `use_plan_words`，默认 true；
- `auto_continue`，默认 true；
- `review_every`，0 表示不暂停，1–50 表示每 N 章暂停；
- `skip_on_fail`，默认 false；
- `instruction`，统一附加要求，可选；
- `ai_config_id`，可选。

校验内容：

- 鉴权：项目归属，两端章节都属于该项目，起始章号不大于结束章号。
- 模型配置能解析出来，并通过 SSRF 校验。
- 范围内的章节按 `(chapter_no, id)` 排序后得到 `chapter_ids`，最多 200 章。
- 默认跳过已写章节后，如果一章都不用生成，返回 400"范围内没有需要生成的章节"。
- 范围内有章节处于 generating 时，返回 409，并列出这些章节的章号。
- 项目已有未结束的批量（状态为 queued、running 或 paused）时，返回 409"已有进行中或待审核的批量生成"。

TaskSpec：

- `resource_key = batch:{project_id}`；
- 没有 serial_key；
- `chapter_id = None`；
- `input = {request, chapter_ids, ai_config_id}`；
- 标题形如"批量生成 第5–24章"。

另提供预览接口 `POST /projects/{pid}/batch-generate/preview`，参数与发起请求相同，不创建任务。返回：

- 将生成的章节（含章号、标题、目标字数和来源：细纲或统一字数）；
- 跳过的已写章节；
- 预计分钟数：每章 `target/3000 × 2` 分钟，开启分析时再加 1 分钟；
- 警告：范围内有章节正在生成、有细纲还没建章、项目关闭了自动分析。

## 4. 范围锁（批量期间单章生成返回 409）

`generation_service.validate_and_claim` 增加一项检查：同项目存在状态为 queued 或 running 的 `chapter.batch` 任务，且本章位于它的 `chapter_ids[step_cursor:]` 中，则返回 409"该章节在批量生成范围内，请等待批量完成或暂停后再操作"。

- 待审核（paused）不在检查范围内，所以审核期间范围内的章节可以单独生成或重写。
- 这个检查只是尽力而为（读后判断），不加锁。同时并发发起时，靠后续逐章领取时的 generating 原子抢占兜底：批量走到某章时如果它正被别的任务生成，就按失败处理，走 Q2 的失败策略。
- 只有 `chapter.generate`、`chapter.polish`、`chapter.revise` 走这个检查。手动编辑和局部改写不受限制。

## 5. 执行流程（`build_job` / `run`）

`build_job` 只读出 task.input、step_cursor 和 checkpoint，组成 BatchJob。每章的 ChapterJob 在 `run` 循环里临时组装，这样第 N 章用到的是第 N-1 章回写之后的数据库状态。

每一章 i 的处理步骤：

1. **跳过判断**（phase 为 generate 时）：
   - 章节已被删除，记为 `skipped_missing`。
   - `content` 非空且没有勾选 `include_written`，记为 `skipped_written`。

   命中任一条件就推进 cursor，处理下一章。
2. **领取**：执行条件 UPDATE 把章节置为 generating。没抢到（正被其他任务生成）时按失败处理。领取成功后，把 `checkpoint.current` 写入并提交。
3. **生成**：
   - 目标字数：勾选 `use_plan_words` 且章节有细纲时用细纲的 `est_words`，否则用 `target_words`。
   - 构造 `GenerateChapterRequest(target_words, instruction, auto_continue, ai_config_id)` 后调用 `build_chapter_job` 和 `run_chapter_job`。
   - delta 照常转发。
   - 失败时最多重试 2 次，两次之间分别等 5 秒和 15 秒（常量可改，测试中改为 0）。重试前要重新领取，因为 `run_chapter_job` 失败时已经恢复了原状态。
   - 最终仍失败：勾选了 `skip_on_fail` 时记为 `failed` 并继续下一章；否则产出 `error`，任务失败，cursor 停在这一章，重试时从这里开始。
   - `build_chapter_job` 抛出 AppError 时（例如配置被删），直接按失败处理，不重试。
4. **分析**：生成成功后先把 phase 设为 analyze 并提交。然后：
   - 项目关闭自动分析或部署开关关闭时，analysis 记为 skipped；
   - 否则调用 `build_analysis_job` 和 `run_analysis_job`，成功记为 ok；失败或抛出 AppError 时记为 failed，并写入 warning，流程继续。

   分析阶段不重试。
5. **推进**：cursor 加 1，phase 回到 generate，`current` 置空，`since_review` 加 1，然后提交。
6. **审核点**：`review_every > 0`、`since_review >= review_every`，并且后面还有需要处理的章节时，把 `since_review` 归零，产出 `paused` 事件后结束。

全部处理完后产出 `done`，result 是汇总：`{total, done, skipped_written, skipped_missing, failed: [...], analysis_failed: [...], warnings}`。

### 输出、进度与事件

- 章节开始时产出 `chapter_start {index, total, chapter_id, chapter_no, title, target_words}`；章节结束时产出 `chapter_end {index, item}`；分析开始时产出 `stage`。
- 引擎新增两个通用事件：
  - `reset`：清空 hub.text 并广播，这样 output_text 只保留当前章的实时正文，避免 200 章的正文全部堆进 output_text；
  - `stage`：只更新 hub.stage，用于显示"第 3/20 章 · 分析中"。
- `progress(task, text)` 对批量返回基于断点的值：`(已处理章数 + 当前章文本进度 × 0.8) / total`。因为 progress 拿不到 hub 内存态，所以在 handler 内用一个 `task.id → 已处理章数` 的字典辅助计算；也可以把 progress 改为只看 `chapter_start` 事件，实现时选一种，测试只断言进度单调且不超过 95。
- `chapter_end` 事件中的 item 同时写入 checkpoint（已提交）。前端通过 `GET /tasks/{id}` 拿 checkpoint 渲染章节清单，SSE 用来实时更新。
- `TaskOut` 新增 `checkpoint` 字段。只有 `chapter.batch` 返回它，其他类型为 null。

## 6. 引擎改动（暂停、继续、结束）

- `_consume`：遇到 `paused` 事件时设置 `hub.paused = data`，按成功路径结束消费。
- `_run` 和 `_finalize` 增加 `paused` 分支：
  - 写入 `status=paused`、`stage="待审核"`、`result=data`；
  - 不调用 on_abort（章节已在当章结束时释放），也不调用 on_success；
  - 关闭 hub 时发出 `paused` 事件。
- 订阅：`paused` 也算终止事件。订阅已暂停的任务时，先给快照，然后发出 `paused`。
- 取消落在暂停之后：如果 hub.paused 已经设置，按 paused 收尾，处理方式与 done 已产出时相同。
- `recover()` 只处理 running，paused 不受影响。

新增 `task_service` 入口：

- `resume_task`（`POST /tasks/{id}/resume`）：只允许 paused 任务。
  1. 重新执行 handler 的 `revalidate_resume`：项目和配置仍然有效，范围内剩余章节没有处于 generating，项目没有其他未结束的批量。
  2. 用条件 UPDATE 把状态从 paused 改为 queued，同时清空 finished_at 和 error，设置 stage 为排队中；用 IntegrityError 兜底 resource_key 冲突，返回 409。
  3. 调用 `ensure_hub` 和 `notify`。

  继续运行的是同一个任务 id，cursor 和 checkpoint 都保留。
- `finish_paused_task`（`POST /tasks/{id}/finish`）：从 paused 改为 succeeded，result 为汇总，并加上 `ended_early: true` 和剩余未处理章节数。
- `cancel_task`：允许 paused 任务转为 cancelled，on_abort 无事可做。
- `retry_task`：复制 checkpoint，`RETRYABLE_STATUSES` 不变。
- `delete_task`：paused 不属于 FINISHED，会被拒绝，提示先结束或取消。
- 列表接口 `active=true` 时也返回 paused 任务（改用 OPEN 集合），这样任务面板的"进行中"页签能看到待审核的任务。

`on_abort`（失败、取消、中断时调用）：如果 `checkpoint.current` 存在，并且该章节仍是 generating，就恢复为 prev_status。这个操作是幂等的。

## 7. 前端

- `types.ts` 和 `api/tasks.ts`：
  - TaskStatus 增加 `paused`，TaskKind 增加 `chapter.batch`，补上标签；
  - `isOpenStatus` 包含 paused；
  - 新增 `resumeTask` 和 `finishTask`；
  - 流解析器把 `paused` 作为终止事件，并处理 `reset`、`stage`、`chapter_start`、`chapter_end`；
  - `taskLink` 跳到章节页。
- `api/chapters.ts` 新增 `previewBatch` 和 `startBatch`。
- `ChaptersPanel`：
  - 工具栏增加"批量生成"按钮，默认起点是第一个空章节；
  - 顶部横幅显示本项目未结束的批量。运行中显示进度、当前章和取消按钮；待审核显示已完成 x/y、"继续"和"结束"；失败或中断显示"从第 N 章续跑"（即重试）。
  - 章节行在批量范围内且还没处理时，显示"排队批量"标签。
- `BatchGenerateModal`：
  - 范围选择：起止章下拉，外加"生成到第 N 章"的快捷输入；
  - 选项：包含已写、统一字数、使用细纲字数、自动续写、审核间隔（0–50）、失败时跳过继续、附加要求、模型；
  - 参数变化时防抖调用预览，展示章节清单、跳过数、预计耗时和警告。
- `BatchProgressView`：章节清单，包含状态、字数和分析结果，外加当前章的实时正文。在横幅展开和 TaskPanel 的结果查看器中复用，并注册到 `taskResultViewers`。
- `TaskPanel`：paused 任务显示"待审核"标签，以及"继续"和"结束"按钮。
- 纯函数放在 `utils/batch.ts`：默认起点、范围求值（与后端预览结果一致，仅用于即时提示）、清单行文案、横幅状态推导。

## 8. 取舍

- **单任务循环，而不是 N 个串联的子任务**：失败暂停、断点续跑、审核点和"任务内分析"都需要跨章状态，放在单任务里最简单；代价是要扩展引擎的 paused 状态。
- **分析放在批量任务内**：避免每用户 2 个并发名额被占满时互相等待造成死锁，也保证第 N 章能看到第 N-1 章的回写。代价是不受分析 serial_key 约束。
- **范围锁用读后检查**：不在 resource_key 层面锁住整段范围（一个任务只有一个 resource_key），竞争由章节的 generating 原子抢占兜底。
- **output_text 只保存当前章**：每章正文已落库，有版本记录，任务输出不需要重复保存。

## 9. 兼容与回滚

- 迁移只新增可空列，下降时删除该列。代码回滚前需要把 paused 任务改为 cancelled，在迁移 downgrade 里执行。
- 旧任务的 checkpoint 为 null，不受影响。
- 前端旧版本遇到未知状态时只是显示不了标签，不会崩溃（打包会同步更新）。
