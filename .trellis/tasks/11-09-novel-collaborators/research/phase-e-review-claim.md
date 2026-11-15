# E4 富文档待审提案与章节占用

> 后续批量接线与可信模型来源更新见 `phase-e-batch-claim.md`；下文的 batch 未接线结论仅反映当时快照。

2026-11-10：本切片只接单章作者任务的候选持久化及采纳核验，不开放协作者章节 AI。

## 实现与事实

- 单章 `ChapterJob` 持执行者在领取时捕获的不可变 `ChapterClaim`；`persist_pending` 按认证/可见性/项目、Task、Chapter 的持锁顺序核验原 Task UUID、入队时间、execution UUID、actor、project、原 grant、章及双向 claim。再核验原 SceneContextStamp、场景身份及原富文档快照；同一事务释放精确 claim 和任务指针、写分块提案及待审断点。任何异常或取消都回滚该事务，避免调用方捕获异常后提交部分提案或释放。
- 提案私有分块保存原 Task/queue/execution/claim/grant 与章身份作为写入时事实，不输出到预览。引擎在暂停后会清空 `Task.execution_key`；采纳因此要求当前任务已暂停且 execution 已释放，并匹配提案内部原队列/任务/执行/章/grant 事实、Task 及提案 run/token、原章内容快照和空闲章状态。采纳锁序调整为项目 → Task/提案 → Chapter，防止新占用被当成旧轮次；相同确认的作者幂等行为保留。
- 保留升级前无执行收据的作者历史提案和尚未接 claim 的作者 batch 提案兼容：这些记录缺少原队列与执行体的可信历史证据，不能声称其具备新单章的执行轮次 fencing。batch `claim=None` 仍是旧作者兼容路径，明确不能覆盖已有新 claim；batch 的逐次 claim、断点及执行体闭环仍需另行接线和验证。当前采纳仍使用作者权限，不放大协作者权限。
- 读取提案的独立会话终检未改变；同项目场景、当前 token、原活动 grant 和读取开始时的任务/提案身份仍会在响应前核验。

## 验证

- 先红：旧保存逻辑遇到已更换的 claim 最终触发章节约束，而不是按原执行身份拒绝；分块写后注入失败、调用方捕获并提交时还能留下孤立提案。旧采纳逻辑在 Task 入队时间改变后仍返回 200 并采纳。以上三个反例在实现前均实际复现。
- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_review_claim.py --tb=short`：16 passed。包括原 claim、排队时刻、执行体、任务/章节身份及 grant 变化，stamp 变化，提案保存和采纳失败后误提交回滚，后来的章节占用，以及正常原子释放。
- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_rich_document_proposals.py tests/test_collaboration_review_read.py --tb=short`：30 passed，覆盖作者确认幂等、批量继续、撤权读取隐私及原有故障路径。
- `uv run ruff check app/services/rich_document_review.py tests/test_collaboration_review_claim.py tests/test_collaboration_review_claim_postgresql.py`：通过；`git diff --check -- backend/app/services/rich_document_review.py`：通过。
- 2026-11-10 在本机隔离 PostgreSQL 16.15 的 `collab_test` 随机 schema 中执行 `tests/test_collaboration_review_claim_postgresql.py`：**3 passed in 12.37s**。覆盖新 claim 先获得锁、提案先获得锁、采纳初查后其他连接更换队列轮次。后续 batch 接线完成后仍须重跑。
