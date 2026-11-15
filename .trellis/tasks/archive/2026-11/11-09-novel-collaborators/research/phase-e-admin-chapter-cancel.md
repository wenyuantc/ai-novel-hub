# 管理员单章任务取消与恢复

管理员受理单章 queued/running 取消时，先按 User→作品顺序加锁，在持久取消意图事务中冻结原 Task UUID、入队代次、实际 execution、章节 claim 与操作 UUID。旧任务缺少完整 claim 或身份时，管理员意图保留，后台不根据 `generating` 状态推断归属。

queued 单章由 drain 以原任务和意图 CAS 写终态，同一事务以原 claim 释放章节、更新统计并写入内部收据。孤立 running 还必须在单进程及旧进程排空前提下证明原执行体已停止，并以受理时的 execution 和 claim 执行同一事务；其他项目清理仍在运行时暂不接管。指针、执行代次或章节归属变化时回滚事务，保留意图，不触碰后来占用。

提交收据记录同一次原任务、入队、执行、claim 与管理员操作；queued 使用本次服务端生成的临时完成 UUID，running 保留实际 execution UUID。收据与终态和原 claim 释放同时提交，Task 在收据待确认时仍有管理员意图及 execution 标记，不能被裁剪或删号。第二个 CAS 仅清除经核实的同一收据和标记，不再次操作章节。提交确认丢失时只核验该收据；进程崩溃后 scan 可凭持久收据继续清标记，不重放旧章节清理。个人投影将 `_admin_chapter_cancel` 与 `_admin_chapter_receipt` 作为内部字段递归过滤。

旧回归 `test_completed_old_paused_round_cannot_skip_new_round_abort_repair` 原来将没有 chapter_id/claim 的 `chapter.analyze` 任意关联到另一章，无法安全证明旧清理归属。现仅改变其第二轮造数：在同一事务写入真实单章 Task 并绑定 claim，以 claim 释放回调模拟第一次失败、第二次成功；保留冻结时钟、旧完成缓存不跳过新轮次、两次调用及最终章状态等原断言。另加无章节关联的旧 analysis 失败关闭测试，验证失败后不自动重放会改任意章的旧 abort。

## 验证

- 新增 SQLite `tests/test_admin_chapter_claim_cancellation.py`：**12 passed**，覆盖 queued 原子取消、孤立 running、旧 claim/执行替换、执行体未停止、回滚、两次提交确认丢失、收据恢复、无证据旧任务与旧 analysis 不重放，也覆盖原取消提交确认丢失后新任务抢占章节。
- 主会话在本机隔离 PostgreSQL 上错峰运行 `tests/test_admin_chapter_claim_cancellation_postgresql.py`：**1 passed in 18.10s**。该测试在真实两连接中暂停章节释放事务，验证其他连接仍见旧状态且项目写锁阻塞，提交后任务终态与章节占用一致。该次发生在补充 running 登记窗口检查之前，最终代码需重跑。
- 原有管理员保存竞争测试已修正两种真实时间顺序：取消意图先到达时验证失败日志活游标不被取消；正文提交先完成时再受理管理员取消，验证已提交的成功结果获胜。测试继续检查日志、版本、终态和用户正文。
- 原有 `test_drain_commit_confirmation_loss_cannot_replay_old_abort_on_new_resource` 也由无证据的模拟章状态改为实际 queued→running 原 claim 及后续新的 queued claim；仍检查提交确认丢失、原/新 claim 各清一次、原 Hub 关闭及旧清理不破坏新占用。改造后该用例单项通过。
- 涉及的管理员 task_key API 测试使用原创建响应中的 task_key，不通过当前整数 ID 查询新身份。章节结束回调测试显式传原 claim。
- `tests/test_admin_chapter_claim_cancellation.py tests/test_admin_task_cancellation.py tests/test_admin_task_check_regressions.py`：**46 passed in 63.65s**；之后仅新增当 `_claiming`、`_finalizing` 或 `_admitted` 仍含该任务时禁止孤立 running 接管的保守检查，对应单项 `test_admin_waits_for_original_running_executor_to_stop` 在最终代码上 **1 passed in 3.63s**。范围内 Ruff 与 `git diff --check` 通过；完整组合及 PG 最终代码由主会话错峰复验。

本切片不开放协作者生成、不引入多进程租约，也不声称历史 NULL claim 可以自动修复。原有用户取消和引擎恢复路径由各自持有者独立接线和验证；Agent 外部调用删除屏障及 0047 身份接线不属于此切片。
