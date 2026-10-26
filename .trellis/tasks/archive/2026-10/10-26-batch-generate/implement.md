# 执行计划：批量顺序生成

## 开工前

- 用 `ls backend/alembic/versions` 确认迁移编号。目前最新是 `0011_outline_pro`，本任务计划用 `0012_batch_generate`。确认 `tasks.status` 和 `tasks.kind` 没有 CHECK 约束。
- 阅读这些 spec：
  - `backend/error-handling.md` 中「后台任务与 SSE 契约」
  - `backend/database-guidelines.md` 中「后台任务表」
  - `backend/story-entities.md`：上下文组装和分析回写
  - `backend/outline.md`：细纲 `est_words`
  - `frontend/component-guidelines.md`：TaskPanel 和结果注册表
  - `frontend/type-safety.md`

## 有序清单

### 后端

1. [ ] 模型与迁移 `0012_batch_generate`：
   - 新增 `tasks.checkpoint` JSON 列；
   - `TASK_STATUSES` 加入 `paused`，新增 `OPEN_TASK_STATUSES`；
   - `TASK_KINDS` 和 schema 的 `TaskKind` 加入 `chapter.batch`；
   - `TaskOut` 新增 `checkpoint`；
   - downgrade 先把 paused 改为 cancelled，再删除该列。

   迁移测试按版本号补充。
2. [ ] 引擎：
   - 处理 `paused`、`reset`、`stage` 事件，增加 paused 收尾分支；
   - 订阅时把 paused 作为终止事件；
   - 取消落在暂停之后时，按 paused 处理。

   补引擎测试：
   - paused 不占名额，且调度器不会领取；
   - recover 不会动 paused；
   - 订阅已暂停的任务得到快照和 paused。
3. [ ] `task_service`：
   - 新增 `resume_task`、`finish_paused_task`；
   - cancel 支持 paused；
   - retry 复制 checkpoint；
   - delete 拒绝 paused；
   - 列表 `active=true` 时也返回 paused；
   - 补上 STAGE_LABELS。

   路由：`POST /tasks/{id}/resume`、`POST /tasks/{id}/finish`。
4. [ ] `generation_service`：
   - 抽出 `claim_chapter`，单章领取改用它，行为不变；
   - `validate_and_claim` 增加范围锁检查（第 4 节）。
5. [ ] `batch_service`：
   - 范围求值（按 `(chapter_no, id)` 排序，最多 200 章）；
   - 计算目标字数来源；
   - 预览（含预计耗时和警告）；
   - 校验未结束批量的唯一性；
   - `revalidate_resume`。
6. [ ] `handlers/batch.py`（`chapter.batch`）：
   - validate_and_claim、build_job、按第 5 节实现 run 循环（跳过、领取、带退避重试的生成、任务内分析、推进、审核点）；
   - progress、on_abort。

   退避时长做成模块常量，测试中改为 0。
7. [ ] 接口：`POST /projects/{pid}/batch-generate/preview` 和 `POST /projects/{pid}/batch-generate`（创建任务，返回 202）。
8. [ ] 测试 `test_batch_generate.py`，对应 PRD 的全部 AC：
   - 默认跳过已写章节；包含已写时重写，且留有版本记录；
   - 顺序生成，第 N 章的 prompt 包含第 N-1 章正文，以及第 N-1 章分析回写后的角色状态；
   - 关闭自动分析时不分析；分析失败只记警告，批量继续；
   - 生成失败重试 2 次后：默认任务失败，cursor 停在失败章，重试后从该章续跑，已完成章节不重写；勾选 skip_on_fail 时继续，结果列出失败章节；
   - 审核点：进入 paused，不占名额；暂停期间范围内章节可以单独生成；继续后同一任务从下一章接着写，并能用上审核期间的修改；结束后任务记为 succeeded 且 ended_early；暂停期间新建批量返回 409；
   - 范围锁：运行中的批量范围内单章生成返回 409，范围外不受影响；
   - 唯一性：同一项目第二个批量返回 409；
   - 取消：已完成章节保留，当前章节恢复 prev_status；
   - 中断：调用 recover 后为 interrupted，当前章恢复原状态；重试后，断点停在分析阶段的章节只补做分析，不重写；
   - 目标字数：细纲 `est_words` 和统一字数覆盖两种情况；
   - 预览：清单、跳过数、警告；
   - 跨用户访问返回 404；
   - 现有任务、生成、分析测试仍然通过。
9. [ ] spec：
   - `error-handling.md`：任务契约增加 paused 状态、resume/finish、新事件、checkpoint；
   - `database-guidelines.md`：迁移列表，以及任务状态集合（ACTIVE、OPEN、FINISHED）；
   - 新增「批量生成」小节，写在 `outline.md` 或新建 `batch-generate.md`，说明范围锁、断点结构和任务内分析的约定。

### 前端

10. [ ] 更新 `types.ts` 和 `api/tasks.ts`：
    - 加入 paused 和 `chapter.batch`；
    - 新增 `resumeTask`、`finishTask`；
    - 流解析器支持 paused、reset、stage、chapter_start、chapter_end；
    - 补标签和 `taskLink`。

    `api/chapters.ts` 新增 `previewBatch` 和 `startBatch`。
11. [ ] `utils/batch.ts` 纯函数：默认起点、横幅状态推导、清单行文案、预计耗时文案。
12. [ ] `BatchGenerateModal`：带预览的发起弹窗。
13. [ ] `BatchProgressView`，注册到结果查看器；`ChaptersPanel` 加入工具栏按钮、横幅和"排队批量"标签。
14. [ ] `TaskPanel`：显示"待审核"标签以及"继续"和"结束"按钮；`useTaskStream` 和 store 处理 paused 终止。
15. [ ] 测试（纯函数）：
    - 默认起点；
    - 横幅状态（运行中、待审核、失败可续跑）；
    - 流解析器对 paused 和 reset 的处理；
    - 清单行文案。
16. [ ] spec：`frontend/component-guidelines.md` 和 `type-safety.md`。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收（用假 AI 服务）：
- 6 章、其中 1 章已写、审核间隔 2：运行后跳过已写章节，写完 2 章进入待审核；审核期间手动改第 2 章，点继续后，第 3 章的 prompt 中能看到改动；最后完成。
- 让假服务在第 4 章返回 500：默认失败，点重试后从第 4 章续跑；勾选"失败时跳过继续"后跑完，结果列出第 4 章。
- 运行中重启后端：任务标为已中断，当前章恢复原状态，重试后续跑。
- 包体积不超过上限。

## 审查关卡与回滚点

- 第 2–3 步（引擎和 task_service）完成后先跑全部任务相关测试，确认没有回归，再开始写 handler。
- 回滚：迁移 downgrade（paused → cancelled，再删列）。各步改动彼此独立，可以按文件回退。
