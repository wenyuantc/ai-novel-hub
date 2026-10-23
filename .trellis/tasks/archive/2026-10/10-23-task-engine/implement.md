# 执行计划：统一持久化后台任务

## 开工前

- 用 `ls backend/alembic/versions` 确认下一个迁移编号（`0005` 已被 chapter-versions 占用；封面任务如果已经合入，就再往后顺延）。
- 阅读 `.trellis/spec/backend/error-handling.md`（SSE 契约）、`database-guidelines.md`、`quality-guidelines.md`，以及 `.trellis/spec/frontend/component-guidelines.md`（章节编辑器、向导）、`hook-guidelines.md`、`state-management.md`。

## 有序清单

### 后端：引擎

1. [ ] 模型：`Task`；`GenerationLog.task_id`。迁移 `0006_tasks`：新表、索引、部分唯一索引，加上新列。在 `tests/test_migrations.py` 补测试，用指定版本号的方式升级和降级。
2. [ ] `Settings`：`task_max_concurrency`、`task_max_per_user`；同步写进 `.env.example`。
3. [ ] `task_engine/hub.py`：缓冲区、订阅（同步临界区内完成快照和注册）、广播、订阅队列上限、节流刷库。
4. [ ] `task_engine/registry.py`：定义 `TaskHandler` 协议。`engine.py` 负责：start（先恢复再调度）、stop（标记关停中断后 cancel）、notify、调度循环（全局和每用户上限，条件 UPDATE 领取）、`_run`（消费事件，写状态、进度、结果）、cancel（排队或运行中）。
5. [ ] `handlers/chapter.py`：把 `generation_service` 拆成 `validate_and_claim` 和 `build_job` 两步，`run` 复用 `run_chapter_job`。`ChapterJob` 增加 `task_id`，写入生成记录。`on_abort` 负责还原章节状态。
6. [ ] `handlers/wizard.py`：同样拆分 `wizard_service`；`resource_key = wizard:{pid}:{kind}`。
7. [ ] `task_service.py` 和 `api/v1/tasks.py`：列表（含 queue_position）、详情、output、stream（SSE，包括心跳）、cancel、retry、删除、清理、保留裁剪（每个用户最多 200 个）。所有入口都要做归属校验，不满足返回 404。
8. [ ] 改造 `api/v1/chapters.py` 和 `wizard.py`：generate、polish、wizard 改为返回 202 `TaskOut`。资源冲突（部分唯一索引或章节抢占失败）返回 409。
9. [ ] `main.py` 的 lifespan 中启动和停止引擎；`create_app` 增加开关，测试 fixture 显式启停。
10. [ ] 测试：
    - `FakeLLM` 增加可阻塞的条目，`helpers` 增加 `wait_task`。
    - 把原有的生成、润色、向导测试改写为任务版本，覆盖的行为不能减少：
      - 落库、版本、生成记录、续写、警告
      - 错误时回退状态
      - 409 冲突
      - 显式指定 ai_config
      - 跨用户访问
    - 新增 `tests/test_tasks.py`，覆盖：
      - AC2：订阅快照加增量无重复，第二个订阅者结果一致，已结束任务的快照
      - AC3：取消运行中任务（流停止、状态复位、生成记录标为 cancelled），以及取消排队中任务
      - AC4：stop 后 start，运行中任务变为 interrupted，排队中任务继续执行；重试后关联原任务并成功完成
      - AC5：每用户 2 个上限、全局上限、同章节冲突返回 409、同类向导冲突返回 409、queue_position 正确
      - AC8：跨用户访问 9 个任务端点都返回 404
      - AC9：保留数量的裁剪
11. [ ] 更新 spec：
    - `backend/error-handling.md`：SSE 契约改为任务订阅事件表，说明 generate、polish、wizard 返回 202，rewrite 仍是同步。
    - `backend/database-guidelines.md`：`tasks` 表、部分唯一索引、单进程恢复前提。
    - `backend/directory-structure.md`：`task_engine` 包。

### 前端

12. [ ] `api/types.ts`（Task 相关类型）、`api/tasks.ts`（CRUD 和 `subscribeTask` 自动重连）；`chapters.ts` 和 `wizard.ts` 的 generate、polish、wizard 改为返回 `Task`。
13. [ ] `hooks/useTaskStream.ts`、`stores/tasks.ts`（轮询节奏，登出时清空）。
14. [ ] `components/TaskPanel.tsx`，挂到 `AppLayout`。
15. [ ] 章节编辑器：生成和润色走任务；挂载时自动接上进行中的任务；停止改为取消任务；显示排队位置；离开守卫调整（AI 任务进行中允许离开）。局部改写不变。
16. [ ] 向导页：`WizardGenerator` 走任务，挂载时自动接上。
17. [ ] 测试：
    - `api/tasks.test.ts`：subscribe 的快照覆盖、重连、done 和 error 的处理。
    - store 的轮询节奏（纯函数部分）。
18. [ ] 更新 spec：`frontend/component-guidelines.md`（编辑器第 2、3、5、6 条的变化，向导，TaskPanel）、`hook-guidelines.md`（useTaskStream）、`state-management.md`（tasks store）、`type-safety.md`。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收：用假 AI 服务（流式输出变慢）在本地启动后，依次验证：

1. 发起生成后关闭标签页，再重新打开，能接续上。
2. 两个标签页同时订阅，看到的内容一致。
3. 取消任务。
4. 重启后端，运行中的任务显示为"已中断"，然后点重试。
5. 同一用户同时发起 3 个任务，第 3 个进入排队。
6. 在任务面板里执行各项操作。
7. 向导页关闭后重新打开，能接续上。

## 风险与回滚点

- 改动面最大的是 `generation_service` 和 `wizard_service` 的拆分，以及原有测试的改写。先改后端并让测试全部通过，再动前端。
- 测试里引擎跑在真实的事件循环上。注意 fixture 结束前要 `stop`，避免任务泄漏到下一个测试。
- 回滚：代码回退，然后执行 `alembic downgrade -1`。
