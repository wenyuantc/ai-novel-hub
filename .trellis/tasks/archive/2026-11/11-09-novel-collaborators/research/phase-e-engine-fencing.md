# E 阶段：任务执行与知识库占用接线

## 已接线的内部边界

- 新文档索引受理在同一事务中写入排队 Task 与服务器生成的文档 queue claim。领取先锁认证门禁、相关用户和作品，再以原 Task UUID、入队时间与空 execution CAS 写入新的 execution UUID，并在同一事务将该文档 claim 轮换为该 UUID。领取或占用任一失败会回滚整个事务。
- 领取提交确认丢失时，只检查本次事先生成的 execution UUID、Task UUID 和入队轮次；文档任务还检查对应运行 claim，绝不认领其他执行体的 `running`。不会重新发送模型请求。
- 运行中的文档写入按作品→Task→文档顺序获取事务屏障，并以原执行和文档 claim 检查分块及向量落库；成功写入文档 ready 状态时同事务清除文档占用。失败/中断只可释放原身份。队列取消可在终态事务内释放原文档占用。
- TaskHub 刷新和终态条件写带入队轮次与 execution UUID；管理员 drain 的恢复和成功赢家也持有原轮次、执行身份。成功回调执行期间 Task 已是终态，但保留 execution 标记；任务删除、批量清理、自动裁剪、账号删除及作品排空不可抹去该标记，回调停止后才以原身份清除。失败的异步清理未完成也保持标记。
- 非文档排队任务取消时，先保留 queued 状态、资源唯一约束和随机清理标记；异步 abort 返回后才提交终态，不为未知提交或取消重发清理。文档旧数据未绑定 claim 时只取消旧 Task，不推断或修改文档；随后普通 retry 新建 Task 并建立新 claim。
- 新分块在保存点内先 flush，拒绝认领历史孤儿检索项，验证分块来源和作品后再建立检索索引。

## 前提、明确的未闭合项与恢复

- 任务引擎仍以单进程部署且升级前旧写进程已经排空为前提；没有分布式租约。本阶段不开放协作者 AI、文档索引或后台执行。
- `0046` 章节占用核心尚未接入生产的单章和批量 handler、保存、回退、提案、手工复位及发送 lease。旧章节 `on_abort`、batch `restore_current` 和生成器 finally 仍不能凭 Task UUID 证明当前章节占用；不得将本阶段 Task execution 标记当成章节占用证明。完整章节接线前不能宣称章节竞争已解决。
- 旧数据 `execution_key` 或文档 claim 为 NULL 不会通过补发统计 UUID 认领。旧无文档 claim 的排队任务可由作者取消并重试，获得新 Task 和新文档 claim；旧运行任务的恢复仍须在旧执行器已停且同资源没有新执行体的单进程前提下进行，不能从缺失字段推断旧章或文档所有权。
- 进程崩溃或取消确认丢失时，queued 清理标记或终态 execution 标记可能持久保留。必须先排空旧进程、核对原 Task UUID、入队轮次、execution、资源 claim 和依赖任务事实，证明不存在仍运行的执行体或未知外部调用后，才可按原身份进行经审查的人工修复；成功回调提交结果未知时不可盲目重发。没有可证明章节 claim 的旧章节清理应保留并等待后续接线，不能按 `generating` 状态猜测归属。
- 原有 `test_completed_old_paused_round_cannot_skip_new_round_abort_repair` 期待在排队任务 abort 失败后删除作品时自动重放旧 abort；现在留下 queued 清理标记并返回冲突，暂未闭合。后续需将章节逐次 claim 接入该路径，证明同一 Task UUID、入队轮次、execution 和章节 claim 后实现安全重试，再保留其原有恢复断言。不能修改测试掩盖这一兼容性缺口。
- 原有任务整数 ID 重用测试在刷新/清理期间删除原账号时，应遵循现行认证终检返回 `UnauthorizedError`；保留替换任务及新 Hub 完整性断言。

## 验证记录

- `tests/test_task_execution_fencing.py`：10 项通过，覆盖两种提交确认丢失、实际 HTTP 受理/取消/旧记录重试、claim 轮换失败回滚、作者完整索引成功、旧 abort、旧执行刷写与成功回调期间保护。
- 作者任务、知识库、协作取消和删号组合首次为 85 项通过、1 项错误文案失败；文案修复后该用例通过。最新任务身份与新增专项组合 25 项通过。
- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/pytest -q tests/test_kb.py tests/test_kb_index_claims.py tests/test_admin_task_cancellation.py tests/test_admin_task_check_regressions.py -k 'not test_completed_old_paused_round_cannot_skip_new_round_abort_repair' --tb=short --maxfail=1`：73 项通过、1 项未选中。
- 完成缓存增加 execution 身份后的管理员专项复跑：`LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/pytest -q tests/test_admin_task_cancellation.py tests/test_admin_task_check_regressions.py -k 'not test_completed_old_paused_round_cannot_skip_new_round_abort_repair' --tb=short --maxfail=1`，33 项通过、1 项未选中。
- 未修改的原有测试 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 .venv/bin/pytest -q tests/test_admin_task_cancellation.py::test_completed_old_paused_round_cannot_skip_new_round_abort_repair --tb=short` 实测 **1 failed**：安全收尾标记仍存在时 `stop_project_tasks` 返回 `ConflictError`，未按旧预期再次调用缺少章节 claim 的 abort。运行目录均为 `backend/`。
- 2026-11-10 以 PostgreSQL 16.15、本机独立 `collab_tasks` 库和随机 schema，错峰执行 `tests/test_task_execution_fencing_postgresql.py tests/test_collaboration_task_postgresql.py tests/test_collaboration_notifications_postgresql.py`：**10 passed in 101.95s**。此结果对应本切片释放时的代码；后续章节 claim 接线会修改引擎和任务服务，完成后必须再次验证。
- 改动文件范围内 Ruff 与已跟踪文件 `git diff --check` 通过。
- 本切片不提交、不部署。其他任务正在共享工作区，测试须以实际最终代码重跑为准。
