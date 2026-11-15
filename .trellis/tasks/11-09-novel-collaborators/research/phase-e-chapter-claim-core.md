# E4 章节逐次占用原语：当前边界

2026-11-10 在本机 `collab_test` 隔离数据库（PostgreSQL 16.15、随机 schema）运行 `AINOVEL_COLLAB_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_chapter_claim_postgresql.py --tb=short`：**3 passed in 8.23s**。这是原语及迁移的双连接验证；生产章节任务仍未接线，独立复核可能继续修改实现。

本文记录内部原语和仍需完成的接线，不表示章节 AI 协作入口已可启用。源码和测试以当前共享工作区为准；实现刻意没有改变生产任务受理、任务执行或生成流程。

## 持久事实与身份

- 0046 以 0045 为父，`Chapter` 增加 `generation_claim_key`、`generation_claim_task_key`、`generation_claim_prev_status`：必须全空，或完整且章为 `generating`、先前状态为 `draft`/`done`。数据库验证组形状和 UUID 长度；原语还校验规范 UUID、正整数、真实 actor、项目、Task 的 `statistics_key`、`queued_at`、`execution_key` 及原邀请 revision。每次章节抢占新建独立 UUID，同一个 batch 的同一执行体也不能重复使用。
- 单章将 `{chapter_id, claim_key, prev_status}` 写入 `Task.input._chapter_claim`；batch 写入 `Task.checkpoint.current._chapter_claim`，同时保存对应的章节和先前状态。章行与 Task 的记录必须一致，才是有效占用。`ChapterClaim` 是服务端不可变快照，不从客户端接收；旧执行体不能把数据库后来读取的执行身份替换到自己的快照。
- 历史 `generating` 且三列全空仍可存储，但不自动认领或恢复。`queued_at` 缺失、Task UUID 非规范或缺少可信 claim 的旧任务不能进入本协议；`initialize_tracking` 后补的统计 UUID 不能证明旧的章节占用属于该任务。
- 0046 SQLite 使用原位扩列，避免重建章节父表。降级前仍有任何 `generating` 或 claim 列非空则拒绝，包含历史空 claim 的生成中章节；必须先证明旧执行体排空并安全处置。单书备份排除三列并忽略注入，生成中章按原导入协议转 draft；整库备份保留真实运行列，恢复前必须停止旧进程并遵循恢复协议。

## 内部调用合同

1. 单章受理方必须先由服务端创建 Task UUID 和 queued_at、flush Task，再在同事务调用 `claim_queued_chapter`，并只在整个任务受理成功时提交；Task 类型、资源键、真实 actor、项目、请求里的 chapter/project 与原 grant 需要对应。章节锁后重读状态，若不是空 claim 的 draft/done 就拒绝。抢占或激活中途失败会回滚整个事务，调用方不得继续假设其他待写改动仍存在。
2. 领取方先按认证/User → 发送可见性写门禁 → project 的顺序取锁，再于同一事务以原 Task UUID/queued_at CAS `queued→running`，设置本次新 execution UUID 并调用 `activate_queued_chapter`。校验不通过连同领取回滚；未知提交只可从新连接核对**本次原始** task UUID、queued_at、execution UUID、章节 claim 与指针，不可把他人领取视为成功。原语不提交、不实现 commit 结果探测或完成收据。
3. 批量执行方每个 attempt 在 running 事务内调用 `claim_batch_chapter`；只有 `checkpoint.current` 为空才能写入新 claim 和对应章状态。尝试结束必须用该 attempt 原快照处理，不能从当前 checkpoint 抄一个新身份当旧清理的凭据。
4. 保存、提案或其他受保护变更之前调用 `require_running_chapter`，其默认模式按写锁顺序重新验证活动账号、原 grant、Task、章和 claim；上游完成正确的事务及内容版本检查仍是必要条件。独立复核后，`for_update=False` 当前一律失败关闭并回滚，因为本函数无法证明调用方持有物理发送 lease。未来的真实出站验证必须设计绑定同一事务、不可由调用方伪造的 lease 凭据并验证锁序，不能把当前接口当作发送授权。
5. `release_chapter_claim` 是系统清理，调用方必须先证明该旧执行体已停止。它不要求已撤权 actor 保持成员权限，但仍校验原 Task、queued/execution、原 grant 记录、章行和原指针，只按原 claim 比较回退为原状态并清空指针，不写正文；身份不匹配返回 `False`。原任务已删除或证据缺损时不猜测，不擅自清理。可匹配 running 或 failed/cancelled/interrupted；没有证明时不把 succeeded/paused 当作普通失败清理。
6. `reset_generating_chapter` 是供将来人工状态入口使用的持锁 CAS：由有 `content.edit` 的 actor 将 generating 复位为 draft/done，清空章上准确当前 claim，不写正文、不篡改旧任务指针。必须在旧出站和写回也接入该 fencing 后才能启用；否则单独接线不能阻止旧任务继续发送。

所有读写在 `no_autoflush` 下验证，身份字段存在待写修改会拒绝；涉及数据库的错误抑制原始 SQL/参数，不把正文带入异常日志。新原语仅协调短数据库事务，不持写事务等待模型响应。系统 cleanup 通过 project → Task → Chapter 取锁；若同一事务还要执行认证写操作，调用方应先按完整认证锁序取到项目屏障，不能从清理反向取锁。

## 已验证及待验证

先编写专项测试并确认原语缺失时无法收集，然后实现。SQLite 核心、迁移和单书备份专项在 `LC_ALL=en_US.UTF-8 uv run pytest -q tests/test_chapter_claim_core.py tests/test_chapter_claim_migration.py` 下 17 项通过；覆盖同执行体 batch 重抢、手工复位后新任务防旧清理、撤权后只允许系统清理、损坏输入、失败后调用方再提交、数据库异常内容保护和 SQLite 真实父子数据迁移。新增整库快照断言后，使用默认 locale 再运行迁移两项、注册表两项与 KB 单书备份一项，5 项通过；新增无效身份前已 flush 的事务回滚单项也通过。Ruff 检查和 `git diff --check` 均通过。

`tests/test_chapter_claim_postgresql.py` 已编写 0046 升降级与双连接阻塞测试，本文开头的 3 项通过是独立复核修改前的结果；复核后的实现仍需错峰重跑。迁移测试仅允许本机 `collab_chapter` 专库，或回退 `collab_test`；并发测试仅允许本机 `collab_test`，均用随机 schema。无配置时三项会跳过，跳过不是 PostgreSQL 通过证据。

本切片**尚未**接 `task_service`、`task_engine`、章节生成、batch、富文档提案、人工章状态编辑、实际发送 lease、用户删除/任务裁剪或恢复。现有生产路径仍可能使用仅基于 `generating` 的旧清理，不能宣称 E4 完整、不能据此开放协作者生成，也不能将此中间状态部署为可用的协作 AI。确认未知、取消终态与清理原子性、写回收据、真实 HTTP 发送竞争、旧任务修复及衍生任务身份仍须在完整接线后独立验证。
